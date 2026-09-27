# Parsed scrape-result cache

The active `GetDiscussionV2.py` scrapers use `ScrapeResultCache` in
`scrape_cache.py`. This memoizes derived application data, not HTTP responses.
Raw HTML, response headers, and BeautifulSoup objects are not retained.

## Retention and freshness

- Episode-to-topic mappings: 600 seconds, keyed by MAL anime ID and page offset.
  Different episodes on the same page share a mapping. Missing discussion links
  are not stored as successful entries; the normal forum API fallback still runs.
- Parsed forum topics/posts: 60 seconds, keyed by topic ID. New or edited posts
  may take up to a minute to appear through this fallback.
- These explicit application TTLs apply even when page responses carry
  `no-cache` or `max-age=0`. This deliberately changes the earlier implementation,
  which treated page HTTP freshness directives as a reason to disable caching.
  It does not revalidate the upstream page during the application TTL.
- `private` and `no-store` still prevent retention as a conservative safeguard.
- Only successfully parsed nonempty results from HTTP 200 responses are admitted.
  Failures, challenge pages without usable content, and empty parse results are
  retried on subsequent requests. Expired results are not served on errors.

## Bounds and concurrency

- LRU: at most 256 results / 16 MiB of serialized JSON per worker, with a
  2 MiB admission limit per result. Active fetches, parsing, and returned objects
  require additional temporary memory. Expired entries are removed on lookup.
- Concurrent requests for the same key share a fetch/parse operation. Waiters
  have a 15-second deadline; the network fetch has a 10-second requests timeout.
- Serialization gives each caller an independent copy, protecting shared results.
- Keys use public provider IDs. No extension-user identifiers or request headers
  are cached. Forum snapshots include public authors and posts as displayed.
- State is per Gunicorn worker and is lost on restart/deploy. It is not shared
  across dynos. API calls (AniList and MAL forum API) remain uncached, so a scrape
  cache hit does not imply that the entire discussion request avoids network I/O.
- The unused legacy `GetDiscussion.py` is unchanged. No new dependency is needed.

## Heroku logs

Search application logs for `scrape_cache`. INFO records go to stderr, captured
by Heroku independently of Flask's default warning level. For a repeated request
within the TTL on the same worker, expect:

```text
scrape_cache event=miss kind=episodes
scrape_cache event=store kind=episodes ttl_seconds=600 bytes=1121
scrape_cache event=hit kind=episodes
```

`shared_wait` / `shared_success` indicate reuse of an in-flight fetch;
`shared_failure` reports a wait failure. `failure` includes only a fixed stage:
`fetch`, `http_status`, `parse`, or `storage`. Skips report `private`, `no-store`,
`expired`, `disabled`, or `oversize`. Logs exclude keys, IDs, URLs, titles, HTML,
user information, and exception messages. These are worker-level diagnostics.

## Validation

Run `python -m unittest test_api test_scrape_cache` (23 tests).
Local patched code tested against live MAL: two reads of Black Clover's episode
page and two reads of Jujutsu Kaisen's forum topic produced two network fetches
in total, with `miss`, `store`, then `hit` for each category. Both upstream pages
returned `Cache-Control: no-cache`. Correct episode 130 mapping and 50 forum
posts were preserved. This verifies local behavior, not the deployed service.

Deploy all changed backend files before checking Heroku for the new hit behavior.
No extension release is required.
