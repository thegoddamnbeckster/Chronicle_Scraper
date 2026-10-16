# -*- coding: utf-8 -*-
"""collection_sync: filling and refreshing a movie set's art folder, retries, texture invalidation and the unreachable-folder alert."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from lib import collection_sync as cs

_REAL_SHOULD_ALERT = cs._should_alert   # captured before any test patches it

BASE = 'smb://nas/Movie Collections'
FOLDER = BASE + '/Heat Collection/'


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        stubs._FAKE_DIRS.add(BASE)
        self.rpc_calls = []
        self.sets = []
        p = mock.patch.object(cs.xbmc, 'executeJSONRPC', side_effect=self._rpc)
        p.start()
        self.addCleanup(p.stop)
        for target, new in (('get_setting_value', mock.Mock(return_value=BASE)), ('_should_alert', mock.Mock(return_value=True))):
            q = mock.patch.object(cs, target, new)
            setattr(self, target, q.start())
            self.addCleanup(q.stop)
        r = mock.patch.object(cs.xbmc, 'sleep')
        r.start()
        self.addCleanup(r.stop)
        self.dialog = mock.MagicMock()
        d = mock.patch.object(cs.xbmcgui, 'Dialog', return_value=self.dialog)
        d.start()
        self.addCleanup(d.stop)
        self.textures = []
        self.download = mock.patch.object(cs.urllib.request, 'urlopen', side_effect=lambda url, timeout=None: _Resp(b'IMG:' + url.encode()))
        self.urlopen = self.download.start()
        self.addCleanup(self.download.stop)

    def _rpc(self, raw):
        req = json.loads(raw)
        self.rpc_calls.append(req)
        method = req['method']
        if method == 'VideoLibrary.GetMovieSets':
            return json.dumps({'result': {'sets': self.sets}})
        if method == 'Textures.GetTextures':
            return json.dumps({'result': {'textures': self.textures}})
        return json.dumps({'result': 'OK'})

    def _methods(self):
        return [c['method'] for c in self.rpc_calls]


class TestSyncCollectionArt(_Base):
    COLLECTION = {'name': 'Heat Collection', 'posterUrl': 'http://c/p.jpg', 'backdropUrl': 'http://c/f.jpg', 'logoUrl': None,
                  'pinnedSlots': ['poster_url']}

    def test_no_name_or_no_configured_folder_does_nothing(self):
        cs.sync_collection_art({})
        self.get_setting_value.return_value = ''
        cs.sync_collection_art(self.COLLECTION)
        self.urlopen.assert_not_called()
        self.assertEqual(self.rpc_calls, [])

    def test_missing_folder_is_created_and_missing_art_downloaded(self):
        cs.sync_collection_art(self.COLLECTION)
        self.assertEqual(stubs._FAKE_FILES[FOLDER + 'poster.jpg'], b'IMG:http://c/p.jpg')
        self.assertEqual(stubs._FAKE_FILES[FOLDER + 'fanart.jpg'], b'IMG:http://c/f.jpg')
        self.assertNotIn(FOLDER + 'clearlogo.png', stubs._FAKE_FILES)   # no logo in Chronicle: nothing written
        self.assertNotIn('Textures.RemoveTexture', self._methods())     # new files need no cache invalidation
        self.assertNotIn('VideoLibrary.SetMovieSetDetails', self._methods())

    def test_existing_art_is_overwritten_and_its_texture_dropped_and_set_art_reregistered(self):
        stubs._FAKE_DIRS.add(FOLDER)
        stubs._FAKE_FILES[FOLDER + 'poster.jpg'] = b'OLD'
        self.sets = [{'setid': 4, 'label': 'Heat Collection', 'art': {}}]
        self.textures = [{'textureid': 11, 'url': FOLDER + 'poster.jpg'}, {'textureid': 12, 'url': 'smb://other/poster.jpg'}]
        cs.sync_collection_art(self.COLLECTION)
        self.assertEqual(stubs._FAKE_FILES[FOLDER + 'poster.jpg'], b'IMG:http://c/p.jpg')
        removed = [c['params']['textureid'] for c in self.rpc_calls if c['method'] == 'Textures.RemoveTexture']
        self.assertEqual(removed, [11])   # only this file's texture, never another folder's poster.jpg
        set_call = [c for c in self.rpc_calls if c['method'] == 'VideoLibrary.SetMovieSetDetails'][0]
        self.assertEqual(set_call['params'], {'setid': 4, 'art': {'poster': FOLDER + 'poster.jpg'}})

    def test_download_failure_is_not_a_folder_problem(self):
        self.urlopen.side_effect = OSError('404')
        cs.sync_collection_art(self.COLLECTION)
        self.dialog.notification.assert_not_called()

    def test_write_failure_alerts_after_the_retries(self):
        with mock.patch.object(cs.xbmcvfs, 'File', side_effect=OSError('read-only share')):
            cs.sync_collection_art({'name': 'Heat Collection', 'posterUrl': 'http://c/p.jpg'})
        self.assertEqual(self.urlopen.call_count, cs._FOLDER_RETRY_ATTEMPTS)
        self.dialog.notification.assert_called_once()
        self.assertIn('Movie Collections', self.dialog.notification.call_args[0][1])

    def test_unreachable_folder_retries_mkdirs_then_alerts_without_redundant_downloads(self):
        with mock.patch.object(cs.xbmcvfs, 'mkdirs', return_value=False) as mkdirs:
            cs.sync_collection_art(self.COLLECTION)
        self.assertEqual(mkdirs.call_count, cs._FOLDER_RETRY_ATTEMPTS)
        self.urlopen.assert_not_called()
        self.dialog.notification.assert_called_once()

    def test_a_flaky_share_that_recovers_is_not_alerted(self):
        results = iter([False, False, True])
        with mock.patch.object(cs.xbmcvfs, 'mkdirs', side_effect=lambda p: next(results)):
            cs.sync_collection_art(self.COLLECTION)
        self.dialog.notification.assert_not_called()

    def test_falsy_write_return_counts_as_a_failed_write(self):
        fake = mock.MagicMock()
        fake.write.return_value = False
        with mock.patch.object(cs.xbmcvfs, 'File', return_value=fake):
            self.assertEqual(cs._write_remote_file(FOLDER + 'poster.jpg', 'http://c/p.jpg'), 'write_failed')
        fake.write.return_value = None   # some Kodi builds return nothing: that is success
        with mock.patch.object(cs.xbmcvfs, 'File', return_value=fake):
            self.assertEqual(cs._write_remote_file(FOLDER + 'poster.jpg', 'http://c/p.jpg'), 'ok')


class TestRepairStaleArt(_Base):
    def test_dead_registered_art_is_repointed_when_the_folder_has_the_file(self):
        gone = 'image://' + 'smb%3a%2f%2fold-nas%2fSets%2fHeat%2fposter.jpg' + '/'
        self.sets = [{'setid': 4, 'label': 'Heat Collection', 'art': {'poster': gone, 'fanart': 'image://http%3a%2f%2fx%2ff.jpg/'}}]
        stubs._FAKE_FILES[FOLDER + 'poster.jpg'] = b'x'
        cs._repair_stale_set_art('Heat Collection', FOLDER)
        set_call = [c for c in self.rpc_calls if c['method'] == 'VideoLibrary.SetMovieSetDetails'][0]
        self.assertEqual(set_call['params']['art'], {'poster': FOLDER + 'poster.jpg'})   # the remote fanart URL is left alone

    def test_nothing_to_repair_makes_no_write(self):
        stubs._FAKE_FILES['smb://nas/ok/poster.jpg'] = b'x'
        self.sets = [{'setid': 4, 'label': 'Heat Collection', 'art': {'poster': 'image://smb%3a%2f%2fnas%2fok%2fposter.jpg/'}}]
        cs._repair_stale_set_art('Heat Collection', FOLDER)
        self.assertNotIn('VideoLibrary.SetMovieSetDetails', self._methods())

    def test_unknown_set_is_ignored(self):
        cs._repair_stale_set_art('Nope', FOLDER)
        self.assertEqual(self._methods(), ['VideoLibrary.GetMovieSets'])


class TestHelpers(_Base):
    def test_decode_local_image_path(self):
        # credentials embedded in the registered path are stripped, matching how the rest of the add-on references shares
        self.assertEqual(cs._decode_local_image_path('image://smb%3a%2f%2fuser%3apw%40host%2fSets%2fposter.jpg/'), 'smb://host/Sets/poster.jpg')
        self.assertIsNone(cs._decode_local_image_path('http://x/p.jpg'))
        self.assertIsNone(cs._decode_local_image_path('image://http%3a%2f%2fx%2fp.jpg/'))

    def test_find_set_handles_rpc_failure(self):
        with mock.patch.object(cs.xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertEqual(cs._find_set('x'), (None, None))
        self.sets = [{'setid': 1, 'label': 'A', 'art': None}]
        self.assertEqual(cs._find_set('A'), (1, {}))

    def test_set_art_tolerates_failures(self):
        with mock.patch.object(cs.xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            cs._set_movie_set_art(1, {'poster': 'x'})
        with mock.patch.object(cs.xbmc, 'executeJSONRPC', return_value='{"error": {}}'):
            cs._set_movie_set_art(1, {'poster': 'x'})

    def test_invalidate_texture_tolerates_failures(self):
        with mock.patch.object(cs.xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            cs._invalidate_texture(FOLDER + 'poster.jpg')

    def test_alert_is_throttled_by_a_marker_file(self):
        marker = 'special://profile/addon_data/x/collection_folder_alert.marker'
        with mock.patch.object(cs.xbmcvfs, 'translatePath', return_value='special://profile/addon_data/x/'):
            self.assertTrue(_REAL_SHOULD_ALERT())
            self.assertIn(marker, stubs._FAKE_FILES)
            self.assertFalse(_REAL_SHOULD_ALERT())
            stubs._FAKE_FILES[marker] = b'0'   # an old marker: long past the suppression window
            self.assertTrue(_REAL_SHOULD_ALERT())
            stubs._FAKE_FILES[marker] = b'garbage'
            self.assertTrue(_REAL_SHOULD_ALERT())


if __name__ == '__main__':
    unittest.main()
