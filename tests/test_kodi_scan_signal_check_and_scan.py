# -*- coding: utf-8 -*-
"""kodi_scan_signal.check_and_scan(): when this device scans on Chronicle's say-so, the throttle, and the source-folder lookup."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from lib import kodi_scan_signal as sig


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.client = mock.MagicMock()
        self.client.is_scan_needed.return_value = True
        self.addon = mock.MagicMock()
        self.addon.getAddonInfo.side_effect = lambda k: {'id': 'script.chronicle.scraper.movie', 'profile': 'special://profile/addon_data/x/'}[k]
        for target, new in (('ChronicleClient', mock.Mock(return_value=self.client)), ('ADDON', self.addon)):
            p = mock.patch.object(sig, target, new)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(sig.xbmcvfs, 'translatePath', side_effect=lambda path: path)
        p.start()
        self.addCleanup(p.stop)


class TestThrottleMarker(_Base):
    def test_no_marker_means_not_recently_triggered(self):
        self.assertFalse(sig._scan_recently_triggered())

    def test_marker_round_trip_and_expiry(self):
        sig._mark_scan_triggered()
        self.assertTrue(sig._scan_recently_triggered())
        path = sig._marker_path()
        stubs._FAKE_FILES[path] = str(time.time() - sig._MIN_SECONDS_BETWEEN_TRIGGERED_SCANS - 5).encode()
        self.assertFalse(sig._scan_recently_triggered())

    def test_unreadable_marker_never_blocks_a_scan(self):
        stubs._FAKE_FILES[sig._marker_path()] = b'garbage'
        self.assertFalse(sig._scan_recently_triggered())

    def test_marker_write_failure_is_swallowed(self):
        with mock.patch.object(sig.xbmcvfs, 'File', side_effect=OSError('read-only')):
            sig._mark_scan_triggered()


class TestOwnSourceDirectories(_Base):
    def _db(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, 'MyVideos131.db')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE path (idPath INTEGER PRIMARY KEY, strPath TEXT, strScraper TEXT, idParentPath INTEGER)')
        conn.executemany('INSERT INTO path (strPath, strScraper, idParentPath) VALUES (?, ?, ?)', rows)
        conn.commit()
        conn.close()
        return tmp.name

    def test_only_this_addons_top_level_sources(self):
        folder = self._db([
            ('smb://nas/Movies/', 'script.chronicle.scraper.movie', None),
            ('smb://nas/Movies/Heat/', 'script.chronicle.scraper.movie', 1),      # a sub-folder Kodi recorded: not a source
            ('smb://nas/TV/', 'script.chronicle.scraper.tv', None),                # another add-on's source
            ('smb://nas/Movies/', 'script.chronicle.scraper.movie', None),         # duplicate row
            ('smb://nas/More/', 'script.chronicle.scraper.movie', None)])
        with mock.patch.object(sig.xbmcvfs, 'translatePath', return_value=folder):
            self.assertEqual(sorted(sig.find_own_source_directories()), ['smb://nas/More/', 'smb://nas/Movies/'])

    def test_picks_the_newest_schema_database(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ('MyVideos119.db', 'MyVideos131.db', 'MyMusic82.db'):
            open(os.path.join(tmp.name, name), 'wb').close()
        with mock.patch.object(sig.xbmcvfs, 'translatePath', return_value=tmp.name):
            self.assertEqual(os.path.basename(sig._find_video_db_path()), 'MyVideos131.db')

    def test_no_database_or_unreadable_database_is_an_empty_list_not_a_guess(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        with mock.patch.object(sig.xbmcvfs, 'translatePath', return_value=empty.name):
            self.assertIsNone(sig._find_video_db_path())
            self.assertEqual(sig.find_own_source_directories(), [])
        open(os.path.join(empty.name, 'MyVideos131.db'), 'wb').write(b'not a database')
        with mock.patch.object(sig.xbmcvfs, 'translatePath', return_value=empty.name):
            self.assertEqual(sig.find_own_source_directories(), [])


class TestCheckAndScan(_Base):
    def setUp(self):
        super().setUp()
        self.rpc = mock.Mock(return_value=json.dumps({'result': 'OK'}))
        p = mock.patch.object(sig.xbmc, 'executeJSONRPC', self.rpc)
        p.start()
        self.addCleanup(p.stop)
        self.dirs = mock.Mock(return_value=['smb://nas/Movies/', 'smb://nas/More/'])
        q = mock.patch.object(sig, 'find_own_source_directories', self.dirs)
        q.start()
        self.addCleanup(q.stop)

    def _scans(self):
        return [json.loads(c[0][0]) for c in self.rpc.call_args_list]

    def test_nothing_new_does_nothing(self):
        self.client.is_scan_needed.return_value = False
        sig.check_and_scan()
        self.rpc.assert_not_called()
        self.client.acknowledge_scan_needed.assert_not_called()

    def test_scans_each_own_source_quietly_then_acknowledges_and_throttles(self):
        sig.check_and_scan()
        scans = self._scans()
        self.assertEqual([s['params'] for s in scans], [{'directory': 'smb://nas/Movies/', 'showdialogs': False},
                                                       {'directory': 'smb://nas/More/', 'showdialogs': False}])
        self.client.acknowledge_scan_needed.assert_called_once()
        self.assertTrue(sig._scan_recently_triggered())

    def test_a_recent_scan_blocks_another_without_acknowledging(self):
        sig._mark_scan_triggered()
        sig.check_and_scan()
        self.rpc.assert_not_called()
        self.client.acknowledge_scan_needed.assert_not_called()

    def test_no_own_sources_acknowledges_so_the_device_stops_being_asked(self):
        self.dirs.return_value = []
        sig.check_and_scan()
        self.rpc.assert_not_called()
        self.client.acknowledge_scan_needed.assert_called_once()

    def test_every_scan_failing_leaves_the_signal_due_but_still_throttles(self):
        self.rpc.side_effect = [RuntimeError('rpc down'), json.dumps({'error': {'message': 'busy'}})]
        sig.check_and_scan()
        self.client.acknowledge_scan_needed.assert_not_called()
        self.assertTrue(sig._scan_recently_triggered())

    def test_one_accepted_scan_is_enough_to_acknowledge(self):
        self.rpc.side_effect = [json.dumps({'error': {'message': 'busy'}}), json.dumps({'result': 'OK'})]
        sig.check_and_scan()
        self.client.acknowledge_scan_needed.assert_called_once()


class TestStartupSettingAndClaim(_Base):
    def test_native_startup_scan_setting(self):
        with mock.patch.object(sig.xbmc, 'executeJSONRPC', return_value=json.dumps({'result': {'value': True}})):
            self.assertTrue(sig._read_native_startup_scan_setting())
        with mock.patch.object(sig.xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertIsNone(sig._read_native_startup_scan_setting())
        with mock.patch.object(sig.xbmc, 'executeJSONRPC', return_value=json.dumps({'error': {}})):
            self.assertIsNone(sig._read_native_startup_scan_setting())

    def test_claim_marker_round_trip(self):
        self.assertIsNone(sig._read_startup_followup_marker())
        sig._claim_startup_followup()
        self.assertAlmostEqual(sig._read_startup_followup_marker(), time.time(), delta=5)
        stubs._FAKE_FILES[sig._STARTUP_FOLLOWUP_MARKER_PATH] = b'garbage'
        self.assertIsNone(sig._read_startup_followup_marker())

    def test_claim_write_failure_is_swallowed(self):
        with mock.patch.object(sig.xbmcvfs, 'File', side_effect=OSError('read-only')):
            sig._claim_startup_followup()


if __name__ == '__main__':
    unittest.main()
