"""Bounded per-worker cache of validated public HTML, with concurrent miss sharing."""
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock
from time import monotonic
import re
import logging


logger = logging.getLogger('aninex.scrape_cache')
# Gunicorn/Flask default levels can hide INFO records. Configure only this logger.
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def log_cache(event, key, **fields):
    # Only a fixed category is logged, never keys, URLs, HTML or exception text.
    kind = key[0] if isinstance(key, tuple) and key and key[0] in ('episodes', 'topic') else 'other'
    logger.info('scrape_cache event=%s kind=%s %s', event, kind,
                ' '.join(f'{name}={value}' for name, value in fields.items()))


class HtmlCache:
    def __init__(self, max_entries=256, max_bytes=16 * 1024 * 1024, clock=monotonic):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.clock = clock
        self.entries = OrderedDict()
        self.pending = {}
        self.size = 0
        self.lock = Lock()

    def get(self, key, ttl, fetch, validate):
        with self.lock:
            now = self.clock()
            for old in list(self.entries):
                if self.entries[old][0] <= now:
                    self.size -= len(self.entries.pop(old)[1])
            if key in self.entries:
                self.entries.move_to_end(key)
                log_cache('hit', key)
                return self.entries[key][1]
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
        if not owner:
            log_cache('shared_wait', key)
            try:
                result = future.result(timeout=15)
                log_cache('shared_success', key)
                return result
            except Exception:
                log_cache('shared_failure', key)
                raise
        log_cache('miss', key)
        stage = 'fetch'
        try:
            response = fetch()
            stage = 'http_status'
            response.raise_for_status()
            stage = 'validation'
            body = response.content
            if response.status_code != 200 or not validate(body):
                raise ValueError('Invalid scrape response')
            # Honor upstream restrictions, and never extend freshness past our TTL.
            directives = response.headers.get('Cache-Control', '').lower()
            blocked = re.search(r'(?:^|,)\s*(no-store|private|no-cache)\b', directives)
            cacheable = not blocked
            age = response.headers.get('Age', '0')
            match = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)', directives)
            if match:
                ttl = min(ttl, max(0, int(match[1]) - (int(age) if age.isdigit() else 0)))
            stage = 'storage'
            stored = False
            with self.lock:
                if cacheable and ttl > 0 and len(body) <= min(self.max_bytes, 2 * 1024 * 1024):
                    while self.entries and (len(self.entries) >= self.max_entries or self.size + len(body) > self.max_bytes):
                        _, (_, removed) = self.entries.popitem(last=False)
                        self.size -= len(removed)
                    if self.max_entries > 0:
                        self.entries[key] = (self.clock() + ttl, body)
                        self.size += len(body)
                        stored = True
            if stored:
                log_cache('store', key, ttl_seconds=ttl, bytes=len(body))
            else:
                reason = (blocked[1] if blocked else 'expired' if ttl <= 0
                          else 'disabled' if self.max_entries <= 0 else 'oversize')
                log_cache('skip', key, reason=reason)
            future.set_result(body)
            return body
        except BaseException as error:
            log_cache('failure', key, stage=stage)
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)


html_cache = HtmlCache()
