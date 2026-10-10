import requests
from flask import jsonify, current_app
from bs4 import BeautifulSoup
import re
import os
import unicodedata
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
import constants
from urllib.parse import urljoin
from scrape_cache import scrape_result_cache
from request_timing import timed

CLIENT_ID = os.getenv('CLIENT_ID')

# ---------------------------------------------------------
# 1. ANILIST GRAPHQL QUERY
# ---------------------------------------------------------

class _TTLCache:
    """Small bounded in-process cache. Repeat lookups for popular shows otherwise cost
    dozens of AniList calls each, which runs into AniList's per-minute rate limit."""

    _MISS = object()

    def __init__(self, ttl, max_entries, clock=monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self.entries = {}
        self.lock = Lock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return False, None
            if entry[0] <= self.clock():
                del self.entries[key]
                return False, None
            return True, entry[1]

    def set(self, key, value):
        with self.lock:
            if key not in self.entries and len(self.entries) >= self.max_entries:
                self.entries.pop(next(iter(self.entries)))
            self.entries[key] = (self.clock() + self.ttl, value)

    def clear(self):
        with self.lock:
            self.entries.clear()


_SEARCH_CACHE = _TTLCache(ttl=3600, max_entries=512)  # search term -> AniList id (or None)
_TREE_CACHE = _TTLCache(ttl=3600, max_entries=256)    # AniList id -> nested season tree
_NODE_CACHE = _TTLCache(ttl=3600, max_entries=512)    # AniList id -> immediate relations
_TOPIC_CACHE = _TTLCache(ttl=60, max_entries=128)     # MAL topic id -> assembled topic


def clear_caches():
    for cache in (_SEARCH_CACHE, _TREE_CACHE, _NODE_CACHE, _TOPIC_CACHE):
        cache.clear()


def _is_anilist_not_found(res, payload, root):
    # A missing catalog entry is not a provider outage. Only accept AniList's
    # explicit not-found shape; rate limits and other errors must still fail.
    if not isinstance(payload, dict):
        return False
    errors = payload.get('errors')
    data = payload.get('data')
    return (res.status_code in (200, 404) and isinstance(errors, list) and bool(errors)
            and all(isinstance(error, dict) and error.get('status') == 404 for error in errors)
            and (data is None or data == {root: None}))


def _anilist_media(query, variables, stage):
    """Run an AniList query whose result is `data.Media`. Returns None only for AniList's
    explicit not-found shape; rate limits and other errors raise so a partial franchise
    walk can never silently produce the wrong episode."""
    with timed(stage):
        res = requests.post(constants.ANILIST_API_URL, json={'query': query, 'variables': variables}, timeout=10)
    # AniList returns {"data": null, "errors": [...]} on error, so guard against None.
    payload = res.json()
    if _is_anilist_not_found(res, payload, 'Media'):
        return None
    res.raise_for_status()
    if not isinstance(payload, dict) or payload.get('errors'):
        raise ValueError('Invalid AniList response')
    data = payload.get('data')
    if data is not None and not isinstance(data, dict):
        raise ValueError('Invalid AniList data')
    node = (data or {}).get('Media')
    if node is not None and not isinstance(node, dict):
        raise ValueError('Invalid AniList media')
    return node


def _search_candidates(search_term):
    with timed('anilist_search'):
        res = requests.post(constants.ANILIST_API_URL, json={
            'query': constants.GRAPHQL_SEARCH_QUERY, 'variables': {'search': search_term}}, timeout=10)
    payload = res.json()
    if _is_anilist_not_found(res, payload, 'Page') or _is_anilist_not_found(res, payload, 'Media'):
        return []
    res.raise_for_status()
    if not isinstance(payload, dict) or payload.get('errors'):
        raise ValueError('Invalid AniList response')
    data = payload.get('data')
    page = data.get('Page') if isinstance(data, dict) else None
    media = page.get('media') if isinstance(page, dict) else None
    if not isinstance(media, list) or not all(isinstance(item, dict) for item in media):
        raise ValueError('Invalid AniList search results')
    return media


def _title_key(value):
    """Compare titles independent of case, punctuation and typographic variants
    (Crunchyroll's "Journey's" vs AniList's "Journey’s", "SPY x FAMILY" vs "SPY×FAMILY")."""
    value = unicodedata.normalize('NFKC', value or '').lower().replace('×', 'x')
    return ' '.join(re.findall(r'[^\W_]+', value))


def _media_titles(media):
    titles = [value for value in (media.get('title') or {}).values() if isinstance(value, str)]
    titles += [value for value in (media.get('synonyms') or []) if isinstance(value, str)]
    return {_title_key(value) for value in titles if value}


def _title_matches(query, media):
    return _title_key(query) in _media_titles(media)


_FORMAT_RANK = {'TV': 0, 'TV_SHORT': 0, 'ONA': 1}


def choose_search_candidate(search_term, candidates):
    """Prefer an entry whose title exactly matches the requested title. AniList's fuzzy
    ranking can put a spin-off first (Frieren's "●● no Mahou" ONA shorts outrank the
    main series). Without an exact match keep AniList's own first result."""
    exact = [(index, media) for index, media in enumerate(candidates) if _title_matches(search_term, media)]
    if exact:
        return min(exact, key=lambda pair: (
            _FORMAT_RANK.get(pair[1].get('format'), 2), -(pair[1].get('popularity') or 0), pair[0]))[1]
    return candidates[0] if candidates else None


def fetch_media_tree(anilist_id):
    """The nested season tree for one AniList entry."""
    hit, node = _TREE_CACHE.get(anilist_id)
    if not hit:
        node = _anilist_media(constants.GRAPHQL_QUERY, {'id': anilist_id}, 'anilist_tree')
        _TREE_CACHE.set(anilist_id, node)
    return node


def fetch_season_tree(search_term):
    hit, anilist_id = _SEARCH_CACHE.get(search_term)
    if not hit:
        candidate = choose_search_candidate(search_term, _search_candidates(search_term))
        anilist_id = candidate.get('id') if candidate else None
        if anilist_id is not None and (isinstance(anilist_id, bool) or not isinstance(anilist_id, int)):
            raise ValueError('Invalid AniList id')
        _SEARCH_CACHE.set(search_term, anilist_id)
    return fetch_media_tree(anilist_id) if anilist_id is not None else None


def fetch_node_relations(anilist_id):
    """Fetch a single Media's immediate relations by AniList id, for stepping along a
    franchise chain when the nested season tree runs out of depth. Provider failures
    raise rather than truncating the chain (a short chain means a wrong episode offset)."""
    if not anilist_id:
        return None
    hit, node = _NODE_CACHE.get(anilist_id)
    if not hit:
        node = _anilist_media(constants.GRAPHQL_NODE_QUERY, {'id': anilist_id}, 'anilist_relations')
        _NODE_CACHE.set(anilist_id, node)
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

_COUR_SUFFIX = re.compile(r'(?:part|cour) (?:\d+|ii|iii|iv)|\d+(?:st|nd|rd|th) (?:part|cour)')


def _is_cour_continuation(previous, following):
    """True when `following` is the same season's next cour ("X Part 2", "X Cour 2"),
    not a new season or arc ("X 2nd Season", "X: Swordsmith Village Arc")."""
    for field in ('romaji', 'english'):
        base = _title_key((previous.get('title') or {}).get(field))
        title = _title_key((following.get('title') or {}).get(field))
        if base and title.startswith(base + ' ') and _COUR_SUFFIX.fullmatch(title[len(base) + 1:]):
            return True
    return False


def find_nth_tv_season(base_node, season_number):
    """Walk the SEQUEL chain from a franchise's first entry and return the entry that
    starts its Nth TV season. Movies/OVAs/specials are skipped and later cours of the
    same season don't count. Used when "<title> Season N" isn't an AniList title, as
    with arc-named seasons (Demon Slayer's Swordsmith Village Arc)."""
    if season_number < 2 or _is_non_tv(base_node):
        return None
    season, season_start, current = 1, base_node, base_node
    visited = {base_node.get('id') or base_node.get('idMal')}
    for _ in range(30):  # bound against cycles / runaway chains
        nxt = _step(current, constants.RELATION_TYPE_SEQUEL)
        if not nxt:
            return None
        key = nxt.get('id') or nxt.get('idMal')
        if key in visited:
            return None
        visited.add(key)
        current = nxt
        if _is_non_tv(nxt) or _is_cour_continuation(season_start, nxt):
            continue
        season, season_start = season + 1, nxt
        if season == season_number:
            return nxt
    return None

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
        with timed('mal_anime_search'):
            response = requests.get(url, params=params, headers={'X-MAL-CLIENT-ID': CLIENT_ID}, timeout=10)
        
        if response.status_code == 200:
            data = response.json().get('data', [])
            if data:
                return data[0]['node']['id']
    except Exception as e:
        current_app.logger.warning("Discussion provider request failed.")
        
    return None

def resolve_mal_id_with_split_cour(anime_query, season, episode):
    target_ep = int(episode)
    season_str = str(season).strip()
    
    # 1. Build targeted search
    if season_str == '1' or season_str == '0' or season_str.lower() in ['movie', 'ova', 'special']:
        search_term = anime_query
    else:
        search_term = f"{anime_query} Season {season}"
        
    current_app.logger.debug("Resolving episode discussion.")
    
    # 2. Fetch the starting node for this specific season
    current_node = fetch_season_tree(search_term)
    if not current_node and season_str.isdigit() and int(season_str) > 1:
        base_node = fetch_season_tree(anime_query)
        if base_node and _title_matches(anime_query, base_node):
            # Seasons named after arcs aren't findable as "<title> Season N"; count
            # TV seasons along the franchise's sequel chain instead.
            nth = find_nth_tv_season(base_node, int(season_str))
            if nth:
                current_node = (fetch_media_tree(nth['id']) if nth.get('id') else None) or nth
            # Streaming services can divide one catalog entry into several seasons.
            # Preserve the absolute episode only for a single-season franchise whose
            # TV entry's episode count covers the number. A franchise with real later
            # seasons must not fall back to its first season's episodes.
            elif (base_node.get('format') == 'TV'
                    and 0 < target_ep <= _tv_episode_count(base_node)
                    and not find_nth_tv_season(base_node, 2)):
                return base_node.get('idMal'), target_ep, (
                    base_node['title'].get('romaji') or anime_query).replace(' ', '_')

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
            current_app.logger.debug("Resolving episode discussion.")
            # Only subtract a sane offset; otherwise leave the number untouched and treat as local.
            if 0 < offset < target_ep:
                target_ep -= offset
        else:
            current_app.logger.debug("Resolving episode discussion.")

    accumulated_eps = 0
    hops = 0
    visited = {current_node.get('id') or current_node.get('idMal')}

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
            
        # Move to the sequel, re-querying when the nested tree runs out of depth.
        hops += 1
        next_node = _step(current_node, constants.RELATION_TYPE_SEQUEL) if hops < 20 else None
        next_key = next_node and (next_node.get('id') or next_node.get('idMal'))
        if next_key and next_key in visited:
            next_node = None
        visited.add(next_key)
        current_node = next_node

    # If math fails entirely, return the base search and hope for the best
    return fallback_mal_search(anime_query, season), target_ep, search_term.replace(' ', '_')

# ---------------------------------------------------------
# 3. SCRAPERS & FORUM SEARCH
# ---------------------------------------------------------

def _timed_get(stage, url):
    # Only runs on a scrape-cache miss, so cache hits add no stage time.
    with timed(stage):
        return requests.get(url, timeout=10)


def parse_episode_links(content, offset):
    """Retain only episode-to-topic IDs, never the upstream page."""
    soup = BeautifulSoup(content, 'html.parser')
    table = soup.find('table', {'class': 'episode_list'})
    if table is None:
        return None
    links = {}
    for number, row in enumerate(table.find_all('tr')[1:], start=offset + 1):
        cells = row.find_all('td')
        if not cells:
            continue
        # Use the explicit episode column when present; retain the old positional
        # interpretation for tables without one. Missing links never shift rows.
        label = cells[0].get_text(strip=True)
        episode = int(label) if label.isdigit() else number
        for link in cells[-1].find_all('a', href=True):
            match = re.search(r'(?:[?&])topicid=(\d+)', link['href'])
            if match:
                links[str(episode)] = match[1]
                break
    return links or None


def get_discussion_link(anime, id, episode):
    try:
        episode = int(episode)
        offset = ((episode-1)//100)*100 if episode > 100 else 0
        BASE_URL = f'https://myanimelist.net/anime/{id}/{anime}/episode?offset={offset}'
        
        links = scrape_result_cache.get(
            ('episodes', int(id), offset), 600,
            lambda: _timed_get('mal_episode_page', BASE_URL),
            lambda body: parse_episode_links(body, offset))
        return links.get(str(episode))
        
    except Exception as e:
        current_app.logger.warning("Discussion provider request failed.")
        return None

def fallback_forum_search(clean_title, local_ep):
    # If the episode just aired and the HTML table isn't updated, search the forum directly
    query = f"{clean_title} Episode {local_ep} Discussion"
    try:
        url = constants.MAL_FORUM_URL
        params = {'q': query, 'limit': 5}
        with timed('mal_forum_search'):
            response = requests.get(url, params=params, headers={'X-MAL-CLIENT-ID': CLIENT_ID}, timeout=10)
        
        if response.status_code == 200:
            topics = response.json().get('data', [])
            for topic in topics:
                # Basic sanity check: ensure the episode number is actually in the title
                if re.search(r'\bEpisode\s+' + re.escape(str(local_ep)) + r'\b', topic.get('title', ''), re.IGNORECASE):
                    return topic.get('id')
    except Exception as e:
        current_app.logger.warning("Discussion provider request failed.")
    return None

def normalize_text(value):
    if not value:
        return ""
    return re.sub(r'\s+', ' ', value).strip()

def scraped_post_time(time_node):
    """ISO 8601 UTC like the MAL API, or '' — never MAL's display text ("Yesterday, 3:20 AM"),
    which browsers can't parse and which makes the extension's date sort inconsistent."""
    if time_node is None:
        return ''
    epoch = time_node.get('data-time')
    if epoch and str(epoch).isdigit():
        try:
            return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ''
    value = normalize_text(time_node.get('datetime'))
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None
    except ValueError:
        return ''
    if parsed is None or parsed.tzinfo is None:
        return ''
    return parsed.astimezone(timezone.utc).isoformat()


def parse_forum_topic(content, discussion_id):
    topic_url = f"https://myanimelist.net/forum/?topicid={discussion_id}"
    soup = BeautifulSoup(content, 'html.parser')
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
        created_at = scraped_post_time(time_node)

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


def scrape_forum_topic_html(discussion_id):
    try:
        topic_id = int(discussion_id)
        topic_url = f"https://myanimelist.net/forum/?topicid={topic_id}"
        return scrape_result_cache.get(
            ('topic', topic_id), 60,
            lambda: _timed_get('mal_topic_scrape', topic_url),
            lambda body: parse_forum_topic(body, topic_id))
    except Exception:
        current_app.logger.warning("Discussion provider request failed.")
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

    error_code, topic = fetch_mal_topic(discussion_id)
    if error_code == 'not_found':
        return jsonify(message=constants.MESSAGE_DISCUSSION_NOT_FOUND)
    if error_code == 'forbidden':
        current_app.logger.debug("Resolving episode discussion.")
        scraped_topic = scrape_forum_topic_html(discussion_id)
        if scraped_topic:
            return jsonify(message=scraped_topic)
    if error_code:
        return jsonify(error='upstream_unavailable', message="Discussion provider is unavailable. Please retry."), 502
    return jsonify(message=topic)


MAL_TOPIC_PAGE_SIZE = 100  # MAL's maximum page size for topic posts
MAL_TOPIC_MAX_PAGES = 10
MAL_TOPIC_FETCH_BUDGET_SECONDS = 8  # keep well inside the extension's 25 s request timeout


def _mal_error_code(mal_data):
    error_payload = mal_data.get('error')
    error_code = error_payload.get('error') if isinstance(error_payload, dict) else error_payload
    return str(error_code or 'error')


def fetch_mal_topic(discussion_id):
    """Every post of a MAL topic, paging past MAL's 100-post limit so popular episodes
    show their recent replies, not just the first 100. Returns (error_code, topic)."""
    topic_id = int(discussion_id)
    hit, cached = _TOPIC_CACHE.get(topic_id)
    if hit:
        return None, cached

    url = f"https://api.myanimelist.net/v2/forum/topic/{topic_id}"
    deadline = monotonic() + MAL_TOPIC_FETCH_BUDGET_SECONDS
    topic, posts, seen = None, [], set()
    truncated = True
    for page in range(MAL_TOPIC_MAX_PAGES):
        if page and monotonic() > deadline:
            break
        try:
            with timed('mal_topic_page'):
                response = requests.get(url, params={'limit': MAL_TOPIC_PAGE_SIZE, 'offset': page * MAL_TOPIC_PAGE_SIZE},
                                        headers={'X-MAL-CLIENT-ID': CLIENT_ID}, timeout=10)
            mal_data = response.json()
            if not isinstance(mal_data, dict):
                raise ValueError('Invalid MAL response')
            if 'error' in mal_data:
                current_app.logger.warning("Discussion provider request failed.")
                if page == 0:
                    return _mal_error_code(mal_data), None
                break
            response.raise_for_status()
            data = mal_data.get('data')
            if not isinstance(data, dict):
                raise ValueError('Missing MAL discussion data')
            page_posts = data.get('posts') or []
            if not isinstance(page_posts, list):
                raise ValueError('Invalid MAL posts')
        except (requests.RequestException, ValueError):
            # The first page decides success; a later page failing keeps what we have.
            if page == 0:
                raise
            current_app.logger.warning("Discussion provider request failed.")
            break
        if topic is None:
            topic = dict(data)
        for post in page_posts:
            # Posts deleted mid-walk shift later pages back; skip the repeats.
            key = post.get('id') if isinstance(post, dict) else None
            if key is not None and key in seen:
                continue
            seen.add(key)
            posts.append(post)
        paging = mal_data.get('paging')
        if len(page_posts) < MAL_TOPIC_PAGE_SIZE or (isinstance(paging, dict) and not paging.get('next')):
            truncated = False
            break

    topic['posts'] = posts
    # MAL's detail payload omits the ID; expose the thread we actually resolved.
    topic['id'] = topic_id
    topic['url'] = f'https://myanimelist.net/forum/?topicid={topic_id}'
    if truncated:
        topic['truncated'] = True
    _TOPIC_CACHE.set(topic_id, topic)
    return None, topic
