import sys
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, '.deps')
import requests
import app as api
import GetDiscussionV2 as discussion


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        discussion.clear_caches()

    def test_invalid_input_does_not_call_provider(self):
        bad = [None, [], {}, {'anime': 'X', 'season': '1', 'episode': '0'},
               {'anime': 'X', 'season': True, 'episode': 1},
               {'anime': 'X', 'season': 1, 'episode': '5abc'},
               {'anime': ' ', 'season': 1, 'episode': 1}]
        with patch.object(api, 'get_discussion') as provider:
            for payload in bad:
                with self.subTest(payload=payload):
                    response = self.client.post('/discussion', json=payload)
                    self.assertEqual(response.status_code, 400)
            provider.assert_not_called()

    def test_normalizes_valid_input(self):
        with patch.object(api, 'get_discussion', return_value={'message': {}}) as provider:
            response = self.client.post('/discussion', json={'anime': ' Example ', 'season': 'MOVIE', 'episode': '1'})
            self.assertEqual(response.status_code, 200)
            provider.assert_called_once_with(anime_query='Example', season='movie', episode=1)

    def test_provider_failures_have_stable_responses(self):
        for error, status in [(requests.Timeout(), 504), (requests.ConnectionError(), 502), (ValueError(), 502)]:
            with patch.object(api, 'get_discussion', side_effect=error):
                response = self.client.post('/discussion', json={'anime': 'Example', 'season': 1, 'episode': 1})
                self.assertEqual(response.status_code, status)
                self.assertIn('error', response.json)

    def test_large_request_is_rejected(self):
        response = self.client.post('/discussion', json={'anime': 'x' * 5000})
        self.assertEqual(response.status_code, 413)

    def test_forum_fallback_matches_whole_episode(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': [
            {'id': 12, 'title': 'Example Episode 12 Discussion'},
            {'id': 2, 'title': 'Example Episode 2 Discussion'}]}
        with api.app.app_context(), patch.object(discussion.requests, 'get', return_value=response):
            self.assertEqual(discussion.fallback_forum_search('Example', 2), 2)

    def test_anilist_http_failure_is_not_treated_as_not_found(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError()
        with patch.object(discussion.requests, 'post', return_value=response):
            with self.assertRaises(requests.HTTPError):
                discussion.fetch_season_tree('Example')

    def test_malformed_anilist_data_is_controlled(self):
        for payload in [{'data': ['bad']}, {'data': {'Media': ['bad']}}]:
            response = Mock()
            response.json.return_value = payload
            with patch.object(discussion.requests, 'post', return_value=response):
                with self.assertRaises(ValueError):
                    discussion.fetch_season_tree('Example')

    def test_anilist_explicit_not_found_is_a_catalog_miss(self):
        response = Mock(status_code=404)
        response.json.return_value = {'errors': [{'status': 404, 'message': 'Not Found.'}],
                                      'data': {'Media': None}}
        with patch.object(discussion.requests, 'post', return_value=response):
            self.assertIsNone(discussion.fetch_season_tree('Black Clover Season 3'))

    def test_anilist_other_errors_are_not_catalog_misses(self):
        for status in [429, 500]:
            response = Mock(status_code=status)
            response.json.return_value = {'errors': [{'status': status}], 'data': None}
            response.raise_for_status.side_effect = requests.HTTPError()
            with patch.object(discussion.requests, 'post', return_value=response):
                with self.assertRaises(requests.HTTPError):
                    discussion.fetch_season_tree('Example')

    def test_streaming_season_uses_base_catalog_absolute_episode(self):
        node = {'idMal': 34572, 'format': 'TV', 'episodes': 170,
                'title': {'romaji': 'Black Clover', 'english': 'Black Clover'}}
        with api.app.app_context(), patch.object(discussion, 'fetch_season_tree', side_effect=[None, node]) as fetch:
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Black Clover', '3', 130),
                             (34572, 130, 'Black_Clover'))
            self.assertEqual([call.args[0] for call in fetch.call_args_list],
                             ['Black Clover Season 3', 'Black Clover'])

    def test_base_fallback_rejects_wrong_title_format_or_episode_range(self):
        base = {'idMal': 34572, 'format': 'TV', 'episodes': 170,
                'title': {'romaji': 'Black Clover'}}
        for changes in [{'episodes': 12}, {'format': 'MOVIE'},
                        {'title': {'romaji': 'Black Clover Special'}}, {'episodes': None}]:
            with self.subTest(changes=changes), api.app.app_context(), \
                    patch.object(discussion, 'fetch_season_tree', side_effect=[None, dict(base, **changes)]), \
                    patch.object(discussion, 'fallback_mal_search', return_value=None):
                self.assertIsNone(discussion.resolve_mal_id_with_split_cour('Black Clover', '3', 130)[0])

    def test_existing_season_keeps_its_own_episode_mapping(self):
        node = {'idMal': 123, 'format': 'TV', 'episodes': 12,
                'title': {'romaji': 'Example Season 3'}}
        with api.app.app_context(), patch.object(discussion, 'fetch_season_tree', return_value=node) as fetch:
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Example', '3', 5),
                             (123, 5, 'Example_Season_3'))
            fetch.assert_called_once_with('Example Season 3')

    def test_api_discussion_exposes_resolved_thread_link(self):
        provider = Mock()
        provider.json.return_value = {'data': {'title': 'Episode discussion', 'posts': []}}
        with api.app.app_context(), \
                patch.object(discussion, 'resolve_mal_id_with_split_cour', return_value=(123, 1, 'Example')), \
                patch.object(discussion, 'get_discussion_link', return_value='1987654'), \
                patch.object(discussion.requests, 'get', return_value=provider):
            payload = discussion.get_discussion('Example', '1', 1).get_json()['message']
        self.assertEqual(payload['id'], 1987654)
        self.assertEqual(payload['url'], 'https://myanimelist.net/forum/?topicid=1987654')
        self.assertEqual(payload['title'], 'Episode discussion')
        self.assertEqual(payload['posts'], [])

    def test_search_prefers_exact_title_over_spin_off(self):
        candidates = [
            {'id': 170068, 'format': 'ONA', 'popularity': 11072,
             'title': {'romaji': 'Sousou no Frieren: ●● no Mahou', 'english': None}},
            {'id': 154587, 'format': 'TV', 'popularity': 487706,
             'title': {'romaji': 'Sousou no Frieren', 'english': 'Frieren: Beyond Journey’s End'}},
            {'id': 182255, 'format': 'TV', 'popularity': 229674,
             'title': {'romaji': 'Sousou no Frieren 2nd Season',
                       'english': 'Frieren: Beyond Journey’s End Season 2'}}]
        self.assertEqual(discussion.choose_search_candidate("Frieren: Beyond Journey's End", candidates)['id'], 154587)
        self.assertEqual(discussion.choose_search_candidate(
            "Frieren: Beyond Journey's End Season 2", candidates)['id'], 182255)
        # Without an exact title match, AniList's own ranking is kept.
        self.assertEqual(discussion.choose_search_candidate('Frieren', candidates)['id'], 170068)
        self.assertEqual(discussion.choose_search_candidate(
            'SPY x FAMILY', [{'id': 1, 'format': 'TV', 'title': {'romaji': 'SPY×FAMILY'}}])['id'], 1)

    def test_anilist_lookups_are_cached(self):
        search = Mock(status_code=200)
        search.json.return_value = {'data': {'Page': {'media': [
            {'id': 7, 'format': 'TV', 'title': {'romaji': 'Example'}}]}}}
        tree = Mock(status_code=200)
        tree.json.return_value = {'data': {'Media': {'id': 7, 'idMal': 70, 'format': 'TV'}}}
        with patch.object(discussion.requests, 'post', side_effect=[search, tree]) as post:
            first = discussion.fetch_season_tree('Example')
            second = discussion.fetch_season_tree('Example')
        self.assertEqual(first['idMal'], 70)
        self.assertEqual(second, first)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs['json']['variables'], {'id': 7})

    def test_relation_fetch_failure_is_not_a_short_chain(self):
        response = Mock(status_code=429)
        response.json.return_value = {'errors': [{'status': 429}], 'data': None}
        response.raise_for_status.side_effect = requests.HTTPError()
        with patch.object(discussion.requests, 'post', return_value=response):
            with self.assertRaises(requests.HTTPError):
                discussion.fetch_node_relations(5)

    @staticmethod
    def _chain(*entries):
        """Link (id, format, episodes, romaji) entries into a nested SEQUEL tree."""
        node = None
        for anilist_id, fmt, episodes, romaji in reversed(entries):
            current = {'id': anilist_id, 'idMal': anilist_id * 10, 'format': fmt, 'episodes': episodes,
                       'title': {'romaji': romaji, 'english': None}, 'relations': {'edges': []}}
            if node:
                current['relations']['edges'].append({'relationType': 'SEQUEL', 'node': node})
            node = current
        return node

    def test_arc_named_seasons_are_counted_along_the_sequel_chain(self):
        base = self._chain((1, 'TV', 26, 'Kimetsu no Yaiba'),
                           (2, 'MOVIE', 1, 'Kimetsu no Yaiba Movie: Mugen Ressha-hen'),
                           (3, 'TV', 7, 'Kimetsu no Yaiba: Mugen Ressha-hen (TV)'),
                           (4, 'TV', 11, 'Kimetsu no Yaiba: Yuukaku-hen'),
                           (5, 'TV', 11, 'Kimetsu no Yaiba: Katanakaji no Sato-hen'))
        base['title']['english'] = 'Demon Slayer: Kimetsu no Yaiba'
        with patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.find_nth_tv_season(base, 4)['id'], 5)
            self.assertIsNone(discussion.find_nth_tv_season(base, 6))
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree', side_effect=[None, base]), \
                patch.object(discussion, 'fetch_media_tree', return_value=None), \
                patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Demon Slayer: Kimetsu no Yaiba', '4', 1),
                             (50, 1, 'Kimetsu_no_Yaiba:_Katanakaji_no_Sato-hen'))
        # A season past the end of the chain must not land on season 1's episodes.
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree', side_effect=[None, base]), \
                patch.object(discussion, 'fetch_node_relations', return_value=None), \
                patch.object(discussion, 'fallback_mal_search', return_value=None):
            self.assertIsNone(discussion.resolve_mal_id_with_split_cour('Demon Slayer: Kimetsu no Yaiba', '6', 2)[0])

    def test_later_cours_do_not_count_as_new_seasons(self):
        base = self._chain((1, 'TV', 12, 'Dr. STONE'),
                           (2, 'TV', 11, 'Dr. STONE: STONE WARS'),
                           (3, 'TV', 11, 'Dr. STONE: NEW WORLD'),
                           (4, 'TV', 11, 'Dr. STONE: NEW WORLD Part 2'),
                           (5, 'TV', 12, 'Dr. STONE: SCIENCE FUTURE'))
        with patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.find_nth_tv_season(base, 3)['id'], 3)
            self.assertEqual(discussion.find_nth_tv_season(base, 4)['id'], 5)

    def _mal_page(self, posts, has_next):
        page = Mock(status_code=200)
        page.json.return_value = {'data': {'title': 'Episode discussion', 'posts': posts},
                                  'paging': {'next': 'more'} if has_next else {}}
        return page

    def test_mal_topic_pages_past_the_first_hundred_posts(self):
        first = [{'id': n} for n in range(100)]
        second = [{'id': n} for n in range(99, 130)]  # one repeat from a deletion mid-walk
        with api.app.app_context(), patch.object(discussion.requests, 'get', side_effect=[
                self._mal_page(first, True), self._mal_page(second, False)]) as get:
            error, topic = discussion.fetch_mal_topic('42')
        self.assertIsNone(error)
        self.assertEqual([post['id'] for post in topic['posts']], list(range(130)))
        self.assertNotIn('truncated', topic)
        self.assertEqual([call.kwargs['params']['offset'] for call in get.call_args_list], [0, 100])

    def test_mal_topic_keeps_first_pages_when_a_later_page_fails(self):
        with api.app.app_context(), patch.object(discussion.requests, 'get', side_effect=[
                self._mal_page([{'id': n} for n in range(100)], True), requests.Timeout()]):
            error, topic = discussion.fetch_mal_topic('42')
        self.assertIsNone(error)
        self.assertEqual(len(topic['posts']), 100)
        self.assertTrue(topic['truncated'])

    def test_missing_mal_topic_is_not_reported_as_an_outage(self):
        missing = Mock(status_code=404)
        missing.json.return_value = {'error': 'not_found', 'message': ''}
        with api.app.app_context(), \
                patch.object(discussion, 'resolve_mal_id_with_split_cour', return_value=(123, 1, 'Example')), \
                patch.object(discussion, 'get_discussion_link', return_value='42'), \
                patch.object(discussion.requests, 'get', return_value=missing):
            response = discussion.get_discussion('Example', '1', 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['message'], discussion.constants.MESSAGE_DISCUSSION_NOT_FOUND)

    def test_scraped_post_times_are_iso_or_empty(self):
        soup = discussion.BeautifulSoup(
            '<div class="date" data-time="1699510755">Nov 8, 2023 10:19 PM</div>'
            '<div class="date">Yesterday, 3:20 AM</div>', 'html.parser')
        dated, display_only = soup.select('.date')
        self.assertEqual(discussion.scraped_post_time(dated), '2023-11-09T06:19:15+00:00')
        self.assertEqual(discussion.scraped_post_time(display_only), '')

    def test_api_does_not_grant_cross_origin_access(self):
        with patch.object(api, 'get_discussion', return_value={'message': {}}):
            response = self.client.post('/discussion', json={'anime': 'Example', 'season': 1, 'episode': 1},
                                        headers={'Origin': 'https://example.com'})
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_extension_origins_may_call_the_api(self):
        # Firefox users can withhold the host permission, making the background fetch CORS-bound.
        for origin in ['moz-extension://2d5b1806-249f-4b54-937c-9dd8f218c52f',
                       'chrome-extension://abcdefghijklmnopabcdefghijklmnop']:
            with self.subTest(origin=origin):
                preflight = self.client.options('/discussion', headers={
                    'Origin': origin, 'Access-Control-Request-Method': 'POST',
                    'Access-Control-Request-Headers': 'content-type'})
                self.assertEqual(preflight.headers.get('Access-Control-Allow-Origin'), origin)
                self.assertIn('Content-Type', preflight.headers.get('Access-Control-Allow-Headers', ''))
                with patch.object(api, 'get_discussion', return_value={'message': {}}):
                    response = self.client.post('/discussion', headers={'Origin': origin},
                                                json={'anime': 'Yu-Gi-Oh!', 'season': 1, 'episode': 1})
                self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), origin)
        for origin in ['https://moz-extension.example.com', 'moz-extension://not-a-uuid']:
            with self.subTest(origin=origin):
                preflight = self.client.options('/discussion', headers={
                    'Origin': origin, 'Access-Control-Request-Method': 'POST'})
                self.assertNotIn('Access-Control-Allow-Origin', preflight.headers)

if __name__ == '__main__':
    unittest.main()
