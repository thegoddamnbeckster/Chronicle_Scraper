# -*- coding: utf-8 -*-
"""
Regression tests for a leftover resume bookmark keeping a FINISHED film under Kodi's "In progress movies" (2026-10-11).

F9 was finished and rated on another Shield weeks earlier. Upstairs Kodi still had playcount 1 and a 36% bookmark, so it
listed the film as in progress. The bookmark is only ever cleared by the watched 'push' branch, and that branch is never
taken once Kodi's lastplayed reads as newer than Chronicle's -- the two clocks were six hours apart for the same moment.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

from lib import progress_sync, watch_rating_sync
from tests.test_timeutil import zone, _us_mountain

# Real F9 values as they sat in the database: the finish stored as 19:34:48 (a local clock reading stored as if it were
# UTC by the old add-on) and the other Shield's lastplayed 01:34:43 (the real UTC finish, written into Kodi as if it
# were local). Six hours apart for the same moment; the time zone fix stops new ones, this repairs the existing ones.
F9_FINISHED = '2026-09-03T19:34:48Z'
F9_KODI = {'playcount': 1, 'lastplayed': '2026-09-04 01:34:43', 'resume': {'position': 3278.0, 'total': 8995.0}}


def setUpModule():
    global _zone
    _zone = zone(_us_mountain)
    _zone.__enter__()


def tearDownModule():
    _zone.__exit__(None, None, None)


class TestHasStaleResume(unittest.TestCase):

    def test_f9_is_stale(self):
        self.assertTrue(progress_sync.has_stale_resume(True, F9_FINISHED, None, dict(F9_KODI)))

    def test_f9_is_not_resolved_by_the_plain_direction_logic(self):
        # Documents WHY the extra rule exists: the clock skew sends F9 down the 'pull' branch.
        direction, _ = progress_sync.resolve_watched_direction(True, F9_FINISHED, dict(F9_KODI))
        self.assertNotEqual('push', direction)

    def test_a_genuinely_newer_rewatch_is_left_alone(self):
        kodi = dict(F9_KODI, lastplayed='2026-10-05 20:00:00')
        self.assertFalse(progress_sync.has_stale_resume(True, F9_FINISHED, None, kodi))

    def test_chronicle_not_finished(self):
        self.assertFalse(progress_sync.has_stale_resume(False, F9_FINISHED, None, dict(F9_KODI)))

    def test_chronicle_has_its_own_resume(self):
        self.assertFalse(progress_sync.has_stale_resume(True, F9_FINISHED, 12.0, dict(F9_KODI)))

    def test_no_bookmark_in_kodi(self):
        kodi = dict(F9_KODI, resume={'position': 0, 'total': 0})
        self.assertFalse(progress_sync.has_stale_resume(True, F9_FINISHED, None, kodi))

    def test_kodi_never_played_it(self):
        kodi = dict(F9_KODI, playcount=0)
        self.assertFalse(progress_sync.has_stale_resume(True, F9_FINISHED, None, kodi))

    def test_no_kodi_item(self):
        self.assertFalse(progress_sync.has_stale_resume(True, F9_FINISHED, None, None))

    def test_no_finish_time_leaves_it_alone(self):
        self.assertFalse(progress_sync.has_stale_resume(True, None, None, dict(F9_KODI)))

    def test_missing_kodi_lastplayed_counts_as_old(self):
        kodi = dict(F9_KODI, lastplayed='')
        self.assertTrue(progress_sync.has_stale_resume(True, F9_FINISHED, None, kodi))


class TestBuildStateUpdates(unittest.TestCase):

    def _details(self, **kw):
        d = {'isWatched': True, 'lastWatchedAt': F9_FINISHED, 'resumePositionPercent': None,
             'resumeUpdatedAt': None, 'runtimeMinutes': 150}
        d.update(kw)
        return d

    def test_stale_bookmark_is_cleared_in_the_periodic_pass(self):
        updates = watch_rating_sync._build_state_updates(
            self._details(), dict(F9_KODI), MagicMock(), 1, log_label='F9')
        self.assertEqual({'position': 0, 'total': 0}, updates.get('resume'))

    def test_a_newer_rewatch_bookmark_survives(self):
        kodi = dict(F9_KODI, lastplayed='2026-10-05 20:00:00')
        updates = watch_rating_sync._build_state_updates(
            self._details(), kodi, MagicMock(), 1, log_label='F9')
        self.assertNotEqual({'position': 0, 'total': 0}, updates.get('resume'))


if __name__ == '__main__':
    unittest.main()
