from flask import Flask, jsonify, current_app
from GetDiscussion import get_discussion
from flask_cors import CORS
from flask import request
app = Flask(__name__)
CORS(app)
@app.route('/')
def home():
    return jsonify(message="Hello from AniNex!")
@app.route('/discussion', methods=['POST'])
def getDiscussionPayload():
    data = request.get_json()
    current_app.logger.info(f"POST Discussion for {data}")
    anime = data.get('anime')
    season = data.get('season')
    episode = data.get('episode')
    return get_discussion(anime=anime, season=season, episode=episode)

if __name__ == '__main__':
    app.run(debug=False)
