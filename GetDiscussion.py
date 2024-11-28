from flask import Flask, jsonify, current_app
import requests
from bs4 import BeautifulSoup
import cydifflib
import re
from dotenv import load_dotenv
import os
CLIENT_ID = os.getenv('CLIENT_ID')

def get_discussion(anime, episode):
    current_app.logger.info(f"GET Discussion for {anime}-{episode}")
    anime_id = get_anime_id(anime)[1]
    discussion_id = get_discussion_link(anime, anime_id, episode)
    BASE_URL = f"https://api.myanimelist.net/v2/forum/topic/{discussion_id}?&limit=100"
    discussion = requests.get(BASE_URL, headers = {'X-MAL-CLIENT-ID': f'{CLIENT_ID}'}).json()
    data = discussion['data'] if 'data' in discussion else {}
    return jsonify(message = data)

def get_discussion_link(anime, id, episode):
    try:
        episode = int(episode)
        offset = (episode//100)*100 if episode > 100 else 0
        BASE_URL = f'https://myanimelist.net/anime/{id}/{anime}/episode?offset={offset}'
        
        response = requests.get(BASE_URL)
        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table',  {'class': 'episode_list'})
        
        remainder = (episode % 100)
        idx = 100 if remainder == 0 else remainder 
        row = table.find_all('tr')[idx]
        link = row.find_all(['td', 'th'])[-1].find('a')['href']
        return re.findall('=(.*)', link)[0]
    except Exception as e:
        current_app.logger.error(f"Exception getting Discussion link for {anime}-{episode}", e)
        None

def get_anime_id(anime):
    BASE_URL = f'https://api.myanimelist.net/v2/anime?q={anime}&limit=100'
    data = requests.get(BASE_URL,  headers = {'X-MAL-CLIENT-ID': f'{CLIENT_ID}'}).json()['data']
    titles_ids = []
    titles = []
    for res in data:
        node = res['node']
        titles.append(node['title'])
        titles_ids.append((node['title'], node['id']))
    try:
        closest_title = cydifflib.get_close_matches(anime, titles, n=1)[0]
        idx = titles.index(closest_title)
        return titles_ids[idx]
    except Exception as e:
        current_app.logger.error(f"Exception getting anime id for {anime}", e)
        return titles_ids[0] if len(titles_ids) > 0 else None

