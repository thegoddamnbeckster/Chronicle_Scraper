# -*- coding: utf-8 -*-
"""The persisted media-id cache."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from lib import media_id_cache


class TestMediaIdCache(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()

    def test_keys(self):
        self.assertEqual((media_id_cache.movie_key(5), media_id_cache.tvshow_key(7)), ('movie:5', 'tvshow:7'))

    def test_missing_cache_is_empty(self):
        self.assertEqual(media_id_cache.load(), {})

    def test_round_trip(self):
        media_id_cache.save({'movie:5': 123, 'tvshow:7': 456})
        self.assertEqual(media_id_cache.load(), {'movie:5': 123, 'tvshow:7': 456})

    def test_corrupt_or_empty_cache_is_not_fatal(self):
        for raw in (b'{not json', b''):
            stubs._FAKE_FILES[media_id_cache._CACHE_PATH] = raw
            self.assertEqual(media_id_cache.load(), {}, raw)

    def test_save_failure_is_swallowed(self):
        with mock.patch.object(media_id_cache.xbmcvfs, 'File', side_effect=OSError('read-only')):
            media_id_cache.save({'a': 1})


if __name__ == '__main__':
    unittest.main()
