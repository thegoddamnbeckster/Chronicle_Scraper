# -*- coding: utf-8 -*-
"""progress_sync: which side wins when Kodi and Chronicle disagree about resume position / watched status.

This file is identical in the movie and TV add-ons' test folders (each add-on carries its own progress_sync with the same decision
logic); anything one lacks is skipped. Dates are days apart so the result does not depend on the machine's time zone.
"""
import json
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import progress_sync as ps

CHRONICLE_AT = '2026-10-01T12:00:00Z'
KODI_NEWER = '2026-10-05 12:00:00'
KODI_OLDER = '2026-09-20 12:00:00'


def _kodi(playcount=0, position=0, total=0, lastplayed=''):
    return {'playcount': playcount, 'resume': {'position': position, 'total': total}, 'lastplayed': lastplayed}


class TestResumeDirection(unittest.TestCase):
    def test_no_kodi_item_pushes_whatever_chronicle_has(self):
        self.assertEqual(ps.resolve_progress_direction(40.0, CHRONICLE_AT, None), ('push', 40.0))
        self.assertEqual(ps.resolve_progress_direction(None, None, None), (None, None))
        self.assertEqual(ps.resolve_progress_direction(0.0, CHRONICLE_AT, None), ('push', 0.0))   # 0 is a value, not "nothing"

    def test_one_sided_data_wins_outright(self):
        self.assertEqual(ps.resolve_progress_direction(None, None, _kodi(position=30, total=100, lastplayed=KODI_OLDER)), ('pull', 30.0))
        self.assertEqual(ps.resolve_progress_direction(40.0, CHRONICLE_AT, _kodi()), ('push', 40.0))
        self.assertEqual(ps.resolve_progress_direction(None, None, _kodi()), (None, None))

    def test_a_percent_without_any_play_time_is_not_a_signal(self):
        self.assertEqual(ps.resolve_progress_direction(None, None, _kodi(position=30, total=100, lastplayed='')), (None, None))

    def test_both_sides_the_newer_wins(self):
        self.assertEqual(ps.resolve_progress_direction(40.0, CHRONICLE_AT, _kodi(position=50, total=100, lastplayed=KODI_NEWER)), ('pull', 50.0))
        self.assertEqual(ps.resolve_progress_direction(40.0, CHRONICLE_AT, _kodi(position=50, total=100, lastplayed=KODI_OLDER)), ('push', 40.0))

    def test_a_watched_kodi_item_counts_as_100_percent(self):
        self.assertEqual(ps.resolve_progress_direction(None, None, _kodi(playcount=1, lastplayed=KODI_NEWER)), ('pull', 100.0))


class TestWatchedDirection(unittest.TestCase):
    def test_no_kodi_item(self):
        self.assertEqual(ps.resolve_watched_direction(True, CHRONICLE_AT, None), ('push', CHRONICLE_AT))
        self.assertEqual(ps.resolve_watched_direction(False, None, None), (None, None))

    def test_one_sided(self):
        self.assertEqual(ps.resolve_watched_direction(False, None, _kodi(playcount=1, lastplayed=KODI_NEWER)), ('pull', KODI_NEWER))
        self.assertEqual(ps.resolve_watched_direction(True, CHRONICLE_AT, _kodi()), ('push', CHRONICLE_AT))
        self.assertEqual(ps.resolve_watched_direction(False, None, _kodi()), (None, None))

    def test_the_same_play_on_both_sides_is_left_alone(self):
        # Reported 2026-10-31: equal times fell through to 'push', so every pass re-wrote identical values.
        same = '2026-10-01 12:00:00'
        chronicle = ps.timeutil.kodi_local_to_iso(same)
        self.assertEqual(ps.resolve_watched_direction(True, chronicle, _kodi(playcount=1, lastplayed=same)), (None, None))

    def test_a_playcount_without_a_play_time_is_not_a_watch(self):
        self.assertEqual(ps.resolve_watched_direction(False, None, _kodi(playcount=1, lastplayed='')), (None, None))

    def test_both_sides_the_newer_wins(self):
        self.assertEqual(ps.resolve_watched_direction(True, CHRONICLE_AT, _kodi(playcount=1, lastplayed=KODI_NEWER)), ('pull', KODI_NEWER))
        self.assertEqual(ps.resolve_watched_direction(True, CHRONICLE_AT, _kodi(playcount=1, lastplayed=KODI_OLDER)), ('push', CHRONICLE_AT))

    def test_a_chronicle_reset_clears_an_older_echo_but_not_a_genuine_rewatch(self):
        reset_at = '2026-10-02T12:00:00Z'
        self.assertEqual(ps.resolve_watched_direction(False, None, _kodi(playcount=1, lastplayed=KODI_OLDER), reset_at), ('reset', None))
        self.assertEqual(ps.resolve_watched_direction(False, None, _kodi(playcount=1, lastplayed=KODI_NEWER), reset_at), ('pull', KODI_NEWER))


