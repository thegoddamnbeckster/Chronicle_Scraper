"""Scanning carries on while something is being watched, at a lower priority: a scraper call waits a moment first, only while a video plays."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401  (installs the xbmc stubs before the module under test is imported)
from lib import playback_pacing


class TestYieldToPlayback(unittest.TestCase):
    def _playing(self, playing):
        return mock.patch.object(playback_pacing.xbmc, 'getCondVisibility', side_effect=lambda cond: playing and cond == 'Player.HasVideo')

    def test_nothing_playing_means_no_wait_at_all(self):
        wait = mock.Mock()
        with self._playing(False):
            self.assertFalse(playback_pacing.yield_to_playback(wait=wait))
        wait.assert_not_called()

    def test_a_video_playing_makes_the_call_wait_a_moment_and_nothing_more(self):
        wait = mock.Mock()
        with self._playing(True):
            self.assertTrue(playback_pacing.yield_to_playback(wait=wait))
        wait.assert_called_once_with(playback_pacing.PLAYBACK_PACE_SECONDS)

    def test_the_wait_is_a_pace_not_a_block(self):
        # A stalled stream is the failure being avoided; a wait of minutes would stall the scan instead of slowing it.
        self.assertLessEqual(playback_pacing.PLAYBACK_PACE_SECONDS, 10)

    def test_a_kodi_that_cannot_be_asked_counts_as_nothing_playing(self):
        with mock.patch.object(playback_pacing.xbmc, 'getCondVisibility', side_effect=RuntimeError('shutting down')):
            self.assertFalse(playback_pacing.is_video_playing())


if __name__ == '__main__':
    unittest.main()
