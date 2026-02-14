from flask import jsonify, current_app
from bs4 import BeautifulSoup
import requests
import cydifflib
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
    season = ' '+season if int(season) > 1 else ''
    current_app.logger.info(f"GET Anime ID for {anime} {CLIENT_ID}")
    BASE_URL = f'https://api.myanimelist.net/v2/anime?q={anime+season}&limit=100'
    data = requests.get(BASE_URL,  headers = {'X-MAL-CLIENT-ID': f'{CLIENT_ID}'}).json()
    print(data)
    if 'data' not in data:
        return ""
    data = data['data']
    titles_ids = []
    titles = []
    for res in data:
        node = res['node']
        titles.append(node['title'])
        titles_ids.append((node['title'], node['id']))
    try:
        closest_title = get_closest_match(anime, season, titles)
        idx = titles.index(closest_title)
        current_app.logger.debug(f"Closest title found: {closest_title} {titles_ids[idx][1]}")
        current_app.logger.debug(f"Data for anime entered {titles[idx]}")
        return titles_ids[idx][1]
    except Exception as e:
        current_app.logger.error(f"Exception getting anime id for {anime}", e)
        return titles_ids[0][1] if len(titles_ids) > 0 else ""

def get_closest_match(anime, season, titles):
    # Load data.json and collect all names for the searched anime
    with open('data.json', encoding='utf-8') as f:
        anime_data = json.load(f)
    all_title_groups = [anime_entry['titles'] for anime_entry in anime_data]
    matching_title_group = [title_group for title_group in all_title_groups if any(anime.lower() == title.lower() for title in title_group)]
    # Find all names that closely match the searched anime
    candidate_names = [title for group in matching_title_group for title in group]

    best_match = None
    best_score = 0
    current_app.logger.info(f"Candidate names for '{anime}' from data.json: {candidate_names}")
    if candidate_names:
        current_app.logger.info(f"Found candidate names in data.json for {anime}")
        # For each candidate name, find the closest match in the titles list
        for candidate in candidate_names:
            candidate = candidate+season
            matches = cydifflib.get_close_matches(candidate, titles, n=1, cutoff=0.0)
            if matches:
                # Use SequenceMatcher to get a similarity ratio
                score = cydifflib.SequenceMatcher(None, candidate, matches[0]).ratio()
                if score > best_score:
                    best_score = score
                    best_match = matches[0]
        if best_match:
            return best_match

    # Fallback: If no match found in local data, try LLM suggestion
    current_app.logger.warning(f"No definitive match found for '{anime}' in local data. Trying LLM fallback.")
    llm_title = get_llm_suggestion(anime)
    if llm_title:
        llm_title_with_season = llm_title + season
        matches = cydifflib.get_close_matches(llm_title_with_season, titles, n=1, cutoff=0.6)
        if matches:
            current_app.logger.info(f"Found close match for LLM suggestion: '{matches[0]}'")
            return matches[0]

    # Final fallback: if LLM fails or no close match, return the first result from API
    current_app.logger.error(f"No close match found for '{anime}' using any method. Returning first available title.")
    return titles[0] if titles else ""


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
                "model": "openrouter/free",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an anime title expert. Given the user input, provide the most common or official English title for the anime that is most likely to be found on MyAnimeList.net. Return only the anime title itself and nothing else."
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