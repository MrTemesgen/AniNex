import requests
from flask import jsonify, current_app
from bs4 import BeautifulSoup
import re
import os
import constants
from urllib.parse import urljoin

CLIENT_ID = os.getenv('CLIENT_ID')

# ---------------------------------------------------------
# 1. ANILIST GRAPHQL QUERY
# ---------------------------------------------------------

def fetch_season_tree(search_term):
    res = requests.post(constants.ANILIST_API_URL, json={'query': constants.GRAPHQL_QUERY, 'variables': {'search': search_term}}, timeout=10)
    # AniList returns {"data": null, "errors": [...]} on error, so guard against None.
    return (res.json().get('data') or {}).get('Media')

# Small in-process cache so repeated chain walks (and repeat requests for popular shows)
# don't re-hit AniList for the same node. Bounded to avoid unbounded growth on a long-lived dyno.
_NODE_CACHE = {}
_NODE_CACHE_MAX = 512

def fetch_node_relations(anilist_id):
    """Fetch a single Media's immediate relations by AniList id, for stepping along a
    franchise chain when the nested season tree runs out of depth. Returns None on failure."""
    if not anilist_id:
        return None
    if anilist_id in _NODE_CACHE:
        return _NODE_CACHE[anilist_id]
    try:
        res = requests.post(constants.ANILIST_API_URL, json={'query': constants.GRAPHQL_NODE_QUERY, 'variables': {'id': anilist_id}}, timeout=10)
        node = (res.json().get('data') or {}).get('Media')
    except Exception as e:
        current_app.logger.error(f"AniList node fetch failed for id {anilist_id}: {e}")
        return None
    if node:
        if len(_NODE_CACHE) >= _NODE_CACHE_MAX:
            _NODE_CACHE.clear()
        _NODE_CACHE[anilist_id] = node
    return node

def _normalize_title(title):
    return re.sub(r'\s+', ' ', (title or '')).strip().lower()

def _tv_episode_count(node):
    """Episode count for a node, falling back to nextAiringEpisode for currently-airing
    entries. Returns 0 when unknown so it doesn't distort sums."""
    count = node.get('episodes')
    if not count:
        next_airing = node.get('nextAiringEpisode')
        count = (next_airing.get('episode', 1) - 1) if next_airing else 0
    return count or 0

def _is_non_tv(node):
    return node.get('format') in [constants.FORMAT_MOVIE, constants.FORMAT_OVA, constants.FORMAT_SPECIAL]

def _related_node(node, relation_type):
    for edge in (node.get('relations') or {}).get('edges', []):
        if edge['relationType'] == relation_type:
            return edge['node']
    return None

def _step(node, relation_type):
    """Follow one prequel/sequel hop, re-querying AniList if the in-tree node lacks relations."""
    nxt = _related_node(node, relation_type)
    if nxt is None and node.get('id'):
        refetched = fetch_node_relations(node['id'])
        if refetched:
            nxt = _related_node(refetched, relation_type)
    return nxt

def calculate_season_span(season_node):
    """Total canonical-TV episodes for this season across its cours (e.g. '... Part 2'),
    following SEQUEL links while the title stays a continuation of this season. Used to
    decide whether an episode number is too large to be local to this season."""
    base = _normalize_title(season_node.get('title', {}).get('romaji'))
    total = _tv_episode_count(season_node)
    visited = {season_node.get('id') or season_node.get('idMal')}
    current = season_node

    for _ in range(20):  # bound against cycles / runaway chains
        nxt = _step(current, constants.RELATION_TYPE_SEQUEL)
        if not nxt:
            break
        # A cour continuation's title is this season's title plus a suffix (Part 2, etc.).
        # A different base title marks the start of the next season, so stop there.
        if not _normalize_title(nxt.get('title', {}).get('romaji')).startswith(base):
            break
        key = nxt.get('id') or nxt.get('idMal')
        if key in visited:
            break
        visited.add(key)
        if not _is_non_tv(nxt):
            total += _tv_episode_count(nxt)
        current = nxt

    return total

def calculate_global_offset(season_node):
    """Total canonical-TV episodes that aired BEFORE this season — the sum of its prequel
    chain back to the franchise root. Steps hop-by-hop, re-querying AniList as needed so
    long franchises are fully traversed (a single nested query can't reach the root)."""
    offset = 0
    visited = {season_node.get('id') or season_node.get('idMal')}
    current = season_node

    for _ in range(20):  # bound against cycles / runaway chains
        prev = _step(current, constants.RELATION_TYPE_PREQUEL)
        if not prev:
            break
        key = prev.get('id') or prev.get('idMal')
        if key in visited:
            break
        visited.add(key)
        if not _is_non_tv(prev):
            offset += _tv_episode_count(prev)
        current = prev

    return offset

# ---------------------------------------------------------
# 2. RESOLVERS & FALLBACKS
# ---------------------------------------------------------

