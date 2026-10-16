# -*- coding: utf-8 -*-
"""library_repair: the safety checks, backups, lock, orchestration and the fabricated-watched / scan helpers that the main test file
does not reach. Every test that writes uses a real temporary SQLite file."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import tests.kodi_stubs as stubs
import xbmc

from lib import library_repair as lr
from tests.test_library_repair import _build_fixture_db


class _Db(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'MyVideos131.db')
        _build_fixture_db(self.db, orphan_count=3)
        self.backups = os.path.join(self.tmp.name, 'backups')


class TestFindDb(_Db):
    def test_one_two_and_no_database(self):
        with mock.patch.object(lr.xbmcvfs, 'translatePath', return_value=self.tmp.name):
            self.assertEqual(os.path.basename(lr.find_video_db_path()), 'MyVideos131.db')
            open(os.path.join(self.tmp.name, 'MyVideos119.db'), 'wb').close()
            with self.assertRaises(lr.LibraryRepairError) as ctx:
                lr.find_video_db_path()
            self.assertEqual(ctx.exception.reason_code, 'multiple_db_files')
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        with mock.patch.object(lr.xbmcvfs, 'translatePath', return_value=empty.name):
            self.assertIsNone(lr.find_video_db_path())


class TestPreconditions(_Db):
    def _check(self, scanning=False, mysql=False, live=1):
        with mock.patch.object(lr, '_is_scanning', return_value=scanning), mock.patch.object(lr, '_is_mysql_library', return_value=mysql), \
                mock.patch.object(lr, '_live_show_count', return_value=live):
            lr.check_preconditions(self.db)

    def _code(self, **kw):
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            self._check(**kw)
        return ctx.exception.reason_code

    def test_a_healthy_database_passes(self):
        self._check()

    def test_each_refusal(self):
        self.assertEqual(self._code(scanning=True), 'scanning')
        self.assertEqual(self._code(mysql=True), 'mysql_library')
        self.assertEqual(self._code(live=None), 'live_check_failed')
        self.assertEqual(self._code(live=0), 'live_count_mismatch')       # Kodi says none, the file has one
        self.assertEqual(self._code(live=40), 'live_count_mismatch')      # wildly different

    def test_a_small_difference_is_tolerated(self):
        self._check(live=3)

    def test_scan_state_fails_closed(self):
        with mock.patch.object(lr.xbmc, 'getCondVisibility', side_effect=RuntimeError('x')):
            self.assertTrue(lr._is_scanning())
        with mock.patch.object(lr.xbmc, 'getCondVisibility', return_value=False):
            self.assertFalse(lr._is_scanning())

    def test_mysql_detection_fails_open(self):
        settings = os.path.join(self.tmp.name, 'advancedsettings.xml')
        with mock.patch.object(lr.xbmcvfs, 'translatePath', return_value=settings):
            self.assertFalse(lr._is_mysql_library())                                   # no file
            open(settings, 'w').write('<advancedsettings><videodatabase><type>MySQL</type></videodatabase></advancedsettings>')
            self.assertTrue(lr._is_mysql_library())
            open(settings, 'w').write('<advancedsettings><musicdatabase><type>mysql</type></musicdatabase></advancedsettings>')
            self.assertFalse(lr._is_mysql_library())                                   # only the music library
        with mock.patch.object(lr.xbmcvfs, 'translatePath', side_effect=RuntimeError('x')):
            self.assertFalse(lr._is_mysql_library())

    def test_schema_problems(self):
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            lr._verify_schema(os.path.join(self.tmp.name, 'no-such-dir', 'x.db'))
        self.assertEqual(ctx.exception.reason_code, 'db_unreachable')
        broken = os.path.join(self.tmp.name, 'broken.db')
        conn = sqlite3.connect(broken)
        conn.execute('CREATE TABLE tvshow (idShow INTEGER)')
        conn.commit()
        conn.close()
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            lr._verify_schema(broken)
        self.assertEqual(ctx.exception.reason_code, 'schema_mismatch')
        conn = sqlite3.connect(broken)
        self.assertEqual(lr._schema_version(conn), 'unknown')
        self.assertFalse(lr._table_exists(conn, 'episode'))
        self.assertTrue(lr._table_exists(conn, 'tvshow'))
        conn.close()

    def test_live_show_count(self):
        with mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps({'result': {'tvshows': [{}, {}]}})):
            self.assertEqual(lr._live_show_count(), 2)
        with mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps({'error': {}})):
            self.assertIsNone(lr._live_show_count())
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('x')):
            self.assertIsNone(lr._live_show_count())


class TestBackups(_Db):
    def test_backup_is_a_verified_copy(self):
        path = lr.create_backup(self.db, self.backups)
        self.assertRegex(os.path.basename(path), r'^MyVideos131\.db\.repair_backup_\d{8}_\d{6}$')
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM episode').fetchone()[0], 5)
        conn.close()

    def test_not_enough_space_refuses_before_writing(self):
        with mock.patch.object(lr.shutil, 'disk_usage', return_value=mock.Mock(free=1)):
            with self.assertRaises(lr.LibraryRepairError) as ctx:
                lr.create_backup(self.db, self.backups)
        self.assertEqual(ctx.exception.reason_code, 'insufficient_space')
        self.assertEqual(os.listdir(self.backups), [])

    def test_unknown_free_space_proceeds(self):
        with mock.patch.object(lr.shutil, 'disk_usage', side_effect=OSError('x')):
            lr._check_free_space(self.db, self.backups)

    def test_a_failed_copy_and_a_corrupt_backup_are_refused(self):
        with mock.patch.object(lr.sqlite3, 'connect', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(lr.LibraryRepairError) as ctx:
                lr.create_backup(self.db, self.backups)
        self.assertEqual(ctx.exception.reason_code, 'backup_failed')
        corrupt = os.path.join(self.tmp.name, 'corrupt.db')
        open(corrupt, 'wb').write(b'x' * 4096)
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            lr._verify_backup(corrupt)
        self.assertEqual(ctx.exception.reason_code, 'backup_corrupt')

    def test_pruning_keeps_the_newest_and_ignores_other_files(self):
        os.makedirs(self.backups)
        names = ['MyVideos131.db.repair_backup_2026010{0}_000000'.format(i) for i in range(1, 8)]
        for name in names + ['notes.txt']:
            open(os.path.join(self.backups, name), 'wb').close()
        removed = lr.prune_old_backups(self.backups, keep=5)
        self.assertEqual([os.path.basename(p) for p in removed], names[:2])
        self.assertIn('notes.txt', os.listdir(self.backups))
        self.assertEqual(lr.prune_old_backups(self.backups, keep=5), [])


class TestLock(_Db):
    def test_acquire_release_and_a_second_run_is_refused(self):
        lr._acquire_lock()
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            lr._acquire_lock()
        self.assertEqual(ctx.exception.reason_code, 'already_running')
        lr._release_lock()
        lr._acquire_lock()           # free again

    def test_a_stale_or_unreadable_lock_is_taken_over(self):
        stubs._FAKE_FILES[lr._LOCK_PATH] = json.dumps({'started_at': time.time() - lr._LOCK_STALE_SECONDS - 5}).encode()
        lr._acquire_lock()
        stubs._FAKE_FILES[lr._LOCK_PATH] = b'garbage'
        lr._acquire_lock()

    def test_release_never_raises(self):
        with mock.patch.object(lr.xbmcvfs, 'delete', side_effect=OSError('x')):
            lr._release_lock()


class TestOrchestration(_Db):
    def setUp(self):
        super().setUp()
        self.report = {'total_episodes': 3, 'groups': [], 'stale_shows': [], 'stuck_files': {'file_ids': []}}
        for name, new in (('find_video_db_path', mock.Mock(return_value=self.db)), ('check_preconditions', mock.Mock()),
                          ('detect_orphans', mock.Mock(return_value=dict(self.report))), ('detect_stale_shows', mock.Mock(return_value=['s'])),
                          ('detect_stuck_files', mock.Mock(return_value={'file_ids': [1]})), ('_backup_dir', mock.Mock(return_value=self.backups))):
            p = mock.patch.object(lr, name, new)
            p.start()
            self.addCleanup(p.stop)

    def test_preview_collects_all_three_reports_and_reports_progress_and_releases_the_lock(self):
        messages = []
        report = lr.preview(progress_callback=messages.append)
        self.assertEqual((report['stale_shows'], report['stuck_files']), (['s'], {'file_ids': [1]}))
        self.assertGreaterEqual(len(messages), 5)
        lr._acquire_lock()           # preview released it

    def test_preview_without_a_database_aborts_and_releases(self):
        lr.find_video_db_path.return_value = None
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            lr.preview()
        self.assertEqual(ctx.exception.reason_code, 'no_db_found')
        lr._acquire_lock()

    def test_a_failing_progress_callback_never_aborts_the_work(self):
        def boom(message):
            raise RuntimeError('ui gone')
        self.assertEqual(lr.preview(progress_callback=boom)['stale_shows'], ['s'])

    def test_prepare_keeps_the_lock_until_finish(self):
        db_path, report = lr.prepare_repair()
        self.assertEqual(db_path, self.db)
        with self.assertRaises(lr.LibraryRepairError):
            lr._acquire_lock()
        self.assertIsNone(lr.finish_repair(db_path, report, execute=False))
        lr._acquire_lock()

    def test_prepare_releases_the_lock_when_detection_fails(self):
        lr.check_preconditions.side_effect = lr.LibraryRepairError('scanning', 'busy')
        with self.assertRaises(lr.LibraryRepairError):
            lr.prepare_repair()
        lr._acquire_lock()

    def test_finish_runs_the_repairs_in_order_and_merges_their_results(self):
        db_path, report = lr.prepare_repair()
        with mock.patch.object(lr, 'run_repair', return_value={'aborted': False, 'deleted_episodes': 3, 'backup_path': 'b'}) as run, \
                mock.patch.object(lr, 'repair_stale_shows', return_value=['Show']) as stale, \
                mock.patch.object(lr, 'repair_stuck_files', return_value={'deleted_files': 2, 'aborted': False}) as stuck:
            result = lr.finish_repair(db_path, report, execute=True)
        run.assert_called_once()
        stale.assert_called_once_with(['s'], self.db)
        stuck.assert_called_once()
        self.assertEqual((result['repaired_stale_shows'], result['deleted_stuck_files'], result['stuck_files_aborted']), (['Show'], 2, False))
        lr._acquire_lock()

    def test_an_aborted_main_repair_skips_the_rest(self):
        db_path, report = lr.prepare_repair()
        with mock.patch.object(lr, 'run_repair', return_value={'aborted': True, 'abort_reason': 'x', 'backup_path': None}), \
                mock.patch.object(lr, 'repair_stale_shows') as stale, mock.patch.object(lr, 'repair_stuck_files') as stuck:
            result = lr.finish_repair(db_path, report, execute=True)
        stale.assert_not_called()
        stuck.assert_not_called()
        self.assertEqual((result['repaired_stale_shows'], result['deleted_stuck_files']), ([], 0))

    def test_fabricated_watched_failures_are_reported_not_raised(self):
        db_path, report = lr.prepare_repair()
        report['fabricated_watched'] = {'episodes': [{'episodeid': 1}]}
        with mock.patch.object(lr, 'run_repair', return_value={'aborted': False}), mock.patch.object(lr, 'repair_stale_shows', return_value=[]), \
                mock.patch.object(lr, 'repair_fabricated_watched', side_effect=lr.LibraryRepairError('chronicle_unreachable', 'Chronicle is down')):
            result = lr.finish_repair(db_path, report, execute=True)
        self.assertEqual(result['fabricated_watched_error'], 'Chronicle is down')

    def test_undo_restores_orphans_and_watched_marks(self):
        with mock.patch.object(lr, '_read_manifest', return_value={'episode_ids': [1]}), \
                mock.patch.object(lr, 'undo_last_repair', return_value={'aborted': False, 'restored_episodes': 1}) as undo, \
                mock.patch.object(lr, 'undo_fabricated_watched', return_value={'restored': 2, 'errors': 0}):
            result = lr.run_undo()
        undo.assert_called_once()
        self.assertEqual((result['restored_episodes'], result['restored_watched']['restored']), (1, 2))
        lr._acquire_lock()

    def test_undo_with_only_a_watched_manifest_does_not_touch_the_database(self):
        with mock.patch.object(lr, '_read_manifest', return_value=None), mock.patch.object(lr, 'peek_fabricated_manifest', return_value={'episodes': [1]}), \
                mock.patch.object(lr, 'undo_last_repair') as undo, mock.patch.object(lr, 'undo_fabricated_watched', return_value={'restored': 1, 'errors': 0}):
            result = lr.run_undo()
        undo.assert_not_called()
        self.assertEqual(result['restored_episodes'], 0)

    def test_undo_without_a_database_aborts_and_releases_the_lock(self):
        lr.find_video_db_path.return_value = None
        with mock.patch.object(lr, '_read_manifest', return_value={'episode_ids': [1]}):
            with self.assertRaises(lr.LibraryRepairError):
                lr.run_undo()
        lr._acquire_lock()


class TestFabricatedWatched(_Db):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(lr, '_backup_dir', return_value=self.backups)
        p.start()
        self.addCleanup(p.stop)

    def _episodes(self):
        stamp = '2026-01-01 10:00:00'
        return [{'episodeid': i, 'title': 'E{0}'.format(i), 'season': 1, 'episode': i, 'playcount': 1, 'lastplayed': stamp, 'file': '/tv/S01E0{0}.mkv'.format(i)}
                for i in (1, 2, 3)] + [{'episodeid': 4, 'playcount': 1, 'lastplayed': '2026-02-02 10:00:00'}, {'episodeid': 5, 'playcount': 0, 'lastplayed': ''}]

    def test_detection_groups_identical_timestamps_of_one_show(self):
        def rpc(method, params):
            if method == 'VideoLibrary.GetTVShows':
                return {'tvshows': [{'tvshowid': 1, 'title': 'Show'}, {'title': 'No id'}]}
            return {'episodes': self._episodes()}
        with mock.patch.object(lr, '_jsonrpc', side_effect=rpc):
            report = lr.detect_fabricated_watched()
        self.assertEqual([e['episodeid'] for e in report['episodes']], [1, 2, 3])
        self.assertEqual(report['groups'], [{'show_name': 'Show', 'count': 3, 'lastplayed': '2026-01-01 10:00:00'}])

    def test_jsonrpc_helper(self):
        with mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps({'result': 'OK'})):
            self.assertEqual(lr._jsonrpc('X', {}), 'OK')
        with mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps({'error': {}})):
            self.assertIsNone(lr._jsonrpc('X', {}))
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('x')):
            self.assertIsNone(lr._jsonrpc('X', {}))

    def _client(self, reachable=True, resets=True):
        client = mock.MagicMock()
        client.test_connection.return_value = (reachable, '' if reachable else 'refused')
        client.get_episode_details_by_file.side_effect = lambda name, season=None, episode=None: {'mediaItemId': 100 + episode}
        client.reset_watch_progress.return_value = resets
        return client

    def _flagged(self):
        return {'episodes': [{'episodeid': e['episodeid'], 'playcount': 1, 'lastplayed': e['lastplayed'], 'file': e.get('file'),
                              'season': 1, 'episode': e['episodeid']} for e in self._episodes()[:3]]}

    def test_chronicle_is_reset_first_then_kodi_and_the_undo_record_is_written_first(self):
        client = self._client()
        order = []
        client.reset_watch_progress.side_effect = lambda i: order.append(('chronicle', i)) or True
        with mock.patch.object(lr, '_is_scanning', return_value=False), \
                mock.patch.object(lr, '_jsonrpc', side_effect=lambda m, p: order.append(('kodi', p['episodeid'])) or {}):
            result = lr.repair_fabricated_watched(self._flagged(), self.backups, client=client)
        self.assertEqual(result, {'cleared': 3, 'chronicle_reset': 3, 'skipped': 0})
        self.assertEqual(order[:2], [('chronicle', 101), ('kodi', 1)])
        self.assertTrue(os.path.exists(lr._fabricated_manifest_path(self.backups)))

    def test_an_episode_whose_chronicle_reset_fails_is_left_untouched(self):
        client = self._client(resets=False)
        with mock.patch.object(lr, '_is_scanning', return_value=False), mock.patch.object(lr, '_jsonrpc', return_value={}) as rpc:
            result = lr.repair_fabricated_watched(self._flagged(), self.backups, client=client)
        self.assertEqual(result, {'cleared': 0, 'chronicle_reset': 0, 'skipped': 3})
        rpc.assert_not_called()

    def test_an_episode_chronicle_has_no_record_of_needs_no_chronicle_reset(self):
        client = self._client()
        client.get_episode_details_by_file.side_effect = lambda *a, **k: None
        with mock.patch.object(lr, '_is_scanning', return_value=False), mock.patch.object(lr, '_jsonrpc', return_value={}):
            result = lr.repair_fabricated_watched(self._flagged(), self.backups, client=client)
        self.assertEqual(result, {'cleared': 3, 'chronicle_reset': 0, 'skipped': 0})

    def test_refusals_change_nothing(self):
        self.assertEqual(lr.repair_fabricated_watched({'episodes': []}, self.backups), {'cleared': 0, 'chronicle_reset': 0, 'skipped': 0})
        with mock.patch.object(lr, '_is_scanning', return_value=True), self.assertRaises(lr.LibraryRepairError) as ctx:
            lr.repair_fabricated_watched(self._flagged(), self.backups, client=self._client())
        self.assertEqual(ctx.exception.reason_code, 'scanning')
        with mock.patch.object(lr, '_is_scanning', return_value=False), self.assertRaises(lr.LibraryRepairError) as ctx:
            lr.repair_fabricated_watched(self._flagged(), self.backups, client=self._client(reachable=False))
        self.assertEqual(ctx.exception.reason_code, 'chronicle_unreachable')
        with mock.patch.object(lr, '_is_scanning', return_value=False), mock.patch('builtins.open', side_effect=OSError('read-only')), \
                self.assertRaises(lr.LibraryRepairError) as ctx:
            lr.repair_fabricated_watched(self._flagged(), self.backups, client=self._client())
        self.assertEqual(ctx.exception.reason_code, 'manifest_write_failed')

    def test_undo_restores_marks_and_removes_the_manifest_only_when_everything_restored(self):
        os.makedirs(self.backups)
        manifest = {'created_at': 1, 'episodes': [{'episodeid': 1, 'playcount': 2, 'lastplayed': '2026-01-01 10:00:00'}, {'episodeid': 2}]}
        with open(lr._fabricated_manifest_path(self.backups), 'w') as f:
            json.dump(manifest, f)
        self.assertEqual(lr.peek_fabricated_manifest()['created_at'], 1)
        with mock.patch.object(lr, '_jsonrpc', side_effect=[{}, None]) as rpc:
            self.assertEqual(lr.undo_fabricated_watched(), {'restored': 1, 'errors': 1})
        self.assertTrue(os.path.exists(lr._fabricated_manifest_path(self.backups)))
        with mock.patch.object(lr, '_jsonrpc', return_value={}) as rpc:
            self.assertEqual(lr.undo_fabricated_watched(), {'restored': 2, 'errors': 0})
        self.assertFalse(os.path.exists(lr._fabricated_manifest_path(self.backups)))
        self.assertEqual(rpc.call_args_list[1][0][1], {'episodeid': 2, 'playcount': 0, 'lastplayed': ''})   # missing fields restore as unwatched

    def test_no_manifest_and_a_corrupt_manifest(self):
        self.assertIsNone(lr.peek_fabricated_manifest())
        self.assertEqual(lr.undo_fabricated_watched(), {'restored': 0, 'errors': 0})
        os.makedirs(self.backups)
        open(lr._fabricated_manifest_path(self.backups), 'w').write('garbage')
        self.assertIsNone(lr.peek_fabricated_manifest())


class TestSourcesAndScan(_Db):
    def test_own_source_directories(self):
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO path (idPath, strPath, strScraper, idParentPath) VALUES (50, "/tv/Other/", "script.chronicle.scraper.movie", NULL)')
        conn.commit()
        conn.close()
        with mock.patch.object(lr, 'ADDON') as addon:
            addon.getAddonInfo.return_value = 'script.chronicle.scraper.tv'
            self.assertEqual(sorted(lr.own_source_directories(self.db)), ['/tv/A Gone Show/Season 01/', '/tv/A Real Show/'])
            self.assertEqual(lr.own_source_directories(os.path.join(self.tmp.name, 'missing', 'x.db')), [])

    def test_trigger_scan_reports_whether_any_scan_was_accepted(self):
        responses = iter([json.dumps({'error': {}}), json.dumps({'result': 'OK'})])
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=lambda r: next(responses)):
            self.assertTrue(lr.trigger_scan(['a', 'b']))
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('x')):
            self.assertFalse(lr.trigger_scan(['a']))
        self.assertFalse(lr.trigger_scan([]))


if __name__ == '__main__':
    unittest.main()


class TestRepairStuckFiles(_Db):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.tmp.name, 'Stuck.db')
        _build_fixture_db(self.db, orphan_count=0, extra_sql="""
            INSERT INTO files (idFile, idPath, strFilename) VALUES (300, 1, 'stuck1.mkv'), (301, 1, 'stuck2.mkv'), (302, 1, 'shared.mkv');
            INSERT INTO bookmark (idFile, timeInSeconds) VALUES (300, 5.0);
            INSERT INTO streamdetails (idFile, iStreamType) VALUES (301, 0);
            INSERT INTO movie (idMovie, idFile) VALUES (1, 302);
        """)

    def _files(self):
        conn = sqlite3.connect(self.db)
        try:
            return sorted(r[0] for r in conn.execute('SELECT idFile FROM files WHERE idFile >= 300'))
        finally:
            conn.close()

    def _repair(self, ids=(300, 301, 302), scanning=False):
        with mock.patch.object(lr, '_is_scanning', return_value=scanning):
            return lr.repair_stuck_files(self.db, {'file_ids': list(ids)}, self.backups)

    def test_nothing_to_do_makes_no_backup(self):
        result = lr.repair_stuck_files(self.db, {'file_ids': []}, self.backups)
        self.assertEqual((result['deleted_files'], result['backup_path'], result['aborted']), (0, None, False))
        self.assertFalse(os.path.exists(self.backups))

    def test_stuck_rows_are_removed_after_a_backup_and_a_file_used_by_a_movie_is_kept(self):
        result = self._repair()
        self.assertEqual((result['deleted_files'], result['skipped_shared_files'], result['aborted']), (2, 1, False))
        self.assertEqual((result['deleted_bookmark'], result['deleted_streamdetails']), (1, 1))
        self.assertEqual(self._files(), [302])
        self.assertTrue(os.path.exists(result['backup_path']))

    def test_a_scan_that_starts_mid_run_rolls_everything_back(self):
        result = self._repair(scanning=True)
        self.assertEqual((result['aborted'], result['abort_reason']), (True, 'scan_started_during_repair'))
        self.assertEqual(self._files(), [300, 301, 302])

    def test_an_unexpected_error_rolls_back(self):
        with mock.patch.object(lr, '_find_shared_file_ids', side_effect=RuntimeError('boom')):
            result = self._repair()
        self.assertEqual(result['abort_reason'], 'unexpected_error')
        self.assertEqual(self._files(), [300, 301, 302])


class TestUndoLastRepair(_Db):
    def _repaired(self):
        orphan_ids, file_ids = _build_fixture_db(os.path.join(self.tmp.name, 'Undo.db'), orphan_count=4)
        self.db = os.path.join(self.tmp.name, 'Undo.db')
        with mock.patch.object(lr, '_is_scanning', return_value=False):
            report = lr.detect_orphans(self.db)
            result = lr.run_repair(self.db, report, self.backups)
        self.assertFalse(result['aborted'])
        return orphan_ids

    def _episode_ids(self):
        conn = sqlite3.connect(self.db)
        try:
            return sorted(r[0] for r in conn.execute('SELECT idEpisode FROM episode'))
        finally:
            conn.close()

    def test_nothing_to_undo(self):
        with self.assertRaises(lr.LibraryRepairError) as ctx:
            lr.undo_last_repair(self.db, self.backups)
        self.assertEqual(ctx.exception.reason_code, 'no_manifest')

    def test_the_removed_rows_come_back_and_nothing_else_changes(self):
        orphans = self._repaired()
        self.assertEqual(self._episode_ids(), [101, 102])
        with mock.patch.object(lr, '_backup_dir', return_value=self.backups):
            self.assertEqual(lr.peek_last_manifest()['episode_ids'], orphans)
        with mock.patch.object(lr, '_is_scanning', return_value=False):
            result = lr.undo_last_repair(self.db, self.backups)
        self.assertEqual((result['aborted'], result['restored_episodes']), (False, 4))
        self.assertEqual(self._episode_ids(), [101, 102] + orphans)

    def test_a_scan_that_starts_mid_undo_rolls_back(self):
        self._repaired()
        with mock.patch.object(lr, '_is_scanning', return_value=True):
            result = lr.undo_last_repair(self.db, self.backups)
        self.assertEqual(result['abort_reason'], 'scan_started_during_undo')
        self.assertEqual(self._episode_ids(), [101, 102])

    def test_an_unexpected_error_rolls_back(self):
        self._repaired()
        with mock.patch.object(lr, '_insert_rows', side_effect=RuntimeError('boom')):
            result = lr.undo_last_repair(self.db, self.backups)
        self.assertEqual(result['abort_reason'], 'unexpected_error')
        self.assertEqual(self._episode_ids(), [101, 102])
