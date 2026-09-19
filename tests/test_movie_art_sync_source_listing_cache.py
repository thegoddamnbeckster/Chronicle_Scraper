# -*- coding: utf-8 -*-
"""
Regression tests for lib/movie_art_sync.py's source-listing cache
(list_source_dirs_cached) -- added (2026-09-18) after a real full-library scan was found to
spend ~30-40s per movie almost entirely on _search_sources_for_movie re-listing every
configured video source from scratch, over the network, for nearly every single movie (see
that function's own module-level doc for the full root-cause). The cache turns "list every
source per movie" into "list every source once per TTL window, reuse for every movie scraped
inside it."
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import movie_art_sync


class TestListSourceDirsCached(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_second_call_within_ttl_reuses_the_cache_without_hitting_the_network(self):
        with patch.object(movie_art_sync, 'listdir_with_timeout',
                           return_value=(['Movie A (2020)', 'Movie B (2021)'], [])) as mock_listdir:
            first = movie_art_sync.list_source_dirs_cached('smb://nas/movies/')
            second = movie_art_sync.list_source_dirs_cached('smb://nas/movies/')

        self.assertEqual(first, ['Movie A (2020)', 'Movie B (2021)'])
        self.assertEqual(second, first)
        mock_listdir.assert_called_once()

    def test_call_after_ttl_expires_hits_the_network_again(self):
        with patch.object(movie_art_sync, 'listdir_with_timeout',
                           return_value=(['Movie A (2020)'], [])) as mock_listdir:
            movie_art_sync.list_source_dirs_cached('smb://nas/movies/')

        # Simulate the TTL having elapsed by backdating the cache entry directly, rather than
        # sleeping for real in a test.
        cache = movie_art_sync._read_source_listing_cache()
        cache['smb://nas/movies/']['timestamp'] = time.time() - movie_art_sync._SOURCE_LISTING_CACHE_TTL_SECONDS - 1
        movie_art_sync._write_source_listing_cache(cache)

        with patch.object(movie_art_sync, 'listdir_with_timeout',
                           return_value=(['Movie A (2020)', 'Movie C (2022)'], [])) as mock_listdir:
            result = movie_art_sync.list_source_dirs_cached('smb://nas/movies/')

        self.assertEqual(result, ['Movie A (2020)', 'Movie C (2022)'])
        mock_listdir.assert_called_once()

    def test_a_timeout_is_not_cached_so_the_next_call_retries(self):
        with patch.object(movie_art_sync, 'listdir_with_timeout',
                           return_value=(None, None)) as mock_listdir:
            first = movie_art_sync.list_source_dirs_cached('smb://flaky-nas/movies/')

        self.assertEqual(first, [])

        with patch.object(movie_art_sync, 'listdir_with_timeout',
                           return_value=(['Movie A (2020)'], [])) as mock_listdir_retry:
            second = movie_art_sync.list_source_dirs_cached('smb://flaky-nas/movies/')

        mock_listdir_retry.assert_called_once()
        self.assertEqual(second, ['Movie A (2020)'])

    def test_different_sources_are_cached_independently(self):
        def _fake_listdir(path, timeout_seconds=None):
            return ([path + '-dir'], [])

        with patch.object(movie_art_sync, 'listdir_with_timeout', side_effect=_fake_listdir):
            a = movie_art_sync.list_source_dirs_cached('smb://nas/movies-a/')
            b = movie_art_sync.list_source_dirs_cached('smb://nas/movies-b/')

        self.assertEqual(a, ['smb://nas/movies-a/-dir'])
        self.assertEqual(b, ['smb://nas/movies-b/-dir'])


if __name__ == '__main__':
    unittest.main()
