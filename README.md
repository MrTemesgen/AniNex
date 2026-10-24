# AniNex API

Backend for the AniNex browser extension (Chrome and
Firefox), which shows MyAnimeList (MAL) episode discussion threads on Crunchyroll episode pages.

Given the show title, season and episode number that Crunchyroll displays, the API finds the
matching MAL anime entry and episode discussion thread, then returns the thread's posts.

Base URL: `https://aninex-60a22ea8db43.herokuapp.com`

## How a lookup works

1. **Find the show.** The title is searched on [AniList](https://anilist.co); the candidate whose
   title actually matches is preferred over AniList's first fuzzy hit, which can be a spin-off.
2. **Find the season.** AniList's prequel/sequel relations are walked to the Nth TV season.
   This handles Crunchyroll's numbering quirks:
   - one season that MAL splits into parts or cours,
   - absolute episode numbers carried across seasons (One Piece, Black Clover, SPY x FAMILY),
   - OVA, special and movie entries (`season` = `ova`, `special` or `movie`).
3. **Find the thread.** The episode's discussion link is read from MAL's episode page, falling
   back to MAL's forum search.
4. **Fetch the posts.** Up to 1,000 posts are read from MAL's API, several pages in parallel
   within an 8-second budget. Pages MAL rejects are split to skip only the unreadable post,
   and an HTML scrape is the last resort.

If AniList is unavailable (as during its October 2026 outage), lookups fall back to MAL's own
search and use MAL's prequel/sequel links to map Crunchyroll episode numbers onto MAL entries.

## Endpoints

All endpoints except `/` and `/data` take a JSON body via `POST`. Browsers can only call the API
from extension origins (`chrome-extension://`, `moz-extension://`); other websites are blocked by CORS.

### `POST /discussion`

| Field | Type | Required | Notes |
|---|---|---|---|
| `anime` | string | yes | Show title as Crunchyroll displays it (1-300 characters) |
| `season` | string or int | yes | Season number (`"1"`-`"999"`), or `"ova"`, `"special"`, `"movie"` |
| `episode` | string or int | yes | Episode number as Crunchyroll displays it (1-99999) |
| `mal_id` | string or int | no | Exact MAL anime ID, used when the user picked the show by hand |

```bash
curl -X POST https://aninex-60a22ea8db43.herokuapp.com/discussion \
  -H "Content-Type: application/json" \
  -d '{"anime": "Attack on Titan", "season": "1", "episode": "5"}'
```

A found thread comes back under `message`:

```json
{
  "message": {
    "id": 1234567,
    "title": "Shingeki no Kyojin Episode 5 Discussion",
    "url": "https://myanimelist.net/forum/?topicid=1234567",
    "posts": [
      {
        "id": 1,
        "created_at": "2013-05-05T12:00:00+00:00",
        "created_by": {"name": "username", "forum_avator": "https://..."},
        "body": "post text with MAL BBCode"
      }
    ],
    "truncated": true
  }
}
```

`truncated` is present only when the thread had more posts than could be fetched in time.
When nothing is found, `message` is a string such as
`"Discussion thread not found on MAL. The episode may not have aired yet."`.

### `POST /search`

Show search for the extension's show picker, used when Crunchyroll gives no usable title or the
automatic match is wrong. POST keeps search text out of URL access logs.

```json
{"q": "frieren"}
```

`q` must be 2-100 characters. The response lists up to 12 non-adult entries:

```json
{
  "results": [
    {
      "mal_id": 52991,
      "title": "Frieren: Beyond Journey's End",
      "romaji": "Sousou no Frieren",
      "native": "葬送のフリーレン",
      "format": "TV",
      "episodes": 28,
      "year": 2023,
      "cover": "https://s4.anilist.co/file/..."
    }
  ]
}
```

### Other routes

- `GET /`: health check, returns `{"message": "Hello from AniNex!"}`.
- `GET /data`: legacy static MAL catalog dump (`data.json`, about 5 MB). The extension doesn't use it.

### Errors

| Status | `error` | Meaning |
|---|---|---|
| 400 | `invalid_request` | Missing or malformed fields |
| 429 | `rate_limited` | Over the per-IP limit (60/min for `/discussion`, 90/min for `/search`); see `Retry-After` |
| 502 | `upstream_unavailable` | AniList or MAL returned an error or unreadable data |
| 504 | `upstream_timeout` | AniList or MAL timed out |

## Caching

Each process keeps in-memory TTL caches, lost on restart or deploy:

| Cache | Lifetime |
|---|---|
| AniList search, season trees, picker results | 1 hour |
| Resolved (title, season, episode) → MAL entry | 6 hours |
| (MAL entry, episode) → thread ID; MAL entry details | 24 hours |
| Assembled thread posts | 60 seconds |

Lookups and picker searches answered by the MAL fallback (for example during an AniList
outage) aren't cached, so normal matching resumes as soon as AniList recovers.
Scraped MAL pages have their own cache; see [SCRAPE_CACHE.md](SCRAPE_CACHE.md).

## Running locally

Requires Python 3.12 and a MAL API client ID
([create one here](https://myanimelist.net/apiconfig)).

```bash
pip install -r requirements.txt
```

```bash
export CLIENT_ID=your_mal_client_id
```

```bash
flask --app app run
```

Run the tests (no network access needed; upstream calls are mocked):

```bash
python -m unittest test_api test_scrape_cache
```

## Deployment

Runs on Heroku with one gunicorn worker and 16 threads (see `Procfile`), so the in-memory
caches and rate limiter are shared by all requests. Set `CLIENT_ID` as a config var:

```bash
heroku config:set CLIENT_ID=your_mal_client_id -a aninex
```

Each `/discussion` request logs one `discussion_timing` line with per-stage durations
(AniList, MAL episode page, forum search, topic pages) and the status code. Logs contain no
show titles, search queries or other user input.

## Privacy

The API stores nothing about users. Client IP addresses are held in memory only for rate
limiting, and Heroku's router logs requests as usual. See the extension's privacy policy for details.
