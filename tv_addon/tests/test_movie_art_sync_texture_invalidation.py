# -*- coding: utf-8 -*-
"""
Regression tests for lib/movie_art_sync.py invalidating Kodi's texture cache when it
overwrites an existing local art file.

Root-caused (2026-09-22): sync_movie_art() overwrites a movie's local poster/fanart file in
place when Chronicle's pick changes, but never told Kodi to drop its own cached copy of that
same path. Kodi's texture cache only re-hashes a local file about once a day, so the new art
sat correctly on disk while Kodi kept rendering the stale cached image -- reported live as "Kodi
art not syncing with Chronicle's refresh." collection_sync.py already solved this exact problem
for movie-set art via a Textures.RemoveTexture call after every overwrite
(_invalidate_texture); this ports the same fix to a plain movie's own poster/fanart, which is
the far more common path and never had it.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import movie_art_sync


class TestTextureInvalidationOnOverwrite(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def _fake_urlopen(self, data=b'fake-image-bytes'):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=data)))
        cm.__exit__ = MagicMock(return_value=False)
        return MagicMock(return_value=cm)

    def test_overwriting_an_existing_file_invalidates_its_cached_texture(self):
        location = ('smb://nas/Movie (2020)/', 'Movie (2020)')

        with patch('urllib.request.urlopen', self._fake_urlopen(b'x' * 42)):
            movie_art_sync.sync_movie_art(
                'Movie', 2020, {'poster': [{'url': 'http://example/poster-v1.jpg', 'source': 'tmdb'}]},
                location=location)

        with patch('urllib.request.urlopen', self._fake_urlopen(b'y' * 99)), \
             patch('lib.movie_art_sync._invalidate_texture') as mock_invalidate:
            movie_art_sync.sync_movie_art(
                'Movie', 2020, {'poster': [{'url': 'http://example/poster-v2.jpg', 'source': 'tmdb'}]},
                location=location)

        invalidated = {c.args[0] for c in mock_invalidate.call_args_list}
        self.assertIn('smb://nas/Movie (2020)/Movie (2020)-poster.jpg', invalidated)
        self.assertIn('smb://nas/Movie (2020)/Movie (2020)-thumb.jpg', invalidated)

    def test_first_time_fill_never_invalidates_a_texture(self):
        # Nothing was cached for a file that didn't exist yet -- invalidating would be a
        # no-op at best, and each call is a live JSON-RPC round trip not worth making.
        location = ('smb://nas/Movie (2020)/', 'Movie (2020)')

        with patch('urllib.request.urlopen', self._fake_urlopen(b'x' * 42)), \
             patch('lib.movie_art_sync._invalidate_texture') as mock_invalidate:
            movie_art_sync.sync_movie_art(
                'Movie', 2020, {'poster': [{'url': 'http://example/poster.jpg', 'source': 'tmdb'}]},
                location=location)

        mock_invalidate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
