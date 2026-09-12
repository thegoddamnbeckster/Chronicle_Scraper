# -*- coding: utf-8 -*-
"""
Regression tests for the tvshow_location.py location cache.

Root-caused live (2026-09-12): find_show_location() ran unconditionally on
every get_episode_details() call, re-resolving the exact same (folder,
tvshowid) for every episode of a show via a JSON-RPC round-trip back into
Kodi's own API (with its own retry-and-sleep on a miss). During a first-time
scan of 100+ shows, with Kodi running many of these scraper callbacks
concurrently, this became self-inflicted contention on Kodi's own API from
Kodi's own scan -- shows sat with only their most-recently-processed handful
of episodes ever committed, no exception anywhere to point at the cause.

These tests prove two things: a cache hit skips the network round-trip
entirely (the actual fix), and the cache is safe (TTL expiry, and the
no-tvshowid source-browsing result is never cached).
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcvfs

from lib import tvshow_location


class TestLocationCacheRoundTrip(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_save_then_load_returns_the_same_folder_and_tvshowid(self):
        tvshow_location._save_cached_location('Some Show', 2024, '/tv/Some Show/', 42)

        cached = tvshow_location._load_cached_location('Some Show', 2024)

        self.assertEqual(cached, ('/tv/Some Show/', 42))

    def test_different_title_or_year_is_a_cache_miss(self):
        tvshow_location._save_cached_location('Some Show', 2024, '/tv/Some Show/', 42)

        self.assertIsNone(tvshow_location._load_cached_location('Some Show', 2025))
        self.assertIsNone(tvshow_location._load_cached_location('A Different Show', 2024))

    def test_never_cached_is_a_miss(self):
        self.assertIsNone(tvshow_location._load_cached_location('Never Cached', 2024))

    def test_expired_entry_is_treated_as_a_miss(self):
        # Rewrite cachedAt directly to a timestamp older than the TTL, instead of sleeping in a
        # test for real.
        tvshow_location._save_cached_location('Old Show', 2020, '/tv/Old Show/', 7)
        path = tvshow_location._location_cache_path('Old Show', 2020)
        data = json.loads(kodi_stubs._FAKE_FILES[path].decode('utf-8'))
        data['cachedAt'] = time.time() - tvshow_location._LOCATION_CACHE_TTL_SECONDS - 1
        kodi_stubs._FAKE_FILES[path] = json.dumps(data).encode('utf-8')

        self.assertIsNone(tvshow_location._load_cached_location('Old Show', 2020))

    def test_entry_just_inside_the_ttl_is_still_a_hit(self):
        tvshow_location._save_cached_location('Fresh Show', 2024, '/tv/Fresh Show/', 9)
        path = tvshow_location._location_cache_path('Fresh Show', 2024)
        data = json.loads(kodi_stubs._FAKE_FILES[path].decode('utf-8'))
        data['cachedAt'] = time.time() - tvshow_location._LOCATION_CACHE_TTL_SECONDS + 5
        kodi_stubs._FAKE_FILES[path] = json.dumps(data).encode('utf-8')

        self.assertEqual(tvshow_location._load_cached_location('Fresh Show', 2024), ('/tv/Fresh Show/', 9))


class TestFindShowLocationUsesTheCache(unittest.TestCase):
    """The actual regression guard: a cache hit must never touch the network."""

    def setUp(self):
        kodi_stubs.reset_vfs()

    @patch('lib.tvshow_location._search_sources_for_show')
    @patch('lib.tvshow_location._lookup_via_video_library')
    def test_cache_hit_never_calls_lookup_via_video_library(self, mock_lookup, mock_search):
        tvshow_location._save_cached_location('Strange New Worlds', 2022, '/tv/Strange New Worlds/', 132)

        folder, tvshowid = tvshow_location.find_show_location('Strange New Worlds', 2022)

        self.assertEqual((folder, tvshowid), ('/tv/Strange New Worlds/', 132))
        mock_lookup.assert_not_called()
        mock_search.assert_not_called()

    @patch('lib.tvshow_location._search_sources_for_show')
    @patch('lib.tvshow_location._lookup_via_video_library')
    def test_cache_miss_falls_through_to_the_real_lookup_and_populates_the_cache(
            self, mock_lookup, mock_search):
        mock_lookup.return_value = (132, '/tv/Strange New Worlds/')

        folder, tvshowid = tvshow_location.find_show_location('Strange New Worlds', 2022)

        self.assertEqual((folder, tvshowid), ('/tv/Strange New Worlds/', 132))
        mock_lookup.assert_called_once_with('Strange New Worlds', 2022)
        mock_search.assert_not_called()

        # The whole point: the NEXT call for this same show must now be a cache hit.
        mock_lookup.reset_mock()
        folder2, tvshowid2 = tvshow_location.find_show_location('Strange New Worlds', 2022)
        self.assertEqual((folder2, tvshowid2), ('/tv/Strange New Worlds/', 132))
        mock_lookup.assert_not_called()

    @patch('lib.tvshow_location._search_sources_for_show')
    @patch('lib.tvshow_location._lookup_via_video_library')
    def test_source_browsing_fallback_result_is_never_cached(self, mock_lookup, mock_search):
        # VideoLibrary doesn't know this show yet (brand new), but source browsing finds its
        # folder -- no tvshowid available yet. See _save_cached_location's own doc for why this
        # specific shape must never be cached: a stale no-tvshowid answer would keep being
        # served even after Kodi actually commits the show and the fast path becomes available.
        mock_lookup.return_value = (None, None)
        mock_search.return_value = '/tv/Brand New Show/'

        folder, tvshowid = tvshow_location.find_show_location('Brand New Show', 2026)
        self.assertEqual((folder, tvshowid), ('/tv/Brand New Show/', None))

        self.assertIsNone(tvshow_location._load_cached_location('Brand New Show', 2026))

        # And the very next call must try again for real, not serve anything stale.
        mock_lookup.reset_mock()
        mock_search.reset_mock()
        mock_lookup.return_value = (55, '/tv/Brand New Show/')
        folder2, tvshowid2 = tvshow_location.find_show_location('Brand New Show', 2026)
        mock_lookup.assert_called_once_with('Brand New Show', 2026)
        self.assertEqual((folder2, tvshowid2), ('/tv/Brand New Show/', 55))

    @patch('lib.tvshow_location._search_sources_for_show')
    @patch('lib.tvshow_location._lookup_via_video_library')
    def test_not_found_anywhere_is_never_cached_either(self, mock_lookup, mock_search):
        mock_lookup.return_value = (None, None)
        mock_search.return_value = None

        folder, tvshowid = tvshow_location.find_show_location('Nonexistent Show', 2026)

        self.assertEqual((folder, tvshowid), (None, None))
        self.assertIsNone(tvshow_location._load_cached_location('Nonexistent Show', 2026))


if __name__ == '__main__':
    unittest.main()
