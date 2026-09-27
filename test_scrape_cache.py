import sys
sys.path.insert(0, '.deps')
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import requests
from scrape_cache import ScrapeResultCache
import app
import GetDiscussionV2 as discussion


def response(body=b'valid', headers=None):
    return Mock(status_code=200, content=body, headers=headers or {})


class CacheTests(unittest.TestCase):
    def test_no_cache_page_produces_reusable_result_until_application_ttl(self):
        now = [0]
        cache = ScrapeResultCache(clock=lambda: now[0])
        fetch = Mock(return_value=response(b'<html>discard me</html>',
                                          {'Cache-Control': 'no-cache, max-age=0'}))
        parse = Mock(return_value={'130': '1834644'})
        with self.assertLogs('aninex.scrape_cache', level='INFO') as logs:
            first = cache.get(('episodes', 34572, 100), 600, fetch, parse)
            first['130'] = 'changed by caller'
            now[0] = 599
            self.assertEqual(cache.get(('episodes', 34572, 100), 600, fetch, parse),
                             {'130': '1834644'})
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(parse.call_count, 1)
            self.assertNotIn(b'<html>', next(iter(cache.entries.values()))[1])
            now[0] = 600
            cache.get(('episodes', 34572, 100), 600, fetch, parse)
            self.assertEqual(fetch.call_count, 2)
        self.assertIn('event=hit kind=episodes', '\n'.join(logs.output))

    def test_forum_result_is_a_snapshot_and_expires(self):
        now = [0]
        cache = ScrapeResultCache(clock=lambda: now[0])
        parse = Mock(side_effect=[{'posts': [{'body': 'first'}]}, {'posts': [{'body': 'updated'}]}])
        fetch = Mock(return_value=response(headers={'Cache-Control': 'no-cache'}))
        a = cache.get(('topic', 1), 60, fetch, parse)
        a['posts'][0]['body'] = 'modified'
        self.assertEqual(cache.get(('topic', 1), 60, fetch, parse)['posts'][0]['body'], 'first')
        now[0] = 60
        self.assertEqual(cache.get(('topic', 1), 60, fetch, parse)['posts'][0]['body'], 'updated')
        self.assertEqual(fetch.call_count, 2)

    def test_episode_map_preserves_missing_rows_and_absolute_numbers(self):
        body = b'<table class="episode_list"><tr><th>Episode</th></tr><tr><td>101</td><td></td></tr><tr><td>102</td><td><a href="?topicid=22">Discuss</a></td></tr></table>'
        self.assertEqual(discussion.parse_episode_links(body, 100), {'102': '22'})

    def test_logs_outcomes_without_sensitive_values(self):
        cache = ScrapeResultCache()
        key = ('episodes', 'PRIVATE_TITLE_OR_URL', 100)
        with self.assertLogs('aninex.scrape_cache', level='INFO') as logs:
            cache.get(key, 60, lambda: response(), lambda b: b.decode())
            cache.get(key, 60, lambda: response(), lambda b: b.decode())
            cache.get(('topic', 123), 60, lambda: response(headers={'Cache-Control': 'no-store'}), lambda b: b.decode())
            with self.assertRaises(requests.Timeout):
                cache.get(('topic', 456), 60, Mock(side_effect=requests.Timeout('PRIVATE_EXCEPTION')), lambda b: b.decode())
        output = '\n'.join(logs.output)
        for expected in ['event=miss', 'event=store', 'event=hit',
                         'event=skip kind=topic reason=no-store', 'event=failure kind=topic stage=fetch']:
            self.assertIn(expected, output)
        for secret in ['PRIVATE_TITLE_OR_URL', 'PRIVATE_EXCEPTION', '123', '456']:
            self.assertNotIn(secret, output)

    def test_hit_expiry_and_lru_memory_bounds(self):
        now = [0]
        cache = ScrapeResultCache(max_entries=2, max_bytes=10, clock=lambda: now[0])
        fetch = Mock(return_value=response(b'abc'))
        for key in ['a', 'b', 'a', 'c']:
            cache.get(key, 10, fetch, lambda b: b.decode())
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(list(cache.entries), ['a', 'c'])
        self.assertEqual(cache.size, 10)
        now[0] = 10
        cache.get('a', 10, fetch, lambda b: b.decode())
        self.assertEqual(fetch.call_count, 4)

    def test_failures_and_invalid_pages_are_not_cached(self):
        for bad in [requests.Timeout(), requests.HTTPError(), ValueError()]:
            cache = ScrapeResultCache()
            fetch = Mock(side_effect=[bad, response()])
            with self.assertRaises(type(bad)):
                cache.get('a', 10, fetch, lambda b: b.decode())
            self.assertEqual(cache.get('a', 10, fetch, lambda b: b.decode()), 'valid')
        cache = ScrapeResultCache()
        with self.assertRaises(ValueError):
            cache.get('a', 10, lambda: response(b'challenge'), lambda b: False)
        self.assertFalse(cache.entries)
        self.assertFalse(cache.pending)

    def test_upstream_restrictions_and_size_limits(self):
        for headers in [{'Cache-Control': 'private'}, {'Cache-Control': 'no-store'},
                        {'Cache-Control': 'private, max-age=0'}]:
            cache = ScrapeResultCache()
            fetch = Mock(return_value=response(headers=headers))
            for _ in range(2): cache.get('a', 60, fetch, lambda b: b.decode())
            self.assertEqual(fetch.call_count, 2)
        now = [0]
        cache = ScrapeResultCache(clock=lambda: now[0])
        fetch = Mock(return_value=response(headers={'Cache-Control': 'max-age=10', 'Age': '8'}))
        cache.get('a', 60, fetch, lambda b: b.decode())
        now[0] = 60
        cache.get('a', 60, fetch, lambda b: b.decode())
        self.assertEqual(fetch.call_count, 2)
        cache = ScrapeResultCache(max_bytes=2)
        cache.get('a', 60, lambda: response(), lambda b: b.decode())
        self.assertEqual(cache.size, 0)

    def test_concurrent_requests_share_a_fetch(self):
        cache = ScrapeResultCache()
        entered, release = Event(), Event()
        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return response()
        mocked = Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=5) as pool:
            tasks = [pool.submit(cache.get, 'a', 60, mocked, lambda b: b.decode()) for _ in range(5)]
            self.assertTrue(entered.wait(3))
            release.set()
            self.assertEqual([task.result(3) for task in tasks], ['valid'] * 5)
        self.assertEqual(mocked.call_count, 1)

    def test_episode_page_shared_across_episodes_and_slug_variants(self):
        body = b'<table class="episode_list"><tr><th>Episode</th></tr><tr><td><a href="?topicid=11">1</a></td></tr><tr><td><a href="?topicid=22">2</a></td></tr></table>'
        with app.app.app_context(), patch.object(discussion, 'scrape_result_cache', ScrapeResultCache()), \
                patch.object(discussion.requests, 'get', return_value=response(body, {'Cache-Control': 'no-cache'})) as fetch:
            self.assertEqual(discussion.get_discussion_link('Example', 123, 1), '11')
            self.assertEqual(discussion.get_discussion_link('Other_slug', 123, 2), '22')
            self.assertEqual(fetch.call_count, 1)

    def test_forum_html_reused_and_invalid_pages_retried(self):
        body = b'<title>Example</title><div class="message-wrapper"><a href="/profile/User">User</a><div class="forum-topic-message message">Hello</div></div>'
        with app.app.app_context(), patch.object(discussion, 'scrape_result_cache', ScrapeResultCache()), \
                patch.object(discussion.requests, 'get', side_effect=[response(b'challenge'), response(body, {'Cache-Control': 'no-cache'})]) as fetch:
            self.assertIsNone(discussion.scrape_forum_topic_html(123))
            a = discussion.scrape_forum_topic_html(123)
            b = discussion.scrape_forum_topic_html(123)
            self.assertEqual(a, b)
            self.assertEqual(a['posts'][0]['body'], 'Hello')
            self.assertEqual(fetch.call_count, 2)


if __name__ == '__main__':
    unittest.main()
