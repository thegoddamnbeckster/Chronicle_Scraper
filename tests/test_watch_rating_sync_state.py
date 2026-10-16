# -*- coding: utf-8 -*-
"""watch_rating_sync: the per-item state reconciliation, text diffs and the Kodi JSON-RPC readers/writers."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import watch_rating_sync as wrs

CHRONICLE_AT = '2026-10-01T12:00:00Z'
KODI_NEWER = '2030-01-01 12:00:00'
KODI_OLDER = '2020-01-01 12:00:00'


def _kodi(userrating=0, playcount=0, position=0, total=0, lastplayed=''):
    return {'userrating': userrating, 'playcount': playcount, 'lastplayed': lastplayed, 'resume': {'position': position, 'total': total}}


class TestBuildStateUpdates(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()

    def _build(self, details, kodi):
        return wrs._build_state_updates(details, kodi, self.client, 5, log_label='Heat')

    def test_a_different_rating_is_written_a_matching_one_is_not(self):
        self.assertEqual(self._build({'userRating': 8}, _kodi(userrating=6)), {'userrating': 8})
        self.assertEqual(self._build({'userRating': 8}, _kodi(userrating=8)), {})
        self.assertEqual(self._build({'userRating': None}, _kodi(userrating=8)), {})        # Chronicle has none: Kodi's is kept

    def test_chronicle_resume_is_pushed_as_seconds(self):
        updates = self._build({'resumePositionPercent': 50.0, 'resumeUpdatedAt': CHRONICLE_AT, 'runtimeMinutes': 100}, _kodi())
        self.assertEqual(updates['resume'], {'position': 3000.0, 'total': 6000})

    def test_a_resume_without_a_runtime_writes_nothing(self):
        self.assertEqual(self._build({'resumePositionPercent': 50.0, 'resumeUpdatedAt': CHRONICLE_AT}, _kodi()), {})

    def test_kodis_newer_resume_is_reported_to_chronicle_not_written(self):
        updates = self._build({'resumePositionPercent': 10.0, 'resumeUpdatedAt': CHRONICLE_AT, 'runtimeMinutes': 100},
                              _kodi(position=500, total=1000, lastplayed=KODI_NEWER))
        self.assertEqual(updates, {})
        self.client.push_resume.assert_called_once()
        self.assertEqual(self.client.push_resume.call_args[0][:2], (5, 50.0))

    def test_a_watched_item_gets_playcount_time_and_a_zeroed_bookmark(self):
        updates = self._build({'isWatched': True, 'lastWatchedAt': CHRONICLE_AT}, _kodi(position=500, total=1000))
        self.assertEqual(updates['playcount'], 1)
        self.assertEqual(updates['resume'], {'position': 0, 'total': 0})
        self.assertRegex(updates['lastplayed'], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')

    def test_a_chronicle_reset_zeroes_playcount_without_sending_lastplayed(self):
        updates = self._build({'isWatched': False, 'watchResetAt': '2020-06-01T00:00:00Z'}, _kodi(playcount=1, lastplayed=KODI_OLDER))
        self.assertEqual(updates, {'playcount': 0, 'resume': {'position': 0, 'total': 0}})

    def test_kodis_newer_watch_is_reported_to_chronicle(self):
        self._build({'isWatched': True, 'lastWatchedAt': CHRONICLE_AT}, _kodi(playcount=1, lastplayed=KODI_NEWER))
        self.client.push_watched.assert_called_once()

    def test_a_stale_bookmark_on_a_finished_item_is_cleared(self):
        kodi = _kodi(playcount=1, position=500, total=1000, lastplayed='2026-10-01 18:00:00')
        updates = self._build({'isWatched': True, 'lastWatchedAt': CHRONICLE_AT}, kodi)
        self.assertEqual(updates.get('resume'), {'position': 0, 'total': 0})


class TestTextDiffs(unittest.TestCase):
    def test_episode_diff_only_adds_what_differs_and_never_blanks(self):
        kodi = {'title': 'Pilot', 'plot': 'old', 'firstaired': '2020-01-01', 'art': {'thumb': 'old', 'fanart': 'f'}}
        details = {'title': 'Pilot', 'overview': 'new', 'aired': '2020-02-02T00:00:00Z', 'thumbUrl': 'new-thumb'}
        self.assertEqual(wrs.diff_episode_text(kodi, details),
                         {'plot': 'new', 'firstaired': '2020-02-02', 'art': {'thumb': 'new-thumb', 'fanart': 'f'}})
        self.assertEqual(wrs.diff_episode_text(kodi, {}), {})
        self.assertEqual(wrs.diff_episode_text(kodi, {'title': 'Pilot', 'overview': 'old', 'aired': '2020-01-01T00:00:00Z', 'thumbUrl': 'old'}), {})

    def test_show_diff_compares_lists_as_sets_and_omits_year(self):
        kodi = {'plot': 'p', 'premiered': '2020-01-01', 'mpaa': 'TV-MA', 'genre': ['Drama', 'Crime'], 'studio': ['HBO']}
        same = {'overview': 'p', 'premiered': '2020-01-01T00:00:00Z', 'mpaa': 'TV-MA', 'genres': ['Crime', 'Drama'], 'studio': 'HBO', 'year': 2021}
        self.assertEqual(wrs.diff_show_text(kodi, same), {})
        changed = {'overview': 'q', 'premiered': '2021-03-03', 'mpaa': 'TV-14', 'genres': ['Comedy'], 'studio': 'AMC'}
        self.assertEqual(wrs.diff_show_text(kodi, changed),
                         {'plot': 'q', 'premiered': '2021-03-03', 'mpaa': 'TV-14', 'genre': ['Comedy'], 'studio': ['AMC']})
        self.assertEqual(wrs.diff_show_text(kodi, {}), {})


class TestRpc(unittest.TestCase):
    def _rpc(self, payload=None, exc=None):
        if exc:
            return mock.patch.object(xbmc, 'executeJSONRPC', side_effect=exc)
        return mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps(payload))

    def test_readers_return_lists_and_survive_every_failure(self):
        readers = ((lambda: wrs._get_all_movies(), 'movies'), (lambda: wrs._get_all_tvshows(), 'tvshows'),
                   (lambda: wrs._get_episodes_for_show(7), 'episodes'))
        for call, key in readers:
            with self._rpc({'result': {key: [{'id': 1}]}}):
                self.assertEqual(call(), [{'id': 1}])
            with self._rpc({'result': {}}):
                self.assertEqual(call(), [])
            with self._rpc({'error': {'message': 'no'}}):
                self.assertEqual(call(), [])
            with self._rpc(exc=RuntimeError('down')):
                self.assertEqual(call(), [])

    def test_writers_name_the_right_method_and_id_and_never_raise(self):
        cases = [(lambda: wrs._set_movie_details(5, {'userrating': 8}), 'VideoLibrary.SetMovieDetails', {'userrating': 8, 'movieid': 5}),
                 (lambda: wrs._set_episode_details(6, {'playcount': 1}), 'VideoLibrary.SetEpisodeDetails', {'playcount': 1, 'episodeid': 6}),
                 (lambda: wrs._set_tvshow_details(7, {'plot': 'x'}), 'VideoLibrary.SetTVShowDetails', {'plot': 'x', 'tvshowid': 7}),
                 (lambda: wrs._set_tvshow_rating(7, 9), 'VideoLibrary.SetTVShowDetails', {'tvshowid': 7, 'userrating': 9})]
        for call, method, params in cases:
            with self._rpc({'result': 'OK'}) as rpc:
                call()
            sent = json.loads(rpc.call_args[0][0])
            self.assertEqual((sent['method'], sent['params']), (method, params))
            with self._rpc({'error': {'message': 'rejected'}}):
                call()
            with self._rpc(exc=RuntimeError('down')):
                call()


if __name__ == '__main__':
    unittest.main()
