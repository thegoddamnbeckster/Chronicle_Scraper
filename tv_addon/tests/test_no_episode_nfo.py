# -*- coding: utf-8 -*-
"""
Regression tests proving Chronicle_Scraper (TV) never writes a local NFO file, at either the
episode or show level.

Per-episode NFO writing was removed first (2026-09-12, per-user direction: "Everything must
scan into Kodi regardless of whether it's found in chronicle or not" surfaced that Kodi's
NfoUrl action for an episode carries no show context whatsoever, and unlike a failed
SHOW-level NfoUrl call (tolerated, falls back to Kodi's normal find/getepisodelist flow), a
failed EPISODE-level one aborts the rest of that show's scan entirely with no fallback of its
own -- a structural Kodi limitation).

Show-level NFO writing (tvshow.nfo, lib/tv_nfo_writer.py, the write_nfo setting) was removed
entirely the next day (2026-09-13, per-user direction, after it was suspected of interfering
with TV show scanning reliability during a heavy rescan session) -- closing the gap this file's
own docstring used to note ("Show-level (tvshow.nfo) writing is unaffected"). Chronicle's own
API is the source of truth this scraper reads from directly; no local NFO was ever required for
it to work.

These tests exercise the real get_details() and get_episode_details() end to end (the ordinary,
non-NfoUrl scan path) and prove no file ever lands in the fake VFS. Module-level checks lock the
removal in so a future accidental re-add (e.g. copy-pasted back in from git history) fails here
immediately, even before it could be wired back into either function.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from python import tvshow_scraper


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

_SHOW_DETAILS = {
    'title': 'Ahsoka',
    'year': 2023,
    'tagline': None,
    'status': 'Ended',
    'runtimeMinutes': 45,
    'seasons': [],
    'ratings': {},
    'artwork': {},
    'userRating': None,
    'cast': None,
}


class TestNoNfoIsEverWritten(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        tvshow_scraper.xbmcplugin.setResolvedUrl.reset_mock()

    def test_get_episode_details_writes_no_nfo(self):
        mock_client = MagicMock()
        mock_client.get_episode_details.return_value = dict(_EPISODE_DETAILS)
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.get_episode_details(
                tvshow_scraper.build_lookup_string(473792), handle=1)

        self.assertTrue(result)
        tvshow_scraper.xbmcplugin.setResolvedUrl.assert_called_once()
        # activity_tracker's own cross-addon scan-signal file is expected and unrelated --
        # this asserts specifically that no .nfo file exists anywhere in the fake VFS.
        nfo_paths = [p for p in kodi_stubs._FAKE_FILES if p.endswith('.nfo')]
        self.assertEqual(nfo_paths, [], "no .nfo file should ever be written for an episode")

    def test_get_details_writes_no_show_nfo(self):
        mock_client = MagicMock()
        mock_client.get_show_details.return_value = dict(_SHOW_DETAILS)
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.get_details(12345, handle=1)

        self.assertTrue(result)
        tvshow_scraper.xbmcplugin.setResolvedUrl.assert_called_once()
        nfo_paths = [p for p in kodi_stubs._FAKE_FILES if p.endswith('.nfo')]
        self.assertEqual(nfo_paths, [], "no tvshow.nfo file should ever be written")

    def test_tv_nfo_writer_module_no_longer_exists(self):
        # Locks the removal in at the module level -- a future accidental re-introduction
        # (e.g. copy-pasted back in from git history) fails this test immediately, even
        # before it could be wired back into get_details().
        with self.assertRaises(ImportError):
            import lib.tv_nfo_writer  # noqa: F401

    def test_fetch_episode_sidecar_no_longer_exists_on_the_client(self):
        from lib.chronicle_client import ChronicleClient
        self.assertFalse(hasattr(ChronicleClient, 'fetch_episode_sidecar'))

    def test_fetch_show_sidecar_no_longer_exists_on_the_client(self):
        from lib.chronicle_client import ChronicleClient
        self.assertFalse(hasattr(ChronicleClient, 'fetch_show_sidecar'))


if __name__ == '__main__':
    unittest.main()
