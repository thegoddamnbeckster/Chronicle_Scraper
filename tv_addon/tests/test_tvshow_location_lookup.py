# -*- coding: utf-8 -*-
"""tvshow_location: finding a show's folder (library first, then source browsing) and the cache in front of it."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs
import xbmc

from lib import tvshow_location as tl


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        p = mock.patch.object(tl.time, 'sleep')
        p.start()
        self.addCleanup(p.stop)

    def _rpc(self, payload=None, exc=None):
        if exc:
            return mock.patch.object(xbmc, 'executeJSONRPC', side_effect=exc)
        return mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps(payload))


class TestLookupShow(_Base):
    def _shows(self, *shows):
        return self._rpc({'result': {'tvshows': list(shows)}})

    def test_picks_the_show_with_the_matching_year_and_normalises_the_folder(self):
        with self._shows({'tvshowid': 1, 'year': 1999, 'file': 'smb://n/TV/Doctor Who (1999)'},
                         {'tvshowid': 2, 'year': 2005, 'file': 'smb://n/TV/Doctor Who (2005)/'}):
            self.assertEqual(tl._lookup_show('Doctor Who', 2005), (2, 'smb://n/TV/Doctor Who (2005)/'))
            self.assertEqual(tl._lookup_show('Doctor Who', 1999), (1, 'smb://n/TV/Doctor Who (1999)/'))

    def test_a_folder_that_does_not_match_the_title_is_refused(self):
        with self._shows({'tvshowid': 1, 'year': 2005, 'file': 'smb://n/TV/Totally Different Show (2005)/'}):
            self.assertEqual(tl._lookup_show('Doctor Who', 2005), (None, None))

    def test_misses_and_failures(self):
        with self._shows():
            self.assertEqual(tl._lookup_show('x', 2000), (None, None))
        with self._shows({'tvshowid': 1, 'year': 2000}):   # no file
            self.assertEqual(tl._lookup_show('x', 2000), (None, None))
        with self._rpc({'error': {'message': 'no'}}):
            self.assertEqual(tl._lookup_show('x', 2000), (None, None))
        with self._rpc(exc=RuntimeError('down')):
            self.assertEqual(tl._lookup_show('x', 2000), (None, None))

    def test_the_library_lookup_retries_a_miss_once(self):
        results = iter([(None, None), (3, 'smb://n/TV/Show/')])
        with mock.patch.object(tl, '_lookup_show', side_effect=lambda t, y: next(results)) as lookup:
            self.assertEqual(tl._lookup_via_video_library('Show', 2000), (3, 'smb://n/TV/Show/'))
        self.assertEqual(lookup.call_count, 2)
        with mock.patch.object(tl, '_lookup_show', return_value=(None, None)) as lookup:
            self.assertEqual(tl._lookup_via_video_library('Show', 2000), (None, None))
        self.assertEqual(lookup.call_count, tl._LOOKUP_RETRIES)


class TestFindShowLocation(_Base):
    def test_a_library_hit_is_cached_and_the_next_call_never_asks_kodi(self):
        with mock.patch.object(tl, '_lookup_via_video_library', return_value=(3, 'smb://n/TV/Show/')) as lookup:
            self.assertEqual(tl.find_show_location('Show', 2000), ('smb://n/TV/Show/', 3))
            self.assertEqual(tl.find_show_location('Show', 2000), ('smb://n/TV/Show/', 3))
        self.assertEqual(lookup.call_count, 1)

    def test_a_source_browsing_hit_has_no_id_and_is_not_cached(self):
        with mock.patch.object(tl, '_lookup_via_video_library', return_value=(None, None)), \
                mock.patch.object(tl, '_search_sources_for_show', return_value='smb://n/TV/Show/') as search:
            self.assertEqual(tl.find_show_location('Show', 2000), ('smb://n/TV/Show/', None))
            tl.find_show_location('Show', 2000)
        self.assertEqual(search.call_count, 2)

    def test_nothing_found_anywhere(self):
        with mock.patch.object(tl, '_lookup_via_video_library', return_value=(None, None)), \
                mock.patch.object(tl, '_search_sources_for_show', return_value=None):
            self.assertEqual(tl.find_show_location('Show', 2000), (None, None))


class TestLocationCache(_Base):
    def test_round_trip_and_key_safety(self):
        tl._save_cached_location('Marvel: Agents/Of S.H.I.E.L.D.', 2013, 'smb://n/TV/A/', 9)
        self.assertEqual(tl._load_cached_location('Marvel: Agents/Of S.H.I.E.L.D.', 2013), ('smb://n/TV/A/', 9))
        self.assertNotIn('/', tl._location_cache_path('a/b', 1).split('tvshow_location_cache/')[1])
        self.assertIsNone(tl._load_cached_location('Other', 2013))

    def test_expired_corrupt_and_unwritable(self):
        tl._save_cached_location('Show', 2000, 'smb://n/TV/Show/', 3)
        path = tl._location_cache_path('Show', 2000)
        stubs._FAKE_FILES[path] = json.dumps({'folder': 'x', 'tvshowid': 1, 'cachedAt': 0}).encode()
        self.assertIsNone(tl._load_cached_location('Show', 2000))
        stubs._FAKE_FILES[path] = b'garbage'
        self.assertIsNone(tl._load_cached_location('Show', 2000))
        with mock.patch.object(tl.xbmcvfs, 'File', side_effect=OSError('read-only')):
            tl._save_cached_location('Show', 2000, 'f', 1)   # must not raise


class TestSearchSources(_Base):
    def setUp(self):
        super().setUp()
        self.listings = {'smb://n/TV/': ['Doctor Who (2005)', 'The Expanse', 'Dark']}
        self.calls = []

        def listing(source, max_age=None):
            self.calls.append(max_age)
            return self.listings.get(source, [])
        for name, new in (('get_video_sources', mock.Mock(return_value=['smb://n/TV/'])),
                          ('list_source_dirs_cached', mock.Mock(side_effect=listing))):
            p = mock.patch.object(tl, name, new)
            p.start()
            self.addCleanup(p.stop)

    def test_title_and_year_then_title_alone_then_a_year_either_side(self):
        self.assertEqual(tl._search_sources_for_show('Doctor Who', 2005), 'smb://n/TV/Doctor Who (2005)/')
        self.assertEqual(tl._search_sources_for_show('The Expanse', 2015), 'smb://n/TV/The Expanse/')
        self.assertEqual(tl._search_sources_for_show('Doctor Who', 2006), 'smb://n/TV/Doctor Who (2005)/')   # one year off

    def test_no_title_or_no_match(self):
        self.assertIsNone(tl._search_sources_for_show('', 2000))
        self.assertIsNone(tl._search_sources_for_show('Not There', 2000))

    def test_a_miss_in_the_long_cached_listing_re_lists_once_so_a_new_folder_is_found(self):
        stale = {'smb://n/TV/': ['Dark']}
        fresh = {'smb://n/TV/': ['Dark', 'Brand New Show']}
        tl.list_source_dirs_cached.side_effect = lambda source, max_age=None: (stale if max_age is None else fresh)[source]
        self.assertEqual(tl._search_sources_for_show('Brand New Show', 2024), 'smb://n/TV/Brand New Show/')


if __name__ == '__main__':
    unittest.main()
