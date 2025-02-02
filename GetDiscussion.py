from flask import Flask, jsonify, current_app
import requests
from bs4 import BeautifulSoup
import cydifflib
import re
from dotenv import load_dotenv
import os
CLIENT_ID = os.getenv('CLIENT_ID')

def getDiscussionBaseUrl(discussion_id):
    return f"https://api.myanimelist.net/v2/forum/topic/{discussion_id}?&limit=100"

# Get the discussion for the episode or anime not found message
def get_discussion(anime, episode):
    current_app.logger.info(f"GET Discussion for {anime}-{episode}")
    anime_id = get_anime_id(anime)
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

def get_anime_id(anime):
    BASE_URL = f'https://api.myanimelist.net/v2/anime?q={anime}&limit=10'
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
        closest_title = cydifflib.get_close_matches(anime, titles, n=1)[0]
        idx = titles.index(closest_title)
        current_app.logger.debug(f"Data for anime entered {titles[idx]}")
        return titles_ids[idx][1]
    except Exception as e:
        current_app.logger.error(f"Exception getting anime id for {anime}", e)
        return titles_ids[0][1] if len(titles_ids) > 0 else ""

