# -*- coding: utf-8 -*-
"""collection_art_sync.run() and the persisted media-id cache."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from lib import collection_art_sync, media_id_cache


class TestCollectionArtSync(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        for target, new in (('ChronicleClient', mock.Mock(return_value=self.client)), ('collection_sync', mock.MagicMock())):
            p = mock.patch.object(collection_art_sync, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.sync = collection_art_sync.collection_sync.sync_collection_art
        p = mock.patch.object(collection_art_sync.xbmc, 'sleep')
        p.start()
        self.addCleanup(p.stop)

    def test_visits_every_collection_and_reports_progress(self):
        self.client.get_all_collections.return_value = [{'name': 'A'}, {'name': 'B'}, {}]
        progress = mock.Mock()
        result = collection_art_sync.run(progress_callback=progress)
        self.assertEqual(result, {'collections': 3, 'errors': 0, 'cancelled': False})
        self.assertEqual([c[0] for c in progress.call_args_list], [(0, 3, 'A'), (1, 3, 'B'), (2, 3, '?')])
        self.assertEqual(self.sync.call_count, 3)

    def test_one_failure_does_not_stop_the_pass(self):
        self.client.get_all_collections.return_value = [{'name': 'A'}, {'name': 'B'}]
        self.sync.side_effect = [RuntimeError('share offline'), None]
        self.assertEqual(collection_art_sync.run(), {'collections': 1, 'errors': 1, 'cancelled': False})

    def test_cancellation_stops_between_collections(self):
        self.client.get_all_collections.return_value = [{'name': 'A'}, {'name': 'B'}, {'name': 'C'}]
        flags = iter([False, True])
        result = collection_art_sync.run(is_cancelled=lambda: next(flags))
        self.assertEqual(result, {'collections': 1, 'errors': 0, 'cancelled': True})

    def test_nothing_in_chronicle(self):
        self.client.get_all_collections.return_value = []
        self.assertEqual(collection_art_sync.run(), {'collections': 0, 'errors': 0, 'cancelled': False})


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