def fallback_mal_search(anime_query, season):
    if not anime_query:
        return None
        
    season_str = str(season).strip()
    if season_str == '1' or season_str == '0' or season_str.lower() in ['movie', 'ova', 'special']:
        search_term = anime_query
    else:
        search_term = f"{anime_query} Season {season}"
        
    try:
        url = constants.MAL_ANIME_URL
        params = {'q': search_term, 'limit': 1}
        response = requests.get(url, params=params, headers={'X-MAL-CLIENT-ID': CLIENT_ID}, timeout=10)
        
        if response.status_code == 200:
            data = response.json().get('data', [])
            if data:
                return data[0]['node']['id']
    except Exception as e:
        current_app.logger.error(f"Fallback MAL search failed for {search_term}: {e}")
        
    return None

def resolve_mal_id_with_split_cour(anime_query, season, episode):
    target_ep = int(episode)
    season_str = str(season).strip()
    
    # 1. Build targeted search
    if season_str == '1' or season_str == '0' or season_str.lower() in ['movie', 'ova', 'special']:
        search_term = anime_query
    else:
        search_term = f"{anime_query} Season {season}"
        
    current_app.logger.info(f"Hybrid search term: {search_term}")
    
    # 2. Fetch the starting node for this specific season
    current_node = fetch_season_tree(search_term)
    
    if not current_node:
        return fallback_mal_search(anime_query, season), target_ep, search_term.replace(' ', '_')

    # Crunchyroll usually numbers an episode locally to the season being watched (continuous
    # across that season's cours), but sometimes sends the franchise-wide (global) number with
    # a correct season. Only reinterpret as global when BOTH hold: a real season > 1 was given
    # AND the episode exceeds this season's full length. When the season is unknown/1 we stay
    # local, so split-cour locals (e.g. Dr. Stone) are never mis-mapped onto early episodes.
    if season_str.isdigit() and int(season_str) > 1:
        season_span = calculate_season_span(current_node)
        if target_ep > season_span:
            offset = calculate_global_offset(current_node)
            current_app.logger.info(f"Episode {target_ep} exceeds season span {season_span}; treating as GLOBAL (prequel offset {offset}).")
            # Only subtract a sane offset; otherwise leave the number untouched and treat as local.
            if 0 < offset < target_ep:
                target_ep -= offset
        else:
            current_app.logger.info(f"Episode {target_ep} within season span {season_span}; treating as LOCAL.")

    accumulated_eps = 0
    
    # 3. Walk forward ONLY from the start of the requested season (handling split cours)
    while current_node:
        mal_id = current_node.get('idMal')
        fmt = current_node.get('format')
        
        ep_count = current_node.get('episodes')
        if not ep_count:
            next_airing = current_node.get('nextAiringEpisode')
            ep_count = (next_airing.get('episode', 2) - 1) if next_airing else 999
            
        title_node = current_node.get('title', {})
        title = title_node.get('romaji') or title_node.get('english') or search_term
        slug = title.replace(' ', '_')
        
        # Skip non-TV formats UNLESS the user explicitly asked for Season 0/Movie
        if fmt in [constants.FORMAT_MOVIE, constants.FORMAT_OVA, constants.FORMAT_SPECIAL] and season_str not in ['0', 'movie', 'ova', 'special']:
            pass 
        else:
            # Check if the requested episode falls in this part of the split-cour
            if target_ep <= (accumulated_eps + ep_count):
                local_ep = target_ep - accumulated_eps
                
                if not mal_id:
                    mal_id = fallback_mal_search(title, season)
                    
                return mal_id, local_ep, slug
            
            accumulated_eps += ep_count
            
        # Move to the sequel
        next_node = None
        if current_node.get('relations') and current_node['relations'].get('edges'):
            for edge in current_node['relations']['edges']:
                if edge['relationType'] == constants.RELATION_TYPE_SEQUEL:
                    next_node = edge['node']
                    break
                    
        current_node = next_node

    # If math fails entirely, return the base search and hope for the best
    return fallback_mal_search(anime_query, season), target_ep, search_term.replace(' ', '_')

# ---------------------------------------------------------
# 3. SCRAPERS & FORUM SEARCH
# ---------------------------------------------------------

