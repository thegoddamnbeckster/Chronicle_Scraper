# -*- coding: utf-8 -*-
"""
resolve_watched_direction's Chronicle-side "rewatch reset" handling. A reset done in Chronicle
(episode, season or whole show) used to undo itself within minutes: Kodi still carried the old
playcount, and with Chronicle unwatched the reconciliation returned 'pull', re-importing that
stale watch. With the reset instant supplied, a Kodi watch at or before it is an echo to CLEAR
(returns 'reset'); a watch after it is a genuine rewatch and still pulls.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib import progress_sync


def _kodi(lastplayed='2026-09-19 17:10:40', playcount=1):
    return {'lastplayed': lastplayed, 'playcount': playcount}


class TestWatchReset(unittest.TestCase):

    def test_kodi_watch_before_the_reset_is_cleared_not_pulled(self):
        direction, value = progress_sync.resolve_watched_direction(
            False, None, _kodi(), chronicle_reset_at='2026-09-25T09:00:00')
        self.assertEqual((direction, value), ('reset', None))

    def test_kodi_watch_after_the_reset_is_a_genuine_rewatch_and_pulls(self):
        direction, value = progress_sync.resolve_watched_direction(
            False, None, _kodi('2026-09-26 20:00:00'), chronicle_reset_at='2026-09-25T09:00:00')
        self.assertEqual(direction, 'pull')
        self.assertEqual(value, '2026-09-26 20:00:00')

    def test_no_reset_recorded_keeps_the_old_pull_behaviour(self):
        direction, _ = progress_sync.resolve_watched_direction(False, None, _kodi())
        self.assertEqual(direction, 'pull')

    def test_reset_never_touches_an_item_kodi_has_not_watched(self):
        self.assertEqual(
            progress_sync.resolve_watched_direction(
                False, None, _kodi(playcount=0, lastplayed=''), chronicle_reset_at='2026-09-25T09:00:00'),
            (None, None))

    def test_chronicle_watched_again_after_reset_still_pushes(self):
        direction, _ = progress_sync.resolve_watched_direction(
            True, '2026-09-27T10:00:00', _kodi('2026-09-19 17:10:40'), chronicle_reset_at='2026-09-25T09:00:00')
        self.assertEqual(direction, 'push')

    def test_apply_watched_reset_clears_playcount_lastplayed_and_resume(self):
        vtag = MagicMock()
        progress_sync.apply_watched_reset(vtag)
        vtag.setPlaycount.assert_called_once_with(0)
        vtag.setLastPlayed.assert_called_once_with('')
        vtag.setResumePoint.assert_called_once_with(0, 0)


if __name__ == '__main__':
    unittest.main()
