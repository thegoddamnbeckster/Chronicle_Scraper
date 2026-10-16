# -*- coding: utf-8 -*-
"""movie_art_sync: finding a movie's folder (known file, library, source browsing) and writing Chronicle's art there."""
import json
import threading
import unittest
from unittest import mock

import tests.kodi_stubs as stubs
import xbmc

from lib import movie_art_sync as mas


def _rpc_returning(payload):
    return mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps(payload))


class TestPureHelpers(unittest.TestCase):
    def test_strip_video_ext(self):
        self.assertEqual(mas.strip_video_ext('Heat (1995).MKV'), 'Heat (1995)')
        self.assertEqual(mas.strip_video_ext('Heat.srt'), 'Heat.srt')
        self.assertIsNone(mas.strip_video_ext(''))
        self.assertIsNone(mas.strip_video_ext(None))

    def test_normalize(self):
        self.assertEqual(mas.normalize("Mar.IA & Co's: 2"), 'mariacos2')
        self.assertEqual(mas.normalize(None), '')

    def test_year_tolerant_match_is_exact_on_title_and_loose_only_on_year(self):
        m = mas.year_tolerant_match
        self.assertTrue(m('alien1979', 'alien', 1979))
        self.assertTrue(m('alien1980', 'alien', 1979))
        self.assertTrue(m('alien', 'alien', 1979))              # no year in the folder
        self.assertFalse(m('alien1981', 'alien', 1979))         # two years off
        self.assertFalse(m('alienromulus2024', 'alien', 1979))  # a different title that merely starts the same
        self.assertFalse(m('alien1979', 'alien', None))         # a year suffix with no year to compare
        self.assertFalse(m('predator', 'alien', 1979))

    def test_multipath_sources_are_split_into_real_paths(self):
        self.assertEqual(mas._expand_multipath('smb://nas/Movies/'), ['smb://nas/Movies/'])
        self.assertEqual(mas._expand_multipath('multipath://smb%3a%2f%2fa%2fMovies%2f/smb%3a%2f%2fb%2fFilms%2f/'),
                         ['smb://a/Movies/', 'smb://b/Films/'])

    def test_video_sources_skip_addons_and_expand_multipath(self):
        sources = {'result': {'sources': [{'file': 'smb://a/Movies/'}, {'file': 'addons://sources/video/'}, {'file': ''},
                                          {'file': 'multipath://smb%3a%2f%2fb%2fX%2f/smb%3a%2f%2fc%2fY%2f/'}, {'file': 'plain'}]}}
        with _rpc_returning(sources):
            self.assertEqual(mas.get_video_sources(), ['smb://a/Movies/', 'smb://b/X/', 'smb://c/Y/'])
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertEqual(mas.get_video_sources(), [])


class TestListdirWithTimeout(unittest.TestCase):
    def test_result_error_and_hang(self):
        with mock.patch.object(mas.xbmcvfs, 'listdir', create=True, return_value=(['a'], ['b.mkv'])):
            self.assertEqual(mas.listdir_with_timeout('smb://x/'), (['a'], ['b.mkv']))
        with mock.patch.object(mas.xbmcvfs, 'listdir', create=True, side_effect=OSError('gone')):
            self.assertEqual(mas.listdir_with_timeout('smb://x/'), (None, None))
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(mas.xbmcvfs, 'listdir', create=True, side_effect=lambda p: release.wait(30)):
            self.assertEqual(mas.listdir_with_timeout('smb://x/', timeout_seconds=0.05), (None, None))


