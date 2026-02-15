from flask import jsonify, current_app
from bs4 import BeautifulSoup
import requests
import cydifflib
from difflib import SequenceMatcher
import re
import os
import json
CLIENT_ID = os.getenv('CLIENT_ID')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Get the discussion for the episode or anime not found message
def get_discussion(anime, season, episode):
    current_app.logger.info(f"GET Discussion for {anime}-{episode}")
    anime_id = get_anime_id(anime, season)
    if anime_id == "": return jsonify(message = "Anime not found")

    discussion_id = get_discussion_link(anime, anime_id, episode) 
    discussion = requests.get(getDiscussionBaseUrl(discussion_id), headers = {'X-MAL-CLIENT-ID': f'{CLIENT_ID}'}).json()
    data = discussion['data'] if 'data' in discussion else {}
    return jsonify(message = data)

# Get the discussion link for the episode
def get_discussion_link(anime, id, episode):
    try:
        episode = int(episode)

        # Each page has 100 episodes, so we need to offset the page by
        # the greatest multiple of 100 less than the episode number
        offset = ((episode-1)//100)*100 if episode > 100 else 0
        BASE_URL = f'https://myanimelist.net/anime/{id}/{anime}/episode?offset={offset}'
        
        response = requests.get(BASE_URL)
        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table',  {'class': 'episode_list'})

        if table is None: return None

        # Get the row of the episode
        remainder = (episode % 100)
        idx = 100 if remainder == 0 else remainder 
        row = table.find_all('tr')[idx]
        link = row.find_all(['td', 'th'])[-1].find('a')['href']
        return re.findall('=(.*)', link)[0]
    except Exception as e:
        current_app.logger.error(f"Exception getting Discussion link for {anime}-{episode}", e)
        None

def getDiscussionBaseUrl(discussion_id):
    return f"https://api.myanimelist.net/v2/forum/topic/{discussion_id}?&limit=100"

def get_anime_id(anime, season):
    season = ' ' + season if int(season) > 1 else ''
    titles_ids = fetch_mal_titles(anime + season)
    current_app.logger.info(f"Fetched {len(titles_ids)} titles from MAL for query '{anime + season}'")
    if not titles_ids:
        return ""

    result = get_closest_match(anime, season, titles_ids)
    if result:
        return result

    # LLM fallback — re-search MAL with the suggested title
    current_app.logger.warning(f"No confident match for '{anime}', trying LLM fallback")
    llm_title = get_llm_suggestion(anime)
    if not llm_title:
        return titles_ids[0][0] if titles_ids else ""

    fallback_titles_ids = fetch_mal_titles(llm_title + season, limit=20)
    if not fallback_titles_ids:
        return titles_ids[0][0] if titles_ids else ""

    result = get_closest_match(llm_title, season, fallback_titles_ids)
    return result if result else fallback_titles_ids[0][0]


def fetch_mal_titles(query, limit=100):
    """Fetches and structures MAL search results into (id, variants) tuples."""
    BASE_URL = f'https://api.myanimelist.net/v2/anime?q={query}&limit={limit}&fields=alternative_titles'
    data = requests.get(BASE_URL, headers={'X-MAL-CLIENT-ID': CLIENT_ID}).json()

    if 'data' not in data:
        return []

    titles_ids = []
    for res in data['data']:
        node = res['node']
        alt = node.get('alternative_titles', {})
        titles_ids.append((node['id'], {
            'main': node['title'],
            'en': alt.get('en', ''),
            'synonyms': alt.get('synonyms', [])
        }))
    return titles_ids


def get_closest_match(anime, season, titles_ids):
    with open('data.json', encoding='utf-8') as f:
        all_title_groups = [entry['titles'] for entry in json.load(f)]

    candidate_group = find_candidate_group(anime, all_title_groups)
    candidate_names = [t + season for t in candidate_group] if candidate_group else [anime + season]
    current_app.logger.info(f"Candidate names for matching: {candidate_names}")
    best_id, best_score = score_and_pick(candidate_names, titles_ids)

    current_app.logger.info(f"Best match score: {best_score:.2f}, id: {best_id}, for candidates: {candidate_names}")
    return best_id if best_score > 0.6 else None

def compute_score(candidate, variant):
    candidate = candidate.lower().strip()
    variant = variant.lower().strip()

    base_score = SequenceMatcher(None, candidate, variant).ratio()

    # Exact match
    if candidate == variant:
        return 1.0

    # Candidate is a whole word found inside the variant
    # e.g. "Onizuka" inside "Great Teacher Onizuka"
    if re.search(rf'\b{re.escape(candidate)}\b', variant):
        return max(base_score, 0.85)

    # All tokens of the candidate appear in the variant
    # e.g. "teacher onizuka" vs "great teacher onizuka"
    candidate_tokens = set(candidate.split())
    variant_tokens = set(variant.split())
    if candidate_tokens.issubset(variant_tokens):
        return max(base_score, 0.80)

    return base_score

def score_and_pick(candidates, titles_ids):
    """Scores candidate names against all MAL title variants, returns (best_id, best_score)."""
    best_id = None
    best_score = 0

    for candidate in candidates:
        normalized_candidate = candidate.lower().strip()
        for mal_id, variants in titles_ids:
            all_variants = [variants['main'], variants['en'], *variants['synonyms']]
            for variant in filter(None, all_variants):
                score = compute_score(normalized_candidate, variant)
                if score > best_score:
                    current_app.logger.debug(f"New best score {score:.2f} for candidate '{candidate}' vs variant '{variant}' (id: {mal_id})")
                    best_score = score
                    best_id = mal_id

    return best_id, best_score

def find_candidate_group(anime_input, all_title_groups):
    normalized_input = anime_input.lower().strip()
    
    # First try exact match (fast)
    for group in all_title_groups:
        if any(normalized_input == t.lower() for t in group):
            return group
    
    # Then try fuzzy match across all titles in all groups
    all_titles_flat = [(t, group) for group in all_title_groups for t in group]
    best_score = 0
    best_group = None
    
    for title, group in all_titles_flat:
        score = SequenceMatcher(None, normalized_input, title.lower()).ratio()
        if score > best_score and score > 0.75:  # threshold
            best_score = score
            best_group = group
    
    return best_group  # None if nothing good found

def get_llm_suggestion(anime_title):
    """
    Gets an anime title suggestion from an LLM via OpenRouter as a fallback.
    """
    current_app.logger.info(f"Querying LLM for a better title for '{anime_title}'")
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            data=json.dumps({
                "model": "google/gemma-3-4b-it:free",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an anime database assistant. "
        "When given an anime title in any language or format, respond with ONLY "
        "the most common romanized or English title as it appears on MyAnimeList. "
        "No punctuation, no explanation, just the title."
                    },
                    {
                        "role": "user",
                        "content": anime_title
                    }
                ]
            })
        )
        response.raise_for_status()
        data = response.json()
        current_app.logger.info(f"LLM raw response: {data}") # Log the full response
        suggestion = data['choices'][0]['message']['content'].strip()
        current_app.logger.info(f"LLM suggested title: '{suggestion}'")
        return suggestion
    except requests.exceptions.RequestException as e:
        current_app.logger.error(f"LLM API call failed: {e}")
        return None