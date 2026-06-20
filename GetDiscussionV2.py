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

    # The episode number arrives local to the title the user is watching (e.g. Crunchyroll
    # numbers each season continuously across its cours). The split-cour walk-forward below
    # maps that number onto the correct AniList/MAL entry, so no global-offset adjustment is
    # needed — attempting one mis-resolved split-cour shows onto early episodes.

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
