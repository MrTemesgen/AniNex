import sys
import threading
import time
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
        for limiter in api.RATE_LIMITS.values():
            limiter.hits.clear()

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
            provider.assert_called_once_with(anime_query='Example', season='movie', episode=1, mal_id=None)

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

    @staticmethod
    def _black_clover():
        """AniList's shape: the 170-episode series, then a separate, airing 2nd Season."""
        series = {'id': 1, 'idMal': 34572, 'format': 'TV', 'episodes': 170,
                  'title': {'romaji': 'Black Clover', 'english': 'Black Clover'}, 'relations': {'edges': []}}
        sequel = {'id': 2, 'idMal': 61967, 'format': 'TV', 'episodes': None, 'nextAiringEpisode': {'episode': 6},
                  'title': {'romaji': 'Black Clover 2nd Season', 'english': 'Black Clover Season 2'},
                  'relations': {'edges': [{'relationType': 'PREQUEL', 'node': series}]}}
        series['relations']['edges'].append({'relationType': 'SEQUEL', 'node': sequel})
        return series, sequel

    def test_crunchyroll_split_seasons_keep_absolute_numbers(self):
        # Crunchyroll splits the 170-episode series into seasons 1-4 with absolute numbers.
        series, sequel = self._black_clover()
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree', side_effect=[None, series]), \
                patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Black Clover', '3', 131),
                             (34572, 131, 'Black_Clover'))
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree', return_value=sequel), \
                patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Black Clover', '2', 52)[:2], (34572, 52))
            # Episodes of the airing 2nd Season stay local to it, including one just past
            # AniList's aired count; neither may land on the original series.
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Black Clover', '2', 5)[:2], (61967, 5))
            with patch.object(discussion, 'fallback_mal_search', return_value=None):
                self.assertNotEqual(discussion.resolve_mal_id_with_split_cour('Black Clover', '2', 6)[0], 34572)

    def test_loose_season_match_from_another_show_yields_to_the_split_series(self):
        # Crunchyroll's "Yu-Gi-Oh!" is the 224-episode Duel Monsters in five seasons with
        # absolute numbers; AniList's search for "Yu-Gi-Oh! Season 2" finds ZEXAL II.
        duel_monsters = self._chain((1, 'TV', 224, 'Yu☆Gi☆Oh! Duel Monsters'),
                                    (2, 'TV', 180, 'Yu☆Gi☆Oh! Duel Monsters GX'))
        duel_monsters['idMal'] = 481
        duel_monsters['title']['english'] = 'Yu-Gi-Oh!'
        zexal_ii = {'id': 3, 'idMal': 15489, 'format': 'TV', 'episodes': 73,
                    'title': {'romaji': 'Yu☆Gi☆Oh! ZEXAL II', 'english': 'Yu-Gi-Oh! ZEXAL II'}}
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree',
                             side_effect=lambda term: zexal_ii if 'Season' in term else duel_monsters), \
                patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Yu-Gi-Oh!', '2', 50)[:2], (481, 50))
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Yu-Gi-Oh!', '5', 210)[:2], (481, 210))

    def test_loose_season_match_is_kept_for_short_base_or_local_numbers(self):
        # Dr. STONE S4 is "SCIENCE FUTURE" (not an exact title) and numbers locally.
        base = self._chain((1, 'TV', 24, 'Dr. STONE'), (2, 'TV', 12, 'Dr. STONE: SCIENCE FUTURE'))
        science_future = base['relations']['edges'][0]['node']
        science_future['title']['english'] = 'Dr. STONE SCIENCE FUTURE'
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree',
                             side_effect=lambda term: science_future if 'Season' in term else base), \
                patch.object(discussion, 'fetch_node_relations', return_value=None):
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Dr. STONE', '4', 1)[:2], (20, 1))
            # A short base isn't a split series, even for a number it covers.
            self.assertEqual(discussion.resolve_mal_id_with_split_cour('Dr. STONE', '2', 12)[:2], (20, 12))

    def test_ova_seasons_resolve_to_the_related_ova_entry(self):
        # Crunchyroll's Slime "OVA Season 1" (E1-E5) is AniList's 2019 five-episode OVA.
        def entry(anilist_id, mal_id, fmt, episodes, year, romaji):
            return {'id': anilist_id, 'idMal': mal_id, 'format': fmt, 'episodes': episodes,
                    'startDate': {'year': year, 'month': 1}, 'title': {'romaji': romaji}}
        base = entry(101280, 37430, 'TV', 24, 2018, 'Tensei Shitara Slime Datta Ken')
        base['title']['english'] = 'That Time I Got Reincarnated as a Slime'
        base['relations'] = {'edges': [
            {'relationType': 'SEQUEL', 'node': entry(161802, 54565, 'OVA', 3, 2023, 'Coleus no Yume')},
            {'relationType': 'ADAPTATION', 'node': entry(86399, 87609, 'MANGA', None, 2015, 'Manga')},
            {'relationType': 'SIDE_STORY', 'node': entry(106509, 38793, 'OVA', 5, 2019, 'Slime OVA')},
            {'relationType': 'SPIN_OFF', 'node': entry(116741, 41488, 'TV', 12, 2021, 'Tensura Nikki')}]}
        with api.app.app_context(), patch.object(discussion, 'fetch_season_tree', return_value=base) as fetch:
            self.assertEqual(discussion.resolve_mal_id_with_split_cour(
                'That Time I Got Reincarnated as a Slime', 'ova', 4)[:2], (38793, 4))
            self.assertEqual(discussion.resolve_mal_id_with_split_cour(
                'That Time I Got Reincarnated as a Slime', 'ova', 1)[:2], (38793, 1))
            # No related OVA has an episode 6: report no match, never season 1's episode.
            self.assertIsNone(discussion.resolve_mal_id_with_split_cour(
                'That Time I Got Reincarnated as a Slime', 'ova', 6)[0])
        fetch.assert_called_with('That Time I Got Reincarnated as a Slime')

    def test_mal_search_only_accepts_entries_named_after_the_show(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': [
            {'node': {'id': 28977, 'title': 'Gintama°', 'alternative_titles': {'en': 'Gintama Season 4'}}},
            {'node': {'id': 40028, 'title': 'Shingeki no Kyojin: The Final Season'}}]}
        with api.app.app_context(), patch.object(discussion.requests, 'get', return_value=response) as get:
            self.assertIsNone(discussion.fallback_mal_search('Yu-Gi-Oh!', '3'))
        self.assertEqual(get.call_args.kwargs['params']['q'], 'Yu-Gi-Oh! Season 3')
        response.json.return_value = {'data': [
            {'node': {'id': 1, 'title': 'Black Cloverfield'}},
            {'node': {'id': 61967, 'title': 'Black Clover 2nd Season',
                      'alternative_titles': {'en': 'Black Clover Season 2'}}}]}
        with api.app.app_context(), patch.object(discussion.requests, 'get', return_value=response):
            self.assertEqual(discussion.fallback_mal_search('Black Clover', '2'), 61967)

    def test_small_episode_numbers_of_missing_seasons_are_not_absolute(self):
        series, _ = self._black_clover()
        with api.app.app_context(), \
                patch.object(discussion, 'fetch_season_tree', side_effect=[None, series]), \
                patch.object(discussion, 'fetch_node_relations', return_value=None), \
                patch.object(discussion, 'fallback_mal_search', return_value=None):
            self.assertIsNone(discussion.resolve_mal_id_with_split_cour('Black Clover', '4', 3)[0])

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

    def _mal_pages_by_offset(self, pages):
        """Fake MAL topic API answering by offset (later pages are fetched in parallel)."""
        def get(url, params, headers, timeout):
            page = pages.get(params['offset'])
            if isinstance(page, Exception):
                raise page
            return page or self._mal_page([], False)  # past the end: empty, like MAL
        return get

    def test_mal_topic_pages_past_the_first_hundred_posts(self):
        first = [{'id': n} for n in range(100)]
        second = [{'id': n} for n in range(99, 130)]  # one repeat from a deletion mid-walk
        with api.app.app_context(), patch.object(discussion.requests, 'get', side_effect=self._mal_pages_by_offset(
                {0: self._mal_page(first, True), 100: self._mal_page(second, False)})) as get:
            error, topic = discussion.fetch_mal_topic('42')
        self.assertIsNone(error)
        self.assertEqual([post['id'] for post in topic['posts']], list(range(130)))
        self.assertNotIn('truncated', topic)
        offsets = sorted(call.kwargs['params']['offset'] for call in get.call_args_list)
        self.assertEqual(offsets[:2], [0, 100])
        self.assertLessEqual(len(offsets), 1 + discussion.MAL_TOPIC_PARALLEL_PAGES)

    def test_mal_topic_later_pages_are_fetched_in_parallel(self):
        pages = {offset: self._mal_page([{'id': offset + n} for n in range(100)], True) for offset in range(0, 600, 100)}
        pages[500] = self._mal_page([{'id': 500 + n} for n in range(40)], False)
        active, peak, lock = [0], [0], threading.Lock()
        fake = self._mal_pages_by_offset(pages)

        def slow_get(url, params, headers, timeout):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return fake(url, params, headers, timeout)

        with api.app.app_context(), patch.object(discussion.requests, 'get', side_effect=slow_get):
            error, topic = discussion.fetch_mal_topic('42')
        self.assertEqual([post['id'] for post in topic['posts']], list(range(540)))
        self.assertNotIn('truncated', topic)
        self.assertGreater(peak[0], 1)

    def test_mal_topic_keeps_first_pages_when_a_later_page_fails(self):
        with api.app.app_context(), patch.object(discussion.requests, 'get', side_effect=self._mal_pages_by_offset(
                {0: self._mal_page([{'id': n} for n in range(100)], True), 100: requests.Timeout()})):
            error, topic = discussion.fetch_mal_topic('42')
        self.assertIsNone(error)
        self.assertEqual(len(topic['posts']), 100)
        self.assertTrue(topic['truncated'])

    def test_repeat_lookups_skip_resolution_and_thread_search(self):
        topic = (None, {'title': 'Episode 1 Discussion', 'posts': []})
        with api.app.app_context(),                 patch.object(discussion, 'resolve_mal_id_with_split_cour', return_value=(21, 339, 'ONE_PIECE')) as resolve,                 patch.object(discussion, 'get_discussion_link', return_value='777') as link,                 patch.object(discussion, 'fetch_mal_topic', return_value=topic):
            for _ in range(3):
                discussion.get_discussion('One Piece', '12', 339)
            discussion.get_discussion('ONE PIECE', '12', 339)  # same title, other case
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(link.call_count, 1)

    def test_lookup_misses_are_not_cached(self):
        with api.app.app_context(),                 patch.object(discussion, 'resolve_mal_id_with_split_cour', return_value=(None, 1, 'X')) as resolve,                 patch.object(discussion, 'fallback_mal_search', return_value=None):
            discussion.get_discussion('New Show', '1', 1)
            discussion.get_discussion('New Show', '1', 1)
        self.assertEqual(resolve.call_count, 2)
        with api.app.app_context(),                 patch.object(discussion, 'resolve_mal_id_with_split_cour', return_value=(5, 1, 'X')),                 patch.object(discussion, 'get_discussion_link', return_value=None) as link,                 patch.object(discussion, 'fallback_forum_search', return_value=None):
            discussion.get_discussion('Airing Show', '1', 1)
            discussion.get_discussion('Airing Show', '1', 1)
        self.assertEqual(link.call_count, 2)  # a thread posted later must still be found

    def test_requests_are_rate_limited_per_client(self):
        limiter = api.RateLimiter(3, 60, clock=lambda: clock[0])
        clock = [0.0]
        with patch.dict(api.RATE_LIMITS, {'/discussion': limiter}),                 patch.object(api, 'get_discussion', return_value={'message': {}}):
            body = {'anime': 'Example', 'season': 1, 'episode': 1}
            statuses = [self.client.post('/discussion', json=body, headers={'X-Forwarded-For': '9.9.9.9, 1.2.3.4'}).status_code
                        for _ in range(4)]
            # The router-appended (last) address is the client; a forged first entry can't dodge it.
            dodge = self.client.post('/discussion', json=body, headers={'X-Forwarded-For': '8.8.8.8, 1.2.3.4'})
            other = self.client.post('/discussion', json=body, headers={'X-Forwarded-For': '5.6.7.8'})
            clock[0] = 61.0
            later = self.client.post('/discussion', json=body, headers={'X-Forwarded-For': '1.2.3.4'})
        self.assertEqual(statuses, [200, 200, 200, 429])
        self.assertEqual(dodge.status_code, 429)
        self.assertEqual(dodge.headers.get('Retry-After'), '30')
        self.assertEqual(other.status_code, 200)
        self.assertEqual(later.status_code, 200)

    def test_procfile_uses_threaded_workers(self):
        with open('Procfile', encoding='utf-8') as procfile:
            command = procfile.read()
        self.assertIn('--worker-class gthread', command)
        self.assertIn('--workers 1', command)

    @staticmethod
    def _mal_thread_with_unreadable(total, unreadable):
        """Fake MAL topic API: any page containing an unreadable post is a 400 'invalid request'."""
        def get(url, params, headers, timeout):
            offset, limit = params['offset'], params['limit']
            positions = range(offset, min(offset + limit, total))
            page = Mock()
            if any(index in unreadable for index in positions):
                page.status_code = 400
                page.json.return_value = {'errors': [{'message': 'invalid request'}]}
                page.raise_for_status.side_effect = requests.HTTPError()
            else:
                page.status_code = 200
                page.json.return_value = {'data': {'title': 'Haikyuu!! Episode 11 Discussion',
                                                   'posts': [{'id': index} for index in positions]}}
            return page
        return get

    def test_unreadable_mal_posts_are_skipped_not_fatal(self):
        # Haikyuu!! episode 11: MAL can't serve post #19, so every page containing it fails.
        with api.app.app_context(), patch.object(
                discussion.requests, 'get', side_effect=self._mal_thread_with_unreadable(130, {18})) as get:
            error, topic = discussion.fetch_mal_topic('1200997')
        self.assertIsNone(error)
        self.assertEqual([post['id'] for post in topic['posts']], [n for n in range(130) if n != 18])
        self.assertEqual(topic['title'], 'Haikyuu!! Episode 11 Discussion')
        self.assertNotIn('truncated', topic)
        self.assertLess(get.call_count, 25)

    def test_threads_ending_inside_a_split_page_and_first_post_unreadable(self):
        for total, unreadable in [(25, {18}), (12, {0}), (40, {3, 31})]:
            with self.subTest(total=total, unreadable=unreadable), api.app.app_context(), patch.object(
                    discussion.requests, 'get', side_effect=self._mal_thread_with_unreadable(total, unreadable)):
                discussion.clear_caches()
                error, topic = discussion.fetch_mal_topic('7')
            self.assertEqual([post['id'] for post in topic['posts']],
                             [n for n in range(total) if n not in unreadable])
            self.assertNotIn('truncated', topic)

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

    def test_discussion_requests_log_stage_timings_without_content(self):
        search = Mock(status_code=200)
        search.json.return_value = {'data': {'Page': {'media': [
            {'id': 7, 'format': 'TV', 'title': {'romaji': 'Secret Show'}}]}}}
        tree = Mock(status_code=200)
        tree.json.return_value = {'data': {'Media': {
            'id': 7, 'idMal': 70, 'format': 'TV', 'episodes': 12, 'title': {'romaji': 'Secret Show'}}}}
        topic = Mock(status_code=200)
        topic.json.return_value = {'data': {'title': 'Secret Show Episode 1', 'posts': [{'id': 1}]}}
        with patch.object(discussion.requests, 'post', side_effect=[search, tree]), \
                patch.object(discussion, 'get_discussion_link', return_value='4242'), \
                patch.object(discussion.requests, 'get', return_value=topic), \
                self.assertLogs('aninex.timing', level='INFO') as logs:
            response = self.client.post('/discussion', json={'anime': 'Secret Show', 'season': 1, 'episode': 1})
        self.assertEqual(response.status_code, 200)
        [line] = logs.output
        self.assertIn('discussion_timing status=200 total_ms=', line)
        for stage in ['anilist_search=', 'anilist_tree=', 'mal_topic_page=']:
            self.assertRegex(line, stage + r'\d+ms/1\b')
        self.assertRegex(line, r'other_ms=\d+ pid=\d+$')
        for secret in ['Secret', '4242', '70', 'myanimelist']:
            self.assertNotIn(secret, line.split(' pid=')[0])

    def test_failed_discussion_requests_are_still_timed(self):
        with self.assertLogs('aninex.timing', level='INFO') as logs:
            with patch.object(api, 'get_discussion', side_effect=requests.Timeout()):
                self.assertEqual(self.client.post(
                    '/discussion', json={'anime': 'Example', 'season': 1, 'episode': 1}).status_code, 504)
            api.app.config['PROPAGATE_EXCEPTIONS'] = False
            try:
                with patch.object(api, 'get_discussion', side_effect=RuntimeError()):
                    self.assertEqual(self.client.post(
                        '/discussion', json={'anime': 'Example', 'season': 1, 'episode': 1}).status_code, 500)
            finally:
                api.app.config['PROPAGATE_EXCEPTIONS'] = None
        self.assertEqual([line.split('discussion_timing ')[1].split(' ')[0] for line in logs.output],
                         ['status=504', 'status=500'])

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

    def test_show_search_returns_picker_cards(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': {'Page': {'media': [
            {'id': 99, 'idMal': 36688, 'format': 'OVA', 'episodes': 1, 'seasonYear': None, 'startDate': {'year': 2018},
             'title': {'romaji': 'Shinmai Maou no Testament: DEPARTURES',
                       'english': 'The Testament of Sister New Devil DEPARTURES', 'native': '新妹魔王の契約者 DEPARTURES'},
             'coverImage': {'large': 'https://s4.anilist.co/file/large.jpg', 'medium': 'https://s4.anilist.co/file/medium.jpg'}},
            {'id': 100, 'idMal': None, 'format': 'TV', 'title': {'romaji': 'No MAL entry'}},
            {'id': 101, 'idMal': 23233, 'format': 'TV', 'episodes': 12, 'seasonYear': 2015,
             'title': {'romaji': 'Shinmai Maou no Testament', 'english': None, 'native': '新妹魔王の契約者'},
             'coverImage': {'large': 'http://insecure.example/cover.jpg'}}]}}}
        with patch.object(discussion.requests, 'post', return_value=response) as post:
            first = self.client.post('/search', json={'q': '  Testament  '})
            second = self.client.post('/search', json={'q': 'testament'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json['results'], [
            {'mal_id': 36688, 'title': 'The Testament of Sister New Devil DEPARTURES',
             'romaji': 'Shinmai Maou no Testament: DEPARTURES', 'native': '新妹魔王の契約者 DEPARTURES',
             'format': 'OVA', 'episodes': 1, 'year': 2018, 'cover': 'https://s4.anilist.co/file/large.jpg'},
            {'mal_id': 23233, 'title': 'Shinmai Maou no Testament', 'romaji': 'Shinmai Maou no Testament',
             'native': '新妹魔王の契約者', 'format': 'TV', 'episodes': 12, 'year': 2015, 'cover': None}])
        self.assertEqual(second.json, first.json)
        self.assertEqual(post.call_count, 1)  # normalized queries share the cache

    def test_show_search_validates_input_and_failures(self):
        with patch.object(discussion.requests, 'post') as post:
            for body in [None, {}, {'q': 'a'}, {'q': 'x' * 101}, {'q': ['list']}]:
                with self.subTest(body=body):
                    self.assertEqual(self.client.post('/search', json=body).status_code, 400)
            post.assert_not_called()
        # Both catalogs timing out (AniList, then the MAL fallback) is a 504.
        with patch.object(discussion.requests, 'post', side_effect=requests.Timeout()),                 patch.object(discussion.requests, 'get', side_effect=requests.Timeout()):
            self.assertEqual(self.client.post('/search', json={'q': 'Example'}).status_code, 504)
        self.assertEqual(self.client.get('/search?q=Example').status_code, 405)  # queries stay out of URLs

    def test_extension_origins_may_search(self):
        origin = 'moz-extension://2d5b1806-249f-4b54-937c-9dd8f218c52f'
        preflight = self.client.options('/search', headers={'Origin': origin, 'Access-Control-Request-Method': 'POST',
                                                             'Access-Control-Request-Headers': 'content-type'})
        self.assertEqual(preflight.headers.get('Access-Control-Allow-Origin'), origin)

    def test_picked_entry_skips_title_resolution(self):
        with patch.object(api, 'get_discussion', return_value={'message': {}}) as provider:
            response = self.client.post('/discussion', json={'anime': 'Shinmai Maou no Testament: DEPARTURES',
                                                              'season': 0, 'episode': 1, 'mal_id': 36688})
        self.assertEqual(response.status_code, 200)
        provider.assert_called_once_with(anime_query='Shinmai Maou no Testament: DEPARTURES', season='0',
                                         episode=1, mal_id=36688)
        for bad in [0, -5, True, 'abc', 123456789]:
            with self.subTest(mal_id=bad):
                self.assertEqual(self.client.post('/discussion', json={
                    'anime': 'Example', 'season': 1, 'episode': 1, 'mal_id': bad}).status_code, 400)
        with api.app.app_context(), \
                patch.object(discussion, 'resolve_mal_id_with_split_cour') as resolve, \
                patch.object(discussion, 'get_discussion_link', return_value='777') as link, \
                patch.object(discussion, 'fetch_mal_topic', return_value=(None, {'title': 'T', 'posts': []})):
            discussion.get_discussion('Shinmai Maou no Testament: DEPARTURES', '0', 1, mal_id=36688)
        resolve.assert_not_called()
        link.assert_called_once_with('Shinmai_Maou_no_Testament:_DEPARTURES', 36688, 1)

    def test_ova_season_accepts_an_entry_that_is_itself_the_ova(self):
        base = {'id': 99, 'idMal': 36688, 'format': 'OVA', 'episodes': 1,
                'title': {'romaji': 'Shinmai Maou no Testament: DEPARTURES',
                          'english': 'The Testament of Sister New Devil DEPARTURES'}, 'relations': {'edges': []}}
        with api.app.app_context(), patch.object(discussion, 'fetch_season_tree', return_value=base):
            self.assertEqual(discussion.resolve_mal_id_with_split_cour(
                'The Testament of Sister New Devil DEPARTURES', 'ova', 1)[:2], (36688, 1))

    @staticmethod
    def _anilist_disabled():
        # AniList's real outage answer (Oct 21): 403 "temporarily disabled".
        response = Mock(status_code=403)
        response.json.return_value = {'errors': [{'message': 'The AniList API has been temporarily disabled due to severe stability issues.', 'status': 403}], 'data': None}
        response.raise_for_status.side_effect = requests.HTTPError()
        return response

    def test_picker_search_falls_back_to_mal_when_anilist_is_down(self):
        mal = Mock(status_code=200)
        mal.json.return_value = {'data': [
            {'node': {'id': 36688, 'title': 'Shinmai Maou no Testament Departures', 'media_type': 'ova', 'num_episodes': 1,
                      'start_season': {'year': 2018}, 'nsfw': 'white',
                      'alternative_titles': {'en': 'The Testament of Sister New Devil Departures', 'ja': '新妹魔王の契約者 DEPARTURES'},
                      'main_picture': {'large': 'https://cdn.myanimelist.net/images/anime/1395/95778l.jpg'}}},
            {'node': {'id': 999, 'title': 'Adult entry', 'nsfw': 'black'}},
            {'node': {'id': 23233, 'title': 'Shinmai Maou no Testament', 'media_type': 'tv', 'num_episodes': 0,
                      'nsfw': 'gray', 'main_picture': {'large': 'http://insecure/cover.jpg'}}}]}
        with patch.object(discussion.requests, 'post', return_value=self._anilist_disabled()), \
                patch.object(discussion.requests, 'get', return_value=mal) as get:
            response = self.client.post('/search', json={'q': 'testament departures'})
            again = self.client.post('/search', json={'q': 'testament departures'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['results'], [
            {'mal_id': 36688, 'title': 'The Testament of Sister New Devil Departures', 'romaji': 'Shinmai Maou no Testament Departures',
             'native': '新妹魔王の契約者 DEPARTURES', 'format': 'OVA', 'episodes': 1, 'year': 2018,
             'cover': 'https://cdn.myanimelist.net/images/anime/1395/95778l.jpg'},
            {'mal_id': 23233, 'title': 'Shinmai Maou no Testament', 'romaji': 'Shinmai Maou no Testament', 'native': None,
             'format': 'TV', 'episodes': None, 'year': None, 'cover': None}])
        self.assertEqual(again.json, response.json)
        self.assertEqual(get.call_count, 2)  # degraded results are not cached
        self.assertEqual(get.call_args.kwargs['params']['q'], 'testament departures')

    def test_picker_fallback_skips_queries_mal_rejects(self):
        with patch.object(discussion.requests, 'post', return_value=self._anilist_disabled()), \
                patch.object(discussion.requests, 'get') as get:
            self.assertEqual(self.client.post('/search', json={'q': 'ab'}).json, {'results': []})
        get.assert_not_called()

    def test_lookups_fall_back_to_mal_search_when_anilist_is_down(self):
        topic = (None, {'title': 'Haikyuu!! Episode 11 Discussion', 'posts': []})
        with api.app.app_context(), \
                patch.object(discussion.requests, 'post', return_value=self._anilist_disabled()), \
                patch.object(discussion, 'fallback_mal_search', return_value=20583) as mal_search, \
                patch.object(discussion, 'get_discussion_link', return_value='1200997') as link, \
                patch.object(discussion, 'fetch_mal_topic', return_value=topic):
            response = discussion.get_discussion('Haikyu!!', '1', 11)
            discussion.get_discussion('Haikyu!!', '1', 11)
        self.assertEqual(response.get_json()['message']['title'], 'Haikyuu!! Episode 11 Discussion')
        mal_search.assert_called_with('Haikyu!!', '1', exact_only=False)
        link.assert_called_once_with('Haikyu!!', 20583, 11)  # thread id cached; resolution is not
        self.assertEqual(mal_search.call_count, 2)
    def test_mal_search_prefers_an_exact_title_over_a_later_season(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': [
            {'node': {'id': 59978, 'title': 'Sousou no Frieren 2nd Season',
                      'alternative_titles': {'en': "Frieren: Beyond Journey's End Season 2"}}},
            {'node': {'id': 52991, 'title': 'Sousou no Frieren',
                      'alternative_titles': {'en': "Frieren: Beyond Journey’s End"}}}]}
        with api.app.app_context(), patch.object(discussion.requests, 'get', return_value=response):
            self.assertEqual(discussion.fallback_mal_search("Frieren: Beyond Journey's End", '1'), 52991)
            self.assertEqual(discussion.fallback_mal_search("Frieren: Beyond Journey's End", '2'), 59978)
    def test_outage_fallback_uses_plain_title_only_for_absolute_numbers(self):
        topic = (None, {'title': 'One Piece Episode 339 Discussion', 'posts': []})
        searches = []

        def mal_search(anime, season, exact_only=False):
            searches.append(season)
            return 21 if season == '1' else None

        with api.app.app_context(),                 patch.object(discussion.requests, 'post', return_value=self._anilist_disabled()),                 patch.object(discussion, 'fallback_mal_search', side_effect=mal_search),                 patch.object(discussion, 'get_discussion_link', return_value='9') as link,                 patch.object(discussion, 'fetch_mal_topic', return_value=topic):
            discussion.get_discussion('One Piece', '12', 339)
            self.assertEqual(searches, ['12', '1'])
            link.assert_called_once_with('One_Piece', 21, 339)
            searches.clear()
            # A small number in a later season is local to it: don't guess season 1.
            response = discussion.get_discussion('Demon Slayer: Kimetsu no Yaiba', '4', 1)
        self.assertEqual(searches, ['4'])
        self.assertEqual(response.get_json()['message'], discussion.constants.MESSAGE_MAL_ID_NOT_FOUND)
    def test_outage_season_search_only_accepts_exact_names(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': [
            {'node': {'id': 61967, 'title': 'Black Clover 2nd Season', 'alternative_titles': {'en': 'Black Clover Season 2'}}}]}
        with api.app.app_context(), patch.object(discussion.requests, 'get', return_value=response):
            self.assertEqual(discussion.fallback_mal_search('Black Clover', '3'), 61967)  # normal mode: named entry
            self.assertIsNone(discussion.fallback_mal_search('Black Clover', '3', exact_only=True))
            self.assertEqual(discussion.fallback_mal_search('Black Clover', '2', exact_only=True), 61967)
    @staticmethod
    def _mal_details(chain):
        """Fake MAL /anime/{id} details: chain = {id: (episodes, media_type, prequel id or None)}."""
        sequels = {prequel: mal_id for mal_id, (_, _, prequel) in chain.items() if prequel}

        def get(url, params, headers, timeout):
            mal_id = int(url.rsplit('/', 1)[1])
            episodes, media_type, prequel = chain[mal_id]
            related = [{'node': {'id': prequel}, 'relation_type': 'prequel'}] if prequel else []
            if mal_id in sequels:
                related.append({'node': {'id': sequels[mal_id]}, 'relation_type': 'sequel'})
            response = Mock(status_code=200)
            response.json.return_value = {'id': mal_id, 'num_episodes': episodes, 'media_type': media_type,
                                          'related_anime': related}
            return response
        return get

    def test_outage_fallback_renumbers_continuously_numbered_seasons(self):
        # MAL: Spy x Family Season 3 (13) <- Season 2 (12) <- Part 2 (13) <- Spy x Family (12).
        # Crunchyroll numbers Season 3 as E38-E50.
        chain = {59027: (13, 'tv', 53887), 53887: (12, 'tv', 50602), 50602: (13, 'tv', 50265), 50265: (12, 'tv', None)}
        with api.app.app_context(), patch.object(discussion.requests, 'get', side_effect=self._mal_details(chain)) as get:
            self.assertEqual(discussion._mal_locate_episode(59027, 38), (59027, 1))
            self.assertEqual(discussion._mal_locate_episode(59027, 50), (59027, 13))
            self.assertEqual(discussion._mal_locate_episode(59027, 5), (59027, 5))    # already local
            self.assertEqual(discussion._mal_locate_episode(59027, 60), (59027, 60))  # past the season: unchanged
            # Crunchyroll Season 1 (25 eps) = MAL "Spy x Family" (12) + "Part 2" (13).
            self.assertEqual(discussion._mal_locate_episode(50265, 24), (50602, 12))
            self.assertEqual(discussion._mal_locate_episode(50265, 7), (50265, 7))
            # From the plain title, a later season's absolute number walks forward (S2 E26 -> Season 2 E1).
            self.assertEqual(discussion._mal_locate_episode(50265, 26), (53887, 1))
            self.assertEqual(discussion._mal_locate_episode(50265, 45), (59027, 8))
            self.assertEqual(get.call_count, 4)  # details are cached
        topic = (None, {'title': 'Spy x Family Season 3 Episode 1 Discussion', 'posts': []})
        with api.app.app_context(),                 patch.object(discussion.requests, 'post', return_value=self._anilist_disabled()),                 patch.object(discussion, 'fallback_mal_search', return_value=59027),                 patch.object(discussion.requests, 'get', side_effect=self._mal_details(chain)),                 patch.object(discussion, 'get_discussion_link', return_value='1') as link,                 patch.object(discussion, 'fetch_mal_topic', return_value=topic):
            discussion.get_discussion('SPY x FAMILY', '3', 38)
        link.assert_called_once_with('SPY_x_FAMILY', 59027, 1)

if __name__ == '__main__':
    unittest.main()
