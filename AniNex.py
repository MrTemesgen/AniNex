from flask import Flask, jsonify, current_app
from GetDiscussion import get_discussion
from flask_cors import CORS
app = Flask(__name__)
CORS(app)
@app.route('/')
def home():
    return jsonify(message="Hello from AniNex!")

@app.route('/anime/<anime>/episode/<episode>/')
def getDiscussion(anime, episode):
    return get_discussion(anime=anime, episode=episode)

if __name__ == '__main__':
    app.run(debug=False)
