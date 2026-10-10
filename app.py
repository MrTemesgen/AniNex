from flask import Flask, Response, jsonify, current_app
from GetDiscussionV2 import get_discussion
from flask import request
import json
import os
import re
import requests
import request_timing

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 4096
# CORS only for browser extensions. Chrome grants the extension's host permission at
# install, which exempts its background fetch from CORS, but Firefox lets users withhold
# or revoke it, and then the fetch is a CORS request from moz-extension://<random uuid>.
# Websites stay blocked so they can't proxy through this service's MAL/AniList quota.
EXTENSION_ORIGIN = re.compile(r'(?:moz-extension://[0-9a-fA-F-]{36}|chrome-extension://[a-p]{32})')


@app.after_request
def allow_extension_origins(response):
    origin = request.headers.get('Origin', '')
    if EXTENSION_ORIGIN.fullmatch(origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Max-Age'] = '86400'
    response.vary.add('Origin')
    return response


def _is_discussion_request():
    return request.method == 'POST' and request.path == '/discussion'


@app.before_request
def start_discussion_timing():
    if _is_discussion_request():
        request_timing.start()


@app.after_request
def log_discussion_timing(response):
    if _is_discussion_request():
        request_timing.finish(response.status_code)
    return response


@app.teardown_request
def log_failed_discussion_timing(error):
    # after_request is skipped when an unhandled exception becomes a 500.
    if error is not None and _is_discussion_request():
        request_timing.finish(500)


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

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')
_data_body = None


@app.route('/data')
def get_data():
    # data.json is ~5 MB; parse and serialize it once per process, not per request.
    global _data_body
    if _data_body is None:
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            _data_body = json.dumps(json.load(f), separators=(',', ':'))
    return Response(_data_body, mimetype='application/json')

if __name__ == '__main__':
    app.run()
