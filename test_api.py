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


if __name__ == '__main__':
    unittest.main()
