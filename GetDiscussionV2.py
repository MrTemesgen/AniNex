import requests
from flask import jsonify, current_app
from bs4 import BeautifulSoup
import re
import os

ANILIST_API_URL = 'https://graphql.anilist.co'
CLIENT_ID = os.getenv('CLIENT_ID')

# ---------------------------------------------------------
# 1. ANILIST GRAPHQL QUERY
# ---------------------------------------------------------

def fetch_season_tree(search_term):
    # Grabs the exact season searched, plus a few levels of sequels for split-cours
    graphql_query = """
    fragment animeFields on Media {
      idMal
      episodes
      format
      title { romaji english }
      nextAiringEpisode { episode }
    }

    query ($search: String) {
      Media (search: $search, type: ANIME, sort: [SEARCH_MATCH, START_DATE]) {
        ...animeFields
        relations {
          edges {
            relationType
            node {
              ...animeFields
              relations {
                edges {
                  relationType
                  node {
                    ...animeFields
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    res = requests.post(ANILIST_API_URL, json={'query': graphql_query, 'variables': {'search': search_term}})
    return res.json().get('data', {}).get('Media')

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
        url = f"https://api.myanimelist.net/v2/anime"
        params = {'q': search_term, 'limit': 1}
        response = requests.get(url, params=params, headers={'X-MAL-CLIENT-ID': CLIENT_ID})
        
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
        # Absolute fallback if AniList returns nothing
        return fallback_mal_search(anime_query, season), target_ep, search_term.replace(' ', '_')

    accumulated_eps = 0
    
    # 3. Walk forward ONLY from the start of the requested season
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
        if fmt in ['MOVIE', 'OVA', 'SPECIAL'] and season_str not in ['0', 'movie', 'ova', 'special']:
            pass 
        else:
            # Check if the requested episode falls in this part of the split-cour
            if target_ep <= (accumulated_eps + ep_count):
                local_ep = target_ep - accumulated_eps
                
                if not mal_id:
                    mal_id = fallback_mal_search(title, season)
                    
                return mal_id, local_ep, slug
            
            # If the episode is larger than this part, add to the pile and check the sequel
            accumulated_eps += ep_count
            
        # Move to the sequel
        next_node = None
        if current_node.get('relations') and current_node['relations'].get('edges'):
            for edge in current_node['relations']['edges']:
                if edge['relationType'] == 'SEQUEL':
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
        
        response = requests.get(BASE_URL)
        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table',  {'class': 'episode_list'})

        if table is None: return None

        remainder = (episode % 100)
        idx = 100 if remainder == 0 else remainder 
        row = table.find_all('tr')[idx]
        link = row.find_all(['td', 'th'])[-1].find('a')['href']
        return re.findall('=(.*)', link)[0]
    except Exception as e:
        current_app.logger.error(f"Scraper failed for {anime}-{episode}: {e}")
        return None

def fallback_forum_search(clean_title, local_ep):
    # If the episode just aired and the HTML table isn't updated, search the forum directly
    query = f"{clean_title} Episode {local_ep} Discussion"
    try:
        url = "https://api.myanimelist.net/v2/forum/topics"
        params = {'q': query, 'limit': 5}
        response = requests.get(url, params=params, headers={'X-MAL-CLIENT-ID': CLIENT_ID})
        
        if response.status_code == 200:
            topics = response.json().get('data', [])
            for topic in topics:
                # Basic sanity check: ensure the episode number is actually in the title
                if str(local_ep) in topic.get('title', ''):
                    return topic.get('id')
    except Exception as e:
        current_app.logger.error(f"Fallback forum search failed: {e}")
    return None

# ---------------------------------------------------------
# 4. MAIN ENDPOINT
# ---------------------------------------------------------

def get_discussion(anime_query, season, episode):
    # Use our hybrid split-cour resolver
    mal_id, local_ep, anime_slug = resolve_mal_id_with_split_cour(anime_query, season, episode)

    if not mal_id:
        return jsonify(message="Could not find a matching MAL ID for this season.")

    # 1. Scrape the discussion ID
    discussion_id = get_discussion_link(anime_slug, mal_id, local_ep)
    
    # 2. Direct forum search fallback
    if not discussion_id:
        current_app.logger.info("Scraping failed. Attempting direct forum search...")
        clean_title = anime_slug.replace('_', ' ') if anime_slug else anime_query
        discussion_id = fallback_forum_search(clean_title, local_ep)

    if not discussion_id:
        return jsonify(message="Discussion thread not found on MAL. The episode may not have aired yet.")

    # Fetch the forum posts
    mal_forum_url = f"https://api.myanimelist.net/v2/forum/topic/{discussion_id}?limit=100"
    response = requests.get(mal_forum_url, headers={'X-MAL-CLIENT-ID': CLIENT_ID})
    
    return jsonify(message=response.json().get('data', {}))