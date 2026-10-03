# -*- coding: utf-8 -*-
"""
Tests for the movie scraper's quick-scan mode (added 2026-10-02) and the lookup changes behind it.

Getting ~900 missing movies into Kodi took about a minute per new movie, almost all of it
find_movie_location waiting on an unresponsive share (twice per movie: `find` and `getdetails`)
plus art/watch work per movie. Quick scan defers the art files, collection art and watched/
resume reconciliation to the passes that already run after every scan; the lookup no longer
waits on a dead share or pauses for a VideoLibrary retry a brand-new movie cannot satisfy.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from lib import movie_art_sync
from python import scraper


_DETAILS = {
    'title': 'Dune: Part Two',
    'year': 2024,
    'runtimeMinutes': 166,
    'crew': [],
    'collection': {'name': 'Dune Collection', 'posterUrl': 'https://example.com/set.jpg'},
    'ratings': {},
    'artwork': {'poster': [{'url': 'https://example.com/poster.jpg'}]},
    'userRating': None,
    'resumePositionPercent': None,
    'resumeUpdatedAt': None,
    'isWatched': True,
    'lastWatchedAt': '2026-01-01T00:00:00Z',
    'cast': None,
    'knownFileName': None,
}

_LOCATION = ('/movies/Dune Part Two (2024)/', 'Dune Part Two (2024)', 'Dune Part Two (2024).mkv', True, None)


def _setting(quick):
    return MagicMock(side_effect=lambda key: quick if key == 'quick_scan' else True)


class TestGetDetailsQuickScan(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        scraper.xbmcplugin.setResolvedUrl.reset_mock()

    def _run(self, quick):
        client = MagicMock()
        client.get_movie_details.return_value = dict(_DETAILS)
        scraper.ADDON.getSettingBool = _setting(quick)
        with patch('python.scraper.ChronicleClient', return_value=client), \
             patch('python.scraper.find_movie_location', return_value=_LOCATION) as locate, \
             patch('python.scraper.sync_movie_art') as sync_art, \
             patch('python.scraper.sync_collection_art') as sync_collection, \
             patch.object(scraper.progress_sync, 'lookup_movie_state',
                          wraps=scraper.progress_sync.lookup_movie_state) as lookup_state, \
             patch.object(scraper.progress_sync, 'apply_watched_push',
                          wraps=scraper.progress_sync.apply_watched_push) as watched_push:
            result = scraper.get_details(media_item_id=555, handle=1)
        return result, client, locate, sync_art, sync_collection, (lookup_state, watched_push)

    def test_quick_scan_adds_the_movie_but_defers_art_files_collection_art_and_watch_sync(self):
        result, _client, _locate, sync_art, sync_collection, progress = self._run(quick=True)

        self.assertTrue(result)
        scraper.xbmcplugin.setResolvedUrl.assert_called_once()   # the movie IS added
        sync_art.assert_not_called()
        sync_collection.assert_not_called()
        lookup_state, watched_push = progress
        lookup_state.assert_not_called()
        watched_push.assert_not_called()

    def test_quick_scan_still_records_the_resolved_file_so_the_post_scan_pass_can_match_it(self):
        # full_sync_check matches Kodi's movie to Chronicle by the exact file name, which
        # Chronicle only knows once resolved-file has been reported.
        _result, client, _locate, _a, _c, _p = self._run(quick=True)

        client.report_resolved_file.assert_called_once_with(555, 'Dune Part Two (2024).mkv')

    def test_quick_scan_skips_the_videolibrary_retry_pause(self):
        _result, _client, locate, _a, _c, _p = self._run(quick=True)

        self.assertEqual(locate.call_args.kwargs.get('library_retries'), 1)

    def test_full_mode_still_does_everything(self):
        result, _client, locate, sync_art, sync_collection, progress = self._run(quick=False)

        self.assertTrue(result)
        sync_art.assert_called_once()
        sync_collection.assert_called_once()
        lookup_state, watched_push = progress
        lookup_state.assert_called_once()
        watched_push.assert_called_once()   # Chronicle says watched; full mode pushes it into Kodi
        self.assertIsNone(locate.call_args.kwargs.get('library_retries'))


class TestQuickScanSetting(unittest.TestCase):

    def test_reads_the_setting(self):
        scraper.ADDON.getSettingBool = _setting(False)
        self.assertFalse(scraper.quick_scan_enabled())
        scraper.ADDON.getSettingBool = _setting(True)
        self.assertTrue(scraper.quick_scan_enabled())

    def test_an_unreadable_setting_means_on(self):
        scraper.ADDON.getSettingBool = MagicMock(side_effect=RuntimeError('no such setting'))
        self.assertTrue(scraper.quick_scan_enabled())


class TestSearchPrecheckDoesNotPause(unittest.TestCase):

    def test_find_precheck_uses_a_single_videolibrary_attempt(self):
        client = MagicMock()
        client.search_movie.return_value = None
        with patch('python.scraper.ChronicleClient', return_value=client), \
             patch('python.scraper.find_movie_location',
                   return_value=(None, None, 'X (2020).mkv', False, None)) as locate:
            scraper.search_for_movie('X', 2020, handle=1)

        self.assertEqual(locate.call_args.kwargs.get('library_retries'), 1)
        client.search_movie.assert_called_once_with('X', 2020, filename='X (2020).mkv')


class TestFindMovieLocationRetries(unittest.TestCase):

    def test_single_attempt_does_not_sleep(self):
        with patch.object(movie_art_sync, '_lookup_movie_file', return_value=(None, None)) as lookup, \
             patch.object(movie_art_sync, '_search_sources_for_movie', return_value=None), \
             patch.object(movie_art_sync.time, 'sleep') as sleep:
            movie_art_sync.find_movie_location('Anything', 2020, library_retries=1)

        lookup.assert_called_once()
        sleep.assert_not_called()

    def test_default_keeps_the_original_retry_and_pause(self):
        with patch.object(movie_art_sync, '_lookup_movie_file', return_value=(None, None)) as lookup, \
             patch.object(movie_art_sync, '_search_sources_for_movie', return_value=None), \
             patch.object(movie_art_sync.time, 'sleep') as sleep:
            movie_art_sync.find_movie_location('Anything', 2020)

        self.assertEqual(lookup.call_count, movie_art_sync._LOOKUP_RETRIES)
        sleep.assert_called_once()


class TestSourceSearchRelistsOnlyOnAMiss(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def _prime(self, source, dirs, age_seconds):
        import time
        cache = movie_art_sync._read_source_listing_cache()
        cache[source] = {'timestamp': time.time() - age_seconds, 'dirs': dirs}
        movie_art_sync._write_source_listing_cache(cache)

    def test_a_hit_in_an_older_cached_listing_does_not_touch_the_network(self):
        source = 'smb://nas/Movies/'
        self._prime(source, ['Red Planet (2000)'], age_seconds=300)

        with patch.object(movie_art_sync, 'get_video_sources', return_value=[source]), \
             patch.object(movie_art_sync, 'listdir_with_timeout') as listdir, \
             patch.object(movie_art_sync, '_find_video_filename', return_value='Red Planet (2000).mkv'):
            result = movie_art_sync._search_sources_for_movie('Red Planet', 2000)

        self.assertEqual(result, (source + 'Red Planet (2000)/', 'Red Planet (2000).mkv'))
        listdir.assert_not_called()

    def test_a_miss_relists_a_stale_source_and_finds_a_folder_added_since(self):
        source = 'smb://nas/Movies/'
        self._prime(source, ['Red Planet (2000)'], age_seconds=300)

        with patch.object(movie_art_sync, 'get_video_sources', return_value=[source]), \
             patch.object(movie_art_sync, 'listdir_with_timeout',
                          return_value=(['Red Planet (2000)', 'Brand New (2026)'], [])) as listdir, \
             patch.object(movie_art_sync, '_find_video_filename', return_value='Brand New (2026).mkv'):
            result = movie_art_sync._search_sources_for_movie('Brand New', 2026)

        self.assertEqual(result, (source + 'Brand New (2026)/', 'Brand New (2026).mkv'))
        listdir.assert_called_once()

    def test_a_miss_does_not_relist_a_listing_fresher_than_a_minute(self):
        source = 'smb://nas/Movies/'
        self._prime(source, ['Red Planet (2000)'], age_seconds=10)

        with patch.object(movie_art_sync, 'get_video_sources', return_value=[source]), \
             patch.object(movie_art_sync, 'listdir_with_timeout') as listdir:
            result = movie_art_sync._search_sources_for_movie('Nonexistent Film', 1999)

        self.assertIsNone(result)
        listdir.assert_not_called()


if __name__ == '__main__':
    unittest.main()
