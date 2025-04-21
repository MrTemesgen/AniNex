from flask import jsonify, current_app
from bs4 import BeautifulSoup
import requests
import cydifflib
import re
import os
import json
CLIENT_ID = os.getenv('CLIENT_ID')

def getDiscussionBaseUrl(discussion_id):
    return f"https://api.myanimelist.net/v2/forum/topic/{discussion_id}?&limit=100"

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

def get_anime_id(anime, season):
    season = ' '+season if int(season) > 1 else ''
    current_app.logger.info(f"GET Anime ID for {anime}")
    BASE_URL = f'https://api.myanimelist.net/v2/anime?q={anime+season}&limit=100'
    data = requests.get(BASE_URL,  headers = {'X-MAL-CLIENT-ID': f'{CLIENT_ID}'}).json()

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

    if not candidate_names:
        current_app.logger.error(f"No matching title group found for {anime}")
    # For each candidate name, find the closest match in the titles list
    best_match = None
    best_score = 0
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
    else:
        #no match found, return the first title in the list
        current_app.logger.error(f"No close match found for {anime} in titles")
        return titles[0] if len(titles) > 0 else ""