import sys
sys.path.insert(0, '.deps')
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import requests
from scrape_cache import HtmlCache
import app
import GetDiscussionV2 as discussion


def response(body=b'valid', headers=None):
    return Mock(status_code=200, content=body, headers=headers or {})


class CacheTests(unittest.TestCase):
    def test_hit_expiry_and_lru_memory_bounds(self):
        now = [0]
        cache = HtmlCache(max_entries=2, max_bytes=6, clock=lambda: now[0])
        fetch = Mock(return_value=response(b'abc'))
        for key in ['a', 'b', 'a', 'c']:
            cache.get(key, 10, fetch, bool)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(list(cache.entries), ['a', 'c'])
        self.assertEqual(cache.size, 6)
        now[0] = 10
        cache.get('a', 10, fetch, bool)
        self.assertEqual(fetch.call_count, 4)

    def test_failures_and_invalid_pages_are_not_cached(self):
        for bad in [requests.Timeout(), requests.HTTPError(), ValueError()]:
            cache = HtmlCache()
            fetch = Mock(side_effect=[bad, response()])
            with self.assertRaises(type(bad)):
                cache.get('a', 10, fetch, bool)
            self.assertEqual(cache.get('a', 10, fetch, bool), b'valid')
        cache = HtmlCache()
        with self.assertRaises(ValueError):
            cache.get('a', 10, lambda: response(b'challenge'), lambda b: False)
        self.assertFalse(cache.entries)
        self.assertFalse(cache.pending)

    def test_upstream_restrictions_and_size_limits(self):
        for headers in [{'Cache-Control': 'private'}, {'Cache-Control': 'no-store'},
                        {'Cache-Control': 'no-cache'}, {'Cache-Control': 'max-age=0'}]:
            cache = HtmlCache()
            fetch = Mock(return_value=response(headers=headers))
            for _ in range(2): cache.get('a', 60, fetch, bool)
            self.assertEqual(fetch.call_count, 2)
        now = [0]
        cache = HtmlCache(clock=lambda: now[0])
        fetch = Mock(return_value=response(headers={'Cache-Control': 'max-age=10', 'Age': '8'}))
        cache.get('a', 60, fetch, bool)
        now[0] = 2
        cache.get('a', 60, fetch, bool)
        self.assertEqual(fetch.call_count, 2)
        cache = HtmlCache(max_bytes=2)
        cache.get('a', 60, lambda: response(), bool)
        self.assertEqual(cache.size, 0)

    def test_concurrent_requests_share_a_fetch(self):
        cache = HtmlCache()
        entered, release = Event(), Event()
        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return response()
        mocked = Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=5) as pool:
            tasks = [pool.submit(cache.get, 'a', 60, mocked, bool) for _ in range(5)]
            self.assertTrue(entered.wait(3))
            release.set()
            self.assertEqual([task.result(3) for task in tasks], [b'valid'] * 5)
        self.assertEqual(mocked.call_count, 1)

    def test_episode_page_shared_across_episodes_and_slug_variants(self):
        body = b'<table class="episode_list"><tr><th>Episode</th></tr><tr><td><a href="?topicid=11">1</a></td></tr><tr><td><a href="?topicid=22">2</a></td></tr></table>'
        with app.app.app_context(), patch.object(discussion, 'html_cache', HtmlCache()), \
                patch.object(discussion.requests, 'get', return_value=response(body)) as fetch:
            self.assertEqual(discussion.get_discussion_link('Example', 123, 1), '11')
            self.assertEqual(discussion.get_discussion_link('Other_slug', 123, 2), '22')
            self.assertEqual(fetch.call_count, 1)

    def test_forum_html_reused_and_invalid_pages_retried(self):
        body = b'<title>Example</title><div class="message-wrapper"><a href="/profile/User">User</a><div class="forum-topic-message message">Hello</div></div>'
        with app.app.app_context(), patch.object(discussion, 'html_cache', HtmlCache()), \
                patch.object(discussion.requests, 'get', side_effect=[response(b'challenge'), response(body)]) as fetch:
            self.assertIsNone(discussion.scrape_forum_topic_html(123))
            a = discussion.scrape_forum_topic_html(123)
            b = discussion.scrape_forum_topic_html(123)
            self.assertEqual(a, b)
            self.assertEqual(a['posts'][0]['body'], 'Hello')
            self.assertEqual(fetch.call_count, 2)


if __name__ == '__main__':
    unittest.main()
