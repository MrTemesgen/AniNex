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


if __name__ == '__main__':
    unittest.main()
