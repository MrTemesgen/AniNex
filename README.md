# AniNex
API for getting anime episode comments from MAL
 
POST url: https://aninex.onrender.com/discussion
### Request Body

Send a JSON payload with the following fields:

- `anime`: (string) The name of the anime.
- `season`: (string or int) The season number (e.g., "1", "2").
- `episode`: (string or int) The episode number.

**Example:**
```json
{
  "anime": "Attack on Titan",
  "season": "1",
  "episode": "5"
}