class TestLibraryLookups(unittest.TestCase):
    def test_known_filename_is_an_exact_basename_match(self):
        movies = {'result': {'movies': [{'movieid': 1, 'file': 'smb://n/A/Other.mkv'}, {'movieid': 2, 'file': 'smb://n/B/Heat (1995).mkv'}]}}
        with _rpc_returning(movies):
            self.assertEqual(mas._lookup_by_known_filename('Heat (1995).mkv'), ('smb://n/B/Heat (1995).mkv', 2))
            self.assertEqual(mas._lookup_by_known_filename('Nope.mkv'), (None, None))
        with _rpc_returning({'error': {}}):
            self.assertEqual(mas._lookup_by_known_filename('x.mkv'), (None, None))
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertEqual(mas._lookup_by_known_filename('x.mkv'), (None, None))

    def test_title_lookup_prefers_the_year_and_verifies_the_folder(self):
        movies = {'result': {'movies': [{'movieid': 1, 'year': 1990, 'file': 'smb://n/Total Recall (1990)/Total Recall (1990).mkv'},
                                        {'movieid': 2, 'year': 2012, 'file': 'smb://n/Total Recall (2012)/Total Recall (2012).mkv'}]}}
        with _rpc_returning(movies):
            self.assertEqual(mas._lookup_movie_file('Total Recall', 2012)[1], 2)
            # no year to check the folder's year against: a folder carrying a year is refused rather than guessed at
            self.assertEqual(mas._lookup_movie_file('Total Recall', None), (None, None))

    def test_a_wrong_stored_title_never_writes_into_another_movies_folder(self):
        movies = {'result': {'movies': [{'movieid': 1, 'year': 1979, 'file': 'smb://n/Alien Romulus (2024)/Alien Romulus (2024).mkv'}]}}
        with _rpc_returning(movies):
            self.assertEqual(mas._lookup_movie_file('Alien', 1979), (None, None))

    def test_misses_and_failures(self):
        for payload in ({'result': {'movies': []}}, {'result': {'movies': [{'movieid': 1}]}}, {'error': {'message': 'no'}}):
            with _rpc_returning(payload):
                self.assertEqual(mas._lookup_movie_file('x', 2000), (None, None))
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertEqual(mas._lookup_movie_file('x', 2000), (None, None))

    def test_retries_only_as_many_times_as_asked(self):
        with mock.patch.object(mas, '_lookup_movie_file', return_value=(None, None)) as lookup, mock.patch.object(mas.time, 'sleep') as sleep:
            mas._lookup_via_video_library('x', 2000)
            self.assertEqual((lookup.call_count, sleep.call_count), (mas._LOOKUP_RETRIES, mas._LOOKUP_RETRIES - 1))
            lookup.reset_mock()
            sleep.reset_mock()
            mas._lookup_via_video_library('x', 2000, retries=1)
            self.assertEqual((lookup.call_count, sleep.call_count), (1, 0))


class TestFindMovieLocation(unittest.TestCase):
    def test_a_known_filename_wins_and_is_not_a_fallback(self):
        with mock.patch.object(mas, '_lookup_by_known_filename', return_value=('smb://n/Heat/Heat.mkv', 5)):
            self.assertEqual(mas.find_movie_location('Heat', 1995, known_filename='Heat.mkv'), ('smb://n/Heat/', 'Heat', 'Heat.mkv', False, 5))

    def test_library_hit_is_a_fallback_only_without_a_known_filename(self):
        with mock.patch.object(mas, '_lookup_by_known_filename', return_value=(None, None)), \
                mock.patch.object(mas, '_lookup_via_video_library', return_value=('smb://n/Heat/Heat.mkv', 5)):
            self.assertTrue(mas.find_movie_location('Heat', 1995)[3])
            self.assertFalse(mas.find_movie_location('Heat', 1995, known_filename='Other.mkv')[3])

    def test_source_browsing_is_the_last_resort_and_has_no_movie_id(self):
        with mock.patch.object(mas, '_lookup_via_video_library', return_value=(None, None)), \
                mock.patch.object(mas, '_search_sources_for_movie', return_value=('smb://n/Heat/', 'Heat.mkv')):
            self.assertEqual(mas.find_movie_location('Heat', 1995), ('smb://n/Heat/', 'Heat', 'Heat.mkv', True, None))

    def test_nothing_found(self):
        with mock.patch.object(mas, '_lookup_via_video_library', return_value=(None, None)), \
                mock.patch.object(mas, '_search_sources_for_movie', return_value=None):
            self.assertEqual(mas.find_movie_location('Heat', 1995), (None, None, None, False, None))


class TestSourceSearch(unittest.TestCase):
    def setUp(self):
        self.dirs = {'smb://n/Movies/': ['Heat (1995)', 'Alien (1979)', 'Alien Romulus (2024)', 'Toy Story 5 ()', 'Arctic Armageddon (2023)']}
        for name, new in (('get_video_sources', mock.Mock(return_value=['smb://n/Movies/'])),
                          ('list_source_dirs_cached', mock.Mock(side_effect=lambda source, max_age=None: self.dirs.get(source, []))),
                          ('_find_video_filename', mock.Mock(side_effect=lambda folder: 'movie.mkv'))):
            p = mock.patch.object(mas, name, new)
            p.start()
            self.addCleanup(p.stop)

    def test_tiers(self):
        self.assertEqual(mas._search_sources_for_movie('Heat', 1995), ('smb://n/Movies/Heat (1995)/', 'movie.mkv'))
        self.assertEqual(mas._search_sources_for_movie('Toy Story 5', 2026)[0], 'smb://n/Movies/Toy Story 5 ()/')           # folder without a year
        self.assertEqual(mas._search_sources_for_movie('Arctic Armageddon', 2024)[0], 'smb://n/Movies/Arctic Armageddon (2023)/')  # a year off

    def test_a_prefix_is_never_a_match(self):
        self.assertIsNone(mas._search_sources_for_movie('Alien Rom', 2024))
        self.assertEqual(mas._search_sources_for_movie('Alien', 1979)[0], 'smb://n/Movies/Alien (1979)/')

    def test_a_folder_without_a_video_is_not_a_match(self):
        mas._find_video_filename.side_effect = lambda folder: None
        self.assertIsNone(mas._search_sources_for_movie('Heat', 1995))

    def test_blank_title(self):
        self.assertIsNone(mas._search_sources_for_movie('', 1995))

    def test_a_new_folder_is_found_by_re_listing_once_when_the_long_cached_listing_misses(self):
        stale = ['Heat (1995)']
        fresh = ['Heat (1995)', 'Brand New (2026)']
        mas.list_source_dirs_cached.side_effect = lambda source, max_age=None: stale if max_age is None else fresh
        self.assertEqual(mas._search_sources_for_movie('Brand New', 2026)[0], 'smb://n/Movies/Brand New (2026)/')


class TestFindVideoFilename(unittest.TestCase):
    def test_first_video_file_or_none(self):
        with mock.patch.object(mas, 'listdir_with_timeout', return_value=([], ['poster.jpg', 'Heat.MKV', 'Heat.srt'])):
            self.assertEqual(mas._find_video_filename('smb://n/Heat/'), 'Heat.MKV')
        with mock.patch.object(mas, 'listdir_with_timeout', return_value=([], ['poster.jpg'])):
            self.assertIsNone(mas._find_video_filename('smb://n/Heat/'))
        with mock.patch.object(mas, 'listdir_with_timeout', return_value=(None, None)):
            self.assertIsNone(mas._find_video_filename('smb://n/Heat/'))


class TestWriteAndSync(unittest.TestCase):
    ART = {'poster': [{'url': 'http://c/p.jpg'}, {'url': 'http://c/p2.jpg'}], 'fanart': [{'url': 'http://c/f.jpg'}]}

    def setUp(self):
        stubs.reset_vfs()
        self.responses = {}
        outer = self

        class Resp:
            def __init__(self, data):
                self._d = data

            def read(self):
                return self._d

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        p = mock.patch('urllib.request.urlopen', side_effect=lambda url, timeout=None: Resp(outer.responses[url]))
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(mas, '_invalidate_texture')
        self.invalidate = q.start()
        self.addCleanup(q.stop)

    def _sync(self, artwork=None, location=('smb://n/Heat (1995)/', 'Heat (1995)')):
        self.responses.update({'http://c/p.jpg': b'POSTER', 'http://c/f.jpg': b'FANART'})
        return mas.sync_movie_art('Heat', 1995, self.ART if artwork is None else artwork, location=location)

    def test_writes_poster_fanart_and_thumb_named_after_the_folder(self):
        self.assertTrue(self._sync())
        base = 'smb://n/Heat (1995)/Heat (1995)-'
        self.assertEqual(stubs._FAKE_FILES[base + 'poster.jpg'], b'POSTER')
        self.assertEqual(stubs._FAKE_FILES[base + 'fanart.jpg'], b'FANART')
        self.assertEqual(stubs._FAKE_FILES[base + 'thumb.jpg'], b'POSTER')   # the thumbnail is the poster
        self.invalidate.assert_not_called()                                   # nothing was replaced

    def test_an_unchanged_url_with_an_unchanged_file_is_not_downloaded_again(self):
        self._sync()
        self.responses.clear()                  # a second download would KeyError
        self.assertFalse(self._sync_without_refilling())

    def _sync_without_refilling(self):
        return mas.sync_movie_art('Heat', 1995, self.ART, location=('smb://n/Heat (1995)/', 'Heat (1995)'))

    def test_a_replaced_file_has_its_texture_dropped(self):
        stubs._FAKE_FILES['smb://n/Heat (1995)/Heat (1995)-poster.jpg'] = b'OLD'
        self._sync()
        self.invalidate.assert_called_once_with('smb://n/Heat (1995)/Heat (1995)-poster.jpg')

    def test_no_artwork_or_no_folder_or_a_missing_type_writes_nothing(self):
        self.assertFalse(self._sync(artwork={}))
        self.assertFalse(self._sync(location=(None, None)))
        self.assertFalse(self._sync(location=('/', 'x')))        # a folder with no usable name
        self.assertTrue(self._sync(artwork={'poster': [{'url': 'http://c/p.jpg'}]}))   # fanart missing: poster + thumb still written
        self.assertNotIn('smb://n/Heat (1995)/Heat (1995)-fanart.jpg', stubs._FAKE_FILES)

    def test_the_location_is_looked_up_when_the_caller_has_none(self):
        found = ('smb://n/Heat (1995)/', 'Heat (1995)', 'x.mkv', False, 5)
        with mock.patch.object(mas, 'find_movie_location', return_value=found) as find:
            self.responses.update({'http://c/p.jpg': b'P', 'http://c/f.jpg': b'F'})
            self.assertTrue(mas.sync_movie_art('Heat', 1995, self.ART))
        find.assert_called_once_with('Heat', 1995)

    def test_download_and_write_failures(self):
        self.assertIsNone(mas._write_remote_file('smb://n/x.jpg', 'http://missing/'))       # download fails (KeyError inside urlopen)
        self.responses['http://c/p.jpg'] = b'data'
        with mock.patch.object(mas.xbmcvfs, 'File', side_effect=OSError('read-only')):
            self.assertIsNone(mas._write_remote_file('smb://n/x.jpg', 'http://c/p.jpg'))
        fake = mock.MagicMock()
        fake.write.return_value = False
        with mock.patch.object(mas.xbmcvfs, 'File', return_value=fake):
            self.assertIsNone(mas._write_remote_file('smb://n/x.jpg', 'http://c/p.jpg'))    # a silent VFS failure is a failure
        self.assertEqual(mas._write_remote_file('smb://n/x.jpg', 'http://c/p.jpg'), 4)

    def test_a_failed_write_is_not_recorded_as_synced(self):
        fake = mock.MagicMock()
        fake.write.return_value = False
        with mock.patch.object(mas.xbmcvfs, 'File', return_value=fake):
            self.assertFalse(self._sync())
        self.assertEqual(mas._read_art_sync_cache(), {})


class TestCaches(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()

    def test_art_cache_round_trip_and_corruption(self):
        mas._mark_art_synced('smb://n/a.jpg', 'http://c/a', 10)
        self.assertEqual(mas._read_art_sync_cache()['smb://n/a.jpg'], {'url': 'http://c/a', 'size': 10})
        stubs._FAKE_FILES[mas._ART_SYNC_CACHE_PATH] = b'garbage'
        self.assertEqual(mas._read_art_sync_cache(), {})
        with mock.patch.object(mas.xbmcvfs, 'File', side_effect=OSError('ro')):
            mas._write_art_sync_cache({'a': 1})

    def test_already_synced_needs_the_same_url_and_the_same_size(self):
        stubs._FAKE_FILES['smb://n/a.jpg'] = b'0123456789'
        mas._mark_art_synced('smb://n/a.jpg', 'http://c/a', 10)
        self.assertTrue(mas._already_synced('smb://n/a.jpg', 'http://c/a'))
        self.assertFalse(mas._already_synced('smb://n/a.jpg', 'http://c/other'))
        stubs._FAKE_FILES['smb://n/a.jpg'] = b'changed by kodi'
        self.assertFalse(mas._already_synced('smb://n/a.jpg', 'http://c/a'))
        self.assertFalse(mas._already_synced('smb://n/missing.jpg', 'http://c/a'))

    def test_source_listing_cache_survives_corruption(self):
        mas._write_source_listing_cache({'smb://n/': {'timestamp': 1, 'dirs': ['a']}})
        self.assertEqual(mas._read_source_listing_cache()['smb://n/']['dirs'], ['a'])
        stubs._FAKE_FILES[mas._SOURCE_LISTING_CACHE_PATH] = b'garbage'
        self.assertEqual(mas._read_source_listing_cache(), {})


if __name__ == '__main__':
    unittest.main()
