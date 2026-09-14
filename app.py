from flask import Flask, jsonify, current_app
from GetDiscussionV2 import get_discussion
from flask_cors import CORS
from flask import request
import re
import requests

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 4096
CORS(app)
@app.route('/')
def home():
    return jsonify(message="Hello from AniNex!")
@app.route('/discussion', methods=['POST'])
def getDiscussionPayload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error='invalid_request', message='Send a JSON object.'), 400
    anime = data.get('anime')
    season = data.get('season')
    episode = data.get('episode')
    if (not isinstance(anime, str) or not anime.strip() or len(anime) > 300
            or isinstance(season, bool) or not isinstance(season, (str, int))
            or not re.fullmatch(r'(?:\d{1,3}|movie|ova|special)', str(season).lower())
            or isinstance(episode, bool) or not isinstance(episode, (str, int))
            or not re.fullmatch(r'[1-9]\d{0,4}', str(episode))):
        return jsonify(error='invalid_request', message='Provide an anime, valid season, and positive episode number.'), 400
    try:
        return get_discussion(anime_query=anime.strip(), season=str(season).lower(), episode=int(episode))
    except requests.Timeout:
        return jsonify(error='upstream_timeout', message='Discussion service timed out. Please retry.'), 504
    except (requests.RequestException, ValueError, TypeError, KeyError):
        current_app.logger.warning('Discussion provider returned an unavailable or invalid response.')
        return jsonify(error='upstream_unavailable', message='Discussion service is unavailable. Please retry.'), 502


if __name__ == '__main__':
    app.run()
