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
    # Add fields=alternative_titles to get English + synonyms
    BASE_URL = f'https://api.myanimelist.net/v2/anime?q={anime+season}&limit=100&fields=alternative_titles'
    data = requests.get(BASE_URL, headers={'X-MAL-CLIENT-ID': CLIENT_ID}).json()

    if 'data' not in data:
        return ""

    titles_ids = []
    for res in data['data']:
        node = res['node']
        alt = node.get('alternative_titles', {})
        # Collect all title variants per anime
        variants = {
            'main': node['title'],          # romaji
            'en': alt.get('en', ''),        # English
            'synonyms': alt.get('synonyms', [])  # other names
        }
        titles_ids.append((node['id'], variants))

    return score_and_pick(anime, titles_ids)


def score_and_pick(user_input, titles_ids):
    """
    titles_ids: list of (mal_id, {'main': str, 'en': str, 'synonyms': [str]})
    """
    normalized_input = user_input.lower().strip()
    best_id = None
    best_score = 0

    for mal_id, variants in titles_ids:
        all_variants = [
            variants['main'],
            variants['en'],
            *variants['synonyms']
        ]
        # Score against every variant, take the best
        for variant in filter(None, all_variants):
            score = SequenceMatcher(None, normalized_input, variant.lower()).ratio()
            if score > best_score:
                best_score = score
                best_id = mal_id

    current_app.logger.info(f"Best match score: {best_score}, id: {best_id}")
    return best_id if best_score > 0.5 else ""

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