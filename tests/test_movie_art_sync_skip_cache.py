# -*- coding: utf-8 -*-
"""
Regression tests for lib/movie_art_sync.py's art-sync skip cache (_already_synced/
_mark_art_synced) -- added (2026-09-18) after the same live scan that surfaced the
source-listing bottleneck also showed sync_movie_art unconditionally re-downloading
poster/fanart from their remote CDN URLs on every single movie, every single scan, even when
nothing had changed since the last successful sync. See that function's own module-level doc
for why file SIZE (not mere existence) is the signal used to decide "safe to skip."
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import movie_art_sync


def _seed_local_file(path, data):
    import xbmcvfs
    f = xbmcvfs.File(path, 'w')
    f.write(bytearray(data))
    f.close()


class TestAlreadySynced(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_true_when_url_and_local_file_size_both_match(self):
        _seed_local_file('smb://nas/movie/poster.jpg', b'x' * 100)
        movie_art_sync._mark_art_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg', 100)

        self.assertTrue(movie_art_sync._already_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg'))

    def test_false_when_never_synced_before(self):
        self.assertFalse(movie_art_sync._already_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg'))

    def test_false_when_url_changed(self):
        _seed_local_file('smb://nas/movie/poster.jpg', b'x' * 100)
        movie_art_sync._mark_art_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg', 100)

        self.assertFalse(movie_art_sync._already_synced('smb://nas/movie/poster.jpg', 'http://example/DIFFERENT.jpg'))

    def test_false_when_local_file_is_missing(self):
        # Marked synced, but the file was never actually seeded locally (or was deleted) --
        # _local_file_size can't confirm it, so this must not be trusted as still up to date.
        movie_art_sync._mark_art_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg', 100)

        self.assertFalse(movie_art_sync._already_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg'))

    def test_false_when_local_file_size_no_longer_matches(self):
        # Simulates Kodi having silently reverted the local file to something else since --
        # the whole reason this module always overwrote unconditionally in the first place.
        _seed_local_file('smb://nas/movie/poster.jpg', b'x' * 55)
        movie_art_sync._mark_art_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg', 100)

        self.assertFalse(movie_art_sync._already_synced('smb://nas/movie/poster.jpg', 'http://example/a.jpg'))


class TestSyncMovieArtSkipsRedundantDownloads(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def _fake_urlopen(self, data=b'fake-image-bytes'):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=data)))
        cm.__exit__ = MagicMock(return_value=False)
        return MagicMock(return_value=cm)

    def test_second_sync_of_unchanged_artwork_never_calls_urlopen(self):
        artwork = {'poster': [{'url': 'http://example/poster.jpg', 'source': 'tmdb'}]}
        location = ('smb://nas/Movie (2020)/', 'Movie (2020)')

        with patch('urllib.request.urlopen', self._fake_urlopen(b'x' * 42)) as mock_urlopen:
            movie_art_sync.sync_movie_art('Movie', 2020, artwork, location=location)
        self.assertEqual(mock_urlopen.call_count, 1)

        with patch('urllib.request.urlopen', self._fake_urlopen(b'x' * 42)) as mock_urlopen_2:
            movie_art_sync.sync_movie_art('Movie', 2020, artwork, location=location)
        self.assertEqual(mock_urlopen_2.call_count, 0)

    def test_changed_artwork_url_still_downloads(self):
        location = ('smb://nas/Movie (2020)/', 'Movie (2020)')

        with patch('urllib.request.urlopen', self._fake_urlopen(b'x' * 42)):
            movie_art_sync.sync_movie_art(
                'Movie', 2020, {'poster': [{'url': 'http://example/poster-v1.jpg', 'source': 'tmdb'}]},
                location=location)

        with patch('urllib.request.urlopen', self._fake_urlopen(b'y' * 99)) as mock_urlopen:
            movie_art_sync.sync_movie_art(
                'Movie', 2020, {'poster': [{'url': 'http://example/poster-v2.jpg', 'source': 'tmdb'}]},
                location=location)
        self.assertEqual(mock_urlopen.call_count, 1)


if __name__ == '__main__':
    unittest.main()
