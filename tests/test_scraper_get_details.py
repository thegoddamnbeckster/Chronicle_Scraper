# -*- coding: utf-8 -*-
"""
Smoke tests for python/scraper.py's get_details()/get_artwork() -- the movie addon's core
find/getdetails scrape path had no direct test coverage before this file (only
activity_tracker.py and tvshow_location_cache.py, which it happens to use, were tested).

Added 2026-09-13 alongside the removal of local NFO writing/rebuilding (write_nfo,
lib/nfo_writer.py, lib/rebuild_state.py) from get_details() -- that removal deleted roughly a
third of the function's body, so this locks in that the remaining scrape path (art sync, rating/
resume/watched reconciliation, ListItem population, setResolvedUrl) still runs end to end with
no exception and, just as importantly, proves no local NFO file is written anymore under any
settings combination.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from python import scraper


_MOVIE_DETAILS = {
    'title': 'Dune: Part Two',
    'year': 2024,
    'tagline': 'Long live the fighters.',
    'runtimeMinutes': 166,
    'crew': [{'name': 'Denis Villeneuve', 'job': 'Director'}],
    'collection': None,
    'ratings': {},
    'artwork': {'poster': [{'url': 'https://example.com/poster.jpg'}]},
    'userRating': None,
    'resumePositionPercent': None,
    'resumeUpdatedAt': None,
    'isWatched': False,
    'lastWatchedAt': None,
    'cast': None,
    'knownFileName': None,
}


class TestGetDetailsSmoke(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        scraper.xbmcplugin.setResolvedUrl.reset_mock()

    def _run_get_details(self, write_nfo_setting=True):
        mock_client = MagicMock()
        mock_client.get_movie_details.return_value = dict(_MOVIE_DETAILS)
        scraper.ADDON.getSettingBool = MagicMock(return_value=write_nfo_setting)
        with patch('python.scraper.ChronicleClient', return_value=mock_client), \
             patch('python.scraper.find_movie_location',
                   return_value=('/movies/Dune Part Two (2024)/', 'Dune Part Two (2024)',
                                 'Dune Part Two (2024).mkv', False, None)), \
             patch('python.scraper.sync_movie_art') as mock_sync_art:
            result = scraper.get_details(media_item_id=555, handle=1)
        return result, mock_sync_art

    def test_get_details_completes_and_resolves_the_listitem(self):
        result, mock_sync_art = self._run_get_details()

        self.assertTrue(result)
        scraper.xbmcplugin.setResolvedUrl.assert_called_once()
        mock_sync_art.assert_called_once()
        # getVideoInfoTag() returns an unconstrained MagicMock (see kodi_stubs._FakeListItem) --
        # it doesn't round-trip real state, so the meaningful assertion is that setTitle() was
        # actually invoked with Chronicle's title, not that a later getter echoes it back.
        listitem = scraper.xbmcplugin.setResolvedUrl.call_args.kwargs.get('listitem') \
            or scraper.xbmcplugin.setResolvedUrl.call_args.args[-1]
        vtag = listitem.getVideoInfoTag()
        vtag.setTitle.assert_called_once_with('Dune: Part Two')

    def test_get_details_writes_no_local_nfo_even_with_write_nfo_setting_on(self):
        # write_nfo no longer exists as a real feature -- confirms a stale True value left
        # over in an existing install's settings.xml can't somehow still trigger a write.
        self._run_get_details(write_nfo_setting=True)
        nfo_paths = [p for p in kodi_stubs._FAKE_FILES if p.endswith('.nfo')]
        self.assertEqual(nfo_paths, [], "no .nfo file should ever be written for a movie")

    def test_get_details_returns_false_for_unresolvable_media_item_id(self):
        result, _ = (scraper.get_details(media_item_id=None, handle=1), None)
        self.assertFalse(result)
        scraper.xbmcplugin.setResolvedUrl.assert_not_called()

    def test_nfo_writer_module_no_longer_exists(self):
        with self.assertRaises(ImportError):
            import lib.nfo_writer  # noqa: F401

    def test_fetch_movie_sidecar_no_longer_exists_on_the_client(self):
        from lib.chronicle_client import ChronicleClient
        self.assertFalse(hasattr(ChronicleClient, 'fetch_movie_sidecar'))


class TestGetArtworkSmoke(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_get_artwork_completes_without_error(self):
        mock_client = MagicMock()
        mock_client.get_movie_details.return_value = dict(_MOVIE_DETAILS)
        with patch('python.scraper.ChronicleClient', return_value=mock_client), \
             patch('python.scraper.sync_movie_art') as mock_sync_art:
            result = scraper.get_artwork(media_item_id=555, handle=1)
        self.assertTrue(result)
        mock_sync_art.assert_called_once()


if __name__ == '__main__':
    unittest.main()
