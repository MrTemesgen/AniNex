# HTML scrape cache

The active `GetDiscussionV2.py` scraping paths use `scrape_cache.py`.

- Episode-list pages: 600 seconds, keyed by MAL anime ID and pagination offset.
  Requests for different episodes on the same page reuse the HTML.
- Forum HTML fallback: 60 seconds, keyed by topic ID. New posts may take up to
  one minute to appear through the HTML fallback.
- Only HTTP 200 pages with recognizable discussion markup are admitted.
  Exceptions, challenge pages, missing markup, and unsuccessful responses are not cached.
- Upstream `private`, `no-store`, and `no-cache` directives prevent storage.
  `max-age` and `Age` can shorten the application TTL.
- Concurrent requests for one key share an in-flight fetch; no lock is held
  during network I/O. Waiters have a 15-second deadline.
- LRU storage is bounded to 256 pages / 16 MiB of HTML per process, with a
  2 MiB per-page admission limit. Parsing and active network requests use
  additional temporary memory. Expired entries are removed on the next lookup.
- Cached bytes contain public upstream HTML, not incoming request headers or
  extension-user identifiers. Cache keys use numeric provider IDs.

This is per Gunicorn worker and resets on restart/deploy. It does not provide a
shared cross-worker/dyno cache; that would require shared storage such as Redis.
It does not cache API responses or alter the unused legacy `GetDiscussion.py`.
It does not serve stale pages after expiration or provider failure.

Live validation on September 26, 2026: MAL's episode and forum pages both returned
`Cache-Control: no-cache`. Repeated sequential requests therefore fetched fresh
pages and stored no entries. TTL reuse is conditional on upstream cache directives;
do not expect this release to reduce sequential MAL requests under those headers.

Run `python -m unittest test_api test_scrape_cache` to verify behavior.
No new runtime dependency or browser release is required. Deploy the backend
including `scrape_cache.py` to activate the change in production.