class TestStaleResume(unittest.TestCase):
    def test_a_leftover_bookmark_on_a_finished_item_is_stale(self):
        kodi = _kodi(playcount=1, position=500, total=1000, lastplayed='2026-10-01 18:00:00')
        self.assertTrue(ps.has_stale_resume(True, CHRONICLE_AT, None, kodi))

    def test_a_real_rewatch_days_later_is_left_alone(self):
        self.assertFalse(ps.has_stale_resume(True, CHRONICLE_AT, None, _kodi(playcount=1, position=500, total=1000, lastplayed=KODI_NEWER)))

    def test_not_stale_when_there_is_nothing_to_clear_or_no_basis(self):
        base = _kodi(playcount=1, position=500, total=1000, lastplayed='2026-10-01 18:00:00')
        self.assertFalse(ps.has_stale_resume(False, CHRONICLE_AT, None, base))           # Chronicle has not finished it
        self.assertFalse(ps.has_stale_resume(True, CHRONICLE_AT, 40.0, base))            # Chronicle itself has a resume point
        self.assertFalse(ps.has_stale_resume(True, CHRONICLE_AT, None, None))
        self.assertFalse(ps.has_stale_resume(True, CHRONICLE_AT, None, _kodi(playcount=0, position=500, total=1000, lastplayed='2026-10-01 18:00:00')))
        self.assertFalse(ps.has_stale_resume(True, CHRONICLE_AT, None, _kodi(playcount=1, position=0, total=0, lastplayed='2026-10-01 18:00:00')))
        self.assertFalse(ps.has_stale_resume(True, None, None, base))                    # no finish time to compare


class TestApply(unittest.TestCase):
    def test_watched_push_sets_playcount_time_and_zeroes_the_resume_point(self):
        vtag = mock.MagicMock()
        ps.apply_watched_push(vtag, CHRONICLE_AT)
        vtag.setPlaycount.assert_called_once_with(1)
        vtag.setResumePoint.assert_called_once_with(0, 0)
        self.assertRegex(vtag.setLastPlayed.call_args[0][0], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')

    def test_watched_push_without_a_time_does_nothing(self):
        vtag = mock.MagicMock()
        ps.apply_watched_push(vtag, None)
        self.assertEqual(vtag.mock_calls, [])

    def test_watched_reset(self):
        vtag = mock.MagicMock()
        ps.apply_watched_reset(vtag)
        vtag.setPlaycount.assert_called_once_with(0)
        vtag.setLastPlayed.assert_called_once_with('')
        vtag.setResumePoint.assert_called_once_with(0, 0)

    def test_resume_push_converts_percent_of_runtime_to_seconds(self):
        vtag = mock.MagicMock()
        ps.apply_resume_push(vtag, 25.0, 120)
        vtag.setResumePoint.assert_called_once_with(1800.0, 7200)

    def test_resume_push_skips_nothing_to_resume_finished_and_unknown_runtime(self):
        for pct, runtime in ((None, 120), (0, 120), (-1, 120), (98, 120), (99.5, 120), (25.0, None), (25.0, 0)):
            vtag = mock.MagicMock()
            ps.apply_resume_push(vtag, pct, runtime)
            vtag.setResumePoint.assert_not_called()

    def test_kodi_time_to_iso_carries_an_offset(self):
        self.assertRegex(ps.kodi_lastplayed_to_iso('2026-10-01 18:00:00'), r'^2026-10-01T18:00:00[+-]\d\d:\d\d$|^2026-10-01T18:00:00Z$')


class TestLookups(unittest.TestCase):
    def _rpc(self, result):
        return mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps({'result': result}))

    def test_movie_state_prefers_the_matching_year(self):
        if not hasattr(ps, 'lookup_movie_state'):
            self.skipTest('movie add-on only')
        movies = [{'year': 1990, 'playcount': 1}, {'year': 2012, 'playcount': 0}]
        with self._rpc({'movies': movies}):
            self.assertEqual(ps.lookup_movie_state('Total Recall', 2012)['year'], 2012)
            self.assertEqual(ps.lookup_movie_state('Total Recall', None)['year'], 1990)
            self.assertEqual(ps.lookup_movie_state('Total Recall', 1999)['year'], 1990)   # no match: first
        with self._rpc({}):
            self.assertIsNone(ps.lookup_movie_state('Nope', 2000))
        self.assertIsNone(ps.lookup_movie_state('', 2000))
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertIsNone(ps.lookup_movie_state('x', 2000))

    def test_episode_state_matches_season_and_episode(self):
        if not hasattr(ps, 'lookup_episode_state'):
            self.skipTest('TV add-on only')
        episodes = [{'season': 1, 'episode': 1}, {'season': 1, 'episode': 2, 'playcount': 1}]
        with self._rpc({'episodes': episodes}):
            self.assertEqual(ps.lookup_episode_state(7, 1, 2)['playcount'], 1)
            self.assertIsNone(ps.lookup_episode_state(7, 9, 9))
        self.assertIsNone(ps.lookup_episode_state(None, 1, 1))
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertIsNone(ps.lookup_episode_state(7, 1, 1))


if __name__ == '__main__':
    unittest.main()
