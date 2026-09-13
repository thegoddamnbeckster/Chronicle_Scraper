# -*- coding: utf-8 -*-
"""
Regression tests proving Chronicle_Scraper (TV) never writes a per-episode NFO file (removed
2026-09-12, per-user direction: "Everything must scan into Kodi regardless of whether it's
found in chronicle or not" surfaced that Kodi's NfoUrl action for an episode carries no show
context whatsoever, and unlike a failed SHOW-level NfoUrl call (tolerated, falls back to
Kodi's normal find/getepisodelist flow), a failed EPISODE-level one aborts the rest of that
show's scan entirely with no fallback of its own -- a structural Kodi limitation. The fix:
Chronicle stops generating per-episode NFOs at all, so that fragile path can never be
triggered by content Chronicle itself creates. See tv_nfo_writer.py's own doc, and
ScraperController.ResolveEpisodeByExternalId's/NfoRebuildQueueService.EnsureSeededAsync's own
docs (Chronicle server repo) for the full reasoning.

These tests exercise the real get_episode_details() end to end (the ordinary, non-NfoUrl scan
path) under the exact conditions that used to trigger a write -- write_nfo enabled AND a
rebuild pass active -- and prove no file ever lands in the fake VFS. A second test locks in
the removal at the module level, so a future accidental re-add would fail loudly here even if
some other test happened to mock around the VFS check.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from python import tvshow_scraper
from lib import tv_nfo_writer


_EPISODE_DETAILS = {
    'title': 'Part One: Master and Apprentice',
    'overview': 'Ahsoka investigates a threat.',
    'season': 1,
    'episode': 1,
    'year': 2023,
    'aired': None,
    'runtimeMinutes': 40,
    'cast': None,
    'crew': None,
    'ratings': {},
    'thumbUrl': 'https://image.tmdb.org/t/p/w500/fake.jpg',
    'externalIds': {'imdb': None, 'tvdb': None, 'tmdb': '2552685', 'trakt': None},
    # No showTitle -- keeps find_show_location() (and everything it used to feed) out of
    # scope entirely; this test only cares whether an episode NFO gets written, not the
    # separate rating/resume reconciliation path.
    'showTitle': None,
    'showYear': None,
    'userRating': None,
    'resumePositionPercent': None,
    'resumeUpdatedAt': None,
    'isWatched': False,
    'lastWatchedAt': None,
}


class TestNoEpisodeNfoIsEverWritten(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        tvshow_scraper.xbmcplugin.setResolvedUrl.reset_mock()

    def test_get_episode_details_writes_no_nfo_even_with_write_nfo_and_active_rebuild(self):
        # Exactly the two conditions that used to gate sync_episode_nfo() -- if either of
        # these could still trigger a write, this is where it would show up.
        tvshow_scraper.ADDON.getSettingBool = MagicMock(return_value=True)  # write_nfo=True
        mock_client = MagicMock()
        mock_client.get_episode_details.return_value = dict(_EPISODE_DETAILS)
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client), \
             patch('python.tvshow_scraper.rebuild_state') as mock_rebuild_state:
            mock_rebuild_state.is_active.return_value = True

            result = tvshow_scraper.get_episode_details(
                tvshow_scraper.build_lookup_string(473792), handle=1)

        self.assertTrue(result)
        tvshow_scraper.xbmcplugin.setResolvedUrl.assert_called_once()
        # activity_tracker's own cross-addon scan-signal file is expected and unrelated --
        # this asserts specifically that no .nfo file exists anywhere in the fake VFS.
        nfo_paths = [p for p in kodi_stubs._FAKE_FILES if p.endswith('.nfo')]
        self.assertEqual(nfo_paths, [], "no .nfo file should ever be written for an episode")

    def test_sync_episode_nfo_no_longer_exists(self):
        # Locks the removal in at the module level -- a future accidental re-introduction
        # (e.g. copy-pasted back in from git history) fails this test immediately, even
        # before it could be wired back into get_episode_details().
        self.assertFalse(hasattr(tv_nfo_writer, 'sync_episode_nfo'))

    def test_fetch_episode_sidecar_no_longer_exists_on_the_client(self):
        from lib.chronicle_client import ChronicleClient
        self.assertFalse(hasattr(ChronicleClient, 'fetch_episode_sidecar'))


if __name__ == '__main__':
    unittest.main()