def get_discussion_link(anime, id, episode):
    try:
        episode = int(episode)
        offset = ((episode-1)//100)*100 if episode > 100 else 0
        BASE_URL = f'https://myanimelist.net/anime/{id}/{anime}/episode?offset={offset}'
        
        response = requests.get(BASE_URL, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table',  {'class': 'episode_list'})

        if table is None: return None

        remainder = (episode % 100)
        idx = 100 if remainder == 0 else remainder 
        row = table.find_all('tr')[idx]
        link = row.find_all(['td', 'th'])[-1].find('a')['href']
        
        # --- NEW: Strict Regex Extraction ---
        match = re.search(r'topicid=(\d+)', link)
        return match.group(1) if match else None
        
    except Exception as e:
        current_app.logger.error(f"Scraper failed for {anime}-{episode}: {e}")
        return None

def fallback_forum_search(clean_title, local_ep):
    # If the episode just aired and the HTML table isn't updated, search the forum directly
    query = f"{clean_title} Episode {local_ep} Discussion"
    try:
        url = constants.MAL_FORUM_URL
        params = {'q': query, 'limit': 5}
        response = requests.get(url, params=params, headers={'X-MAL-CLIENT-ID': CLIENT_ID}, timeout=10)
        
        if response.status_code == 200:
            topics = response.json().get('data', [])
            for topic in topics:
                # Basic sanity check: ensure the episode number is actually in the title
                if str(local_ep) in topic.get('title', ''):
                    return topic.get('id')
    except Exception as e:
        current_app.logger.error(f"Fallback forum search failed: {e}")
    return None

def normalize_text(value):
    if not value:
        return ""
    return re.sub(r'\s+', ' ', value).strip()

def scrape_forum_topic_html(discussion_id):
    try:
        topic_url = f"https://myanimelist.net/forum/?topicid={discussion_id}"
        response = requests.get(topic_url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        title = normalize_text(soup.title.get_text()) if soup.title else f"MAL Topic {discussion_id}"

        post_selectors = [
            'div.message-wrapper',
            'table.body[id^="message"]',
            'div.forum-topic-message.message',
            'div.forum-post',
            'div.js-forum-topic-post',
            'tr[id^="topicRow"]',
            'div[id^="message"]',
            'table.forum_board_view tr',
        ]

        containers = []
        for selector in post_selectors:
            containers = soup.select(selector)
            if containers:
                break

        posts = []
        seen_keys = set()

        for index, container in enumerate(containers, start=1):
            profile_link = container.select_one('a[href*="/profile/"], a[href*="profile.php"]')
            body_node = container.select_one(
                '.forum-topic-message.message, table.body td, table.body, .message, .content, .forum-post-message, .js-forum-post-body, [id^="postMessage"]'
            )

            body_text = normalize_text(body_node.get_text(" ", strip=True) if body_node else container.get_text(" ", strip=True))
            if not body_text:
                continue

            username = normalize_text(profile_link.get_text(" ", strip=True) if profile_link else "")
            author_href = profile_link.get('href') if profile_link else None

            time_node = container.select_one('time, .date, .forum-post-date, .message-header .date, small')
            created_at = normalize_text(
                (time_node.get('datetime') if time_node and time_node.has_attr('datetime') else time_node.get_text(" ", strip=True))
                if time_node else ""
            )

            post_anchor = (
                container.get('id')
                or (body_node.get('id') if body_node else None)
                or (container.select_one('table.body[id]')['id'] if container.select_one('table.body[id]') else None)
                or f"post-{index}"
            )
            dedupe_key = (username, body_text[:120])
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            posts.append({
                'id': post_anchor,
                'number': len(posts) + 1,
                'created_at': created_at,
                'created_by': {
                    'name': username,
                    'forum_avator': '',
                    'href': urljoin(topic_url, author_href) if author_href else ''
                },
                'body': body_text,
            })

        if not posts:
            return None

        return {
            'id': int(discussion_id),
            'title': title,
            'num_of_posts': len(posts),
            'posts': posts,
            'source': 'html_scrape',
            'url': topic_url,
        }
    except Exception as e:
        current_app.logger.error(f"Forum HTML scrape failed for topic {discussion_id}: {e}")
        return None

# ---------------------------------------------------------
# 4. MAIN ENDPOINT
# ---------------------------------------------------------

def get_discussion(anime_query, season, episode):
    # Use our hybrid split-cour resolver
    mal_id, local_ep, anime_slug = resolve_mal_id_with_split_cour(anime_query, season, episode)

    if not mal_id:
        return jsonify(message=constants.MESSAGE_MAL_ID_NOT_FOUND)

    # 1. Scrape the discussion ID
    discussion_id = get_discussion_link(anime_slug, mal_id, local_ep)
    
    # 2. Direct forum search fallback
    if not discussion_id:
        current_app.logger.info("Scraping failed. Attempting direct forum search...")
        clean_title = anime_slug.replace('_', ' ') if anime_slug else anime_query
        discussion_id = fallback_forum_search(clean_title, local_ep)

    if not discussion_id:
        return jsonify(message=constants.MESSAGE_DISCUSSION_NOT_FOUND)

    # Fetch the forum posts
    mal_forum_url = f"https://api.myanimelist.net/v2/forum/topic/{discussion_id}?limit=100"
    response = requests.get(mal_forum_url, headers={'X-MAL-CLIENT-ID': CLIENT_ID}, timeout=10)
    
    # Prefer the structured API response when MAL allows it.
    mal_data = response.json()
    if 'error' in mal_data:
        current_app.logger.error(f"MAL API Error: {mal_data}")
        error_payload = mal_data.get('error', {})
        error_code = error_payload.get('error') if isinstance(error_payload, dict) else error_payload
        if error_code == 'forbidden':
            current_app.logger.info(f"Falling back to HTML forum scrape for topic {discussion_id}")
            scraped_topic = scrape_forum_topic_html(discussion_id)
            if scraped_topic:
                return jsonify(message=scraped_topic)
        return jsonify(error=mal_data, message="MAL API rejected the discussion ID.")
        
    return jsonify(message=mal_data.get('data', {}))
