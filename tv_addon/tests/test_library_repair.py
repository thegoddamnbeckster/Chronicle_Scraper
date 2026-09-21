# -*- coding: utf-8 -*-
"""
Tests for lib/library_repair.py -- the "Repair Stuck Episodes" feature that finds and removes
orphaned `episode` rows (idShow pointing at a show that no longer exists) directly in Kodi's own
live video database. See that module's own docstring for the full root-cause explanation and the
non-negotiable safety rules every function here is written under.

Since sqlite3 isn't stubbed anywhere in this codebase (kodi_stubs.py's fake VFS is an in-memory
dict, not a real filesystem) and this module's entire point is its exact SQL behaviour, every
test here builds a REAL temporary on-disk SQLite file with a minimal fixture schema (just the
columns library_repair.py actually touches) rather than mocking the database away.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcvfs

import xbmc
import xbmcvfs

from lib import library_repair


_FIXTURE_SCHEMA = """
CREATE TABLE version (idVersion INTEGER);
CREATE TABLE tvshow (idShow INTEGER PRIMARY KEY, c00 TEXT);
CREATE TABLE episode (idEpisode INTEGER PRIMARY KEY, idShow INTEGER, idFile INTEGER, idSeason INTEGER);
CREATE TABLE files (idFile INTEGER PRIMARY KEY, idPath INTEGER, strFilename TEXT);
CREATE TABLE path (idPath INTEGER PRIMARY KEY, strPath TEXT, strScraper TEXT, idParentPath INTEGER);
CREATE TABLE art (art_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, type TEXT, url TEXT);
CREATE TABLE uniqueid (uniqueid_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, value TEXT, type TEXT);
CREATE TABLE bookmark (idBookmark INTEGER PRIMARY KEY, idFile INTEGER, timeInSeconds REAL);
CREATE TABLE streamdetails (idFile INTEGER, iStreamType INTEGER);
CREATE TABLE movie (idMovie INTEGER PRIMARY KEY, idFile INTEGER);
CREATE TABLE musicvideo (idMVideo INTEGER PRIMARY KEY, idFile INTEGER);
CREATE TABLE tvshowlinkpath (idShow INTEGER, idPath INTEGER);
"""

# The one show/episode pair that must survive every single test in this file untouched --
# every test that runs a real repair/undo asserts this is still exactly as seeded.
_CONTROL_SHOW_ID = 1
_CONTROL_EPISODE_IDS = (101, 102)
_CONTROL_FILE_IDS = (101, 102)


def _build_fixture_db(path, orphan_count=10, orphan_id_show=999, extra_sql=None):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_FIXTURE_SCHEMA)
        conn.execute('INSERT INTO version (idVersion) VALUES (131)')

        conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (?, ?)', (_CONTROL_SHOW_ID, 'A Real Show'))
        conn.execute('INSERT INTO path (idPath, strPath, strScraper, idParentPath) '
                     'VALUES (1, "/tv/A Real Show/", "script.chronicle.scraper.tv", NULL)')
        for i, (episode_id, file_id) in enumerate(zip(_CONTROL_EPISODE_IDS, _CONTROL_FILE_IDS)):
            conn.execute('INSERT INTO files (idFile, idPath, strFilename) VALUES (?, 1, ?)',
                         (file_id, 'S01E0{0}.mkv'.format(i + 1)))
            conn.execute('INSERT INTO episode (idEpisode, idShow, idFile, idSeason) VALUES (?, ?, ?, 10)',
                         (episode_id, _CONTROL_SHOW_ID, file_id))
            conn.execute("INSERT INTO art (media_id, media_type, type, url) VALUES (?, 'episode', 'thumb', 'x')",
                         (episode_id,))
            conn.execute("INSERT INTO uniqueid (media_id, media_type, value, type) VALUES (?, 'episode', 'tt1', 'imdb')",
                         (episode_id,))

        conn.execute('INSERT INTO path (idPath, strPath, strScraper, idParentPath) '
                     'VALUES (2, "/tv/A Gone Show/Season 01/", "script.chronicle.scraper.tv", NULL)')
        orphan_episode_ids = []
        orphan_file_ids = []
        for i in range(orphan_count):
            episode_id = 200 + i
            file_id = 200 + i
            orphan_episode_ids.append(episode_id)
            orphan_file_ids.append(file_id)
            conn.execute('INSERT INTO files (idFile, idPath, strFilename) VALUES (?, 2, ?)',
                         (file_id, 'S01E{0:02d}.mkv'.format(i + 1)))
            conn.execute('INSERT INTO episode (idEpisode, idShow, idFile, idSeason) VALUES (?, ?, ?, 20)',
                         (episode_id, orphan_id_show, file_id))
            conn.execute("INSERT INTO art (media_id, media_type, type, url) VALUES (?, 'episode', 'thumb', 'y')",
                         (episode_id,))
            conn.execute("INSERT INTO uniqueid (media_id, media_type, value, type) VALUES (?, 'episode', 'tt2', 'imdb')",
                         (episode_id,))
            if i == 0:
                conn.execute('INSERT INTO bookmark (idFile, timeInSeconds) VALUES (?, 42.0)', (file_id,))
                conn.execute('INSERT INTO streamdetails (idFile, iStreamType) VALUES (?, 0)', (file_id,))

        if extra_sql:
            conn.executescript(extra_sql)

        conn.commit()
    finally:
        conn.close()
    return orphan_episode_ids, orphan_file_ids


def _row_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: conn.execute('SELECT COUNT(*) FROM {0}'.format(table)).fetchone()[0]
            for table in ('tvshow', 'episode', 'files', 'art', 'uniqueid', 'bookmark', 'streamdetails')
        }
    finally:
        conn.close()


def _json_rpc_stub(live_show_count, real_folders=None, removed_show_ids=None):
    """`real_folders` maps a directory path -> list of real subfolder names present there, for
    Files.GetDirectory stubbing (detect_stale_shows). `removed_show_ids`, if given, is a list
    this stub appends each RemoveTVShow(tvshowid) call to, for repair_stale_shows assertions."""
    real_folders = real_folders or {}

    def _handler(request_json):
        request = json.loads(request_json)
        method = request['method']
        params = request.get('params', {})
        if method == 'VideoLibrary.GetTVShows':
            return json.dumps({'result': {'tvshows': [{}] * live_show_count}})
        if method == 'VideoLibrary.Scan':
            return json.dumps({'result': 'OK'})
        if method == 'Files.GetDirectory':
            directory = params.get('directory')
            names = real_folders.get(directory)
            if names is None:
                return json.dumps({'result': {'files': []}})
            return json.dumps({'result': {'files': [
                {'file': n, 'filetype': 'directory'} for n in names]}})
        if method == 'VideoLibrary.RemoveTVShow':
            if removed_show_ids is not None:
                removed_show_ids.append(params.get('tvshowid'))
            return json.dumps({'result': 'OK'})
        return json.dumps({'result': {}})
    return _handler


class LibraryRepairTestBase(unittest.TestCase):
    """Common fixture wiring: a real temp dir standing in for special://database/, a real temp
    dir standing in for the addon's backup folder, and translatePath/getAddonInfo/executeJSONRPC
    patched to route there -- see library_repair.py's own use of each for why."""

    live_show_count = 1  # matches the one control-group show every fixture seeds by default

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_dir = os.path.join(self.tmp_dir, 'database')
        self.backup_dir = os.path.join(self.tmp_dir, 'addon_data', 'library_repair_backups')
        os.makedirs(self.db_dir)
        self.db_path = os.path.join(self.db_dir, 'MyVideos131.db')

        self._orig_translate = xbmcvfs.translatePath
        self._orig_json_rpc = xbmc.executeJSONRPC
        self._orig_get_cond = xbmc.getCondVisibility

        xbmcvfs.translatePath = MagicMock(side_effect=self._translate_path)
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(self.live_show_count))
        xbmc.getCondVisibility = MagicMock(return_value=False)

        library_repair.ADDON.getAddonInfo = MagicMock(
            side_effect=lambda key: 'special://profile/addon_data/tv/' if key == 'profile'
            else 'script.chronicle.scraper.tv')

    def tearDown(self):
        xbmcvfs.translatePath = self._orig_translate
        xbmc.executeJSONRPC = self._orig_json_rpc
        xbmc.getCondVisibility = self._orig_get_cond
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # A stale lock from an assertion failure mid-test must never leak into the next test.
        try:
            xbmcvfs.delete(library_repair._LOCK_PATH)
        except Exception:
            pass

    def _translate_path(self, path):
        if path == 'special://database/':
            return self.db_dir + os.sep
        if path == 'special://profile/addon_data/tv/library_repair_backups/':
            return self.backup_dir + os.sep
        if path == 'special://profile/advancedsettings.xml':
            return os.path.join(self.tmp_dir, 'no_such_advancedsettings.xml')
        return ''

    def build_fixture(self, **kwargs):
        return _build_fixture_db(self.db_path, **kwargs)


class TestDetectOrphans(LibraryRepairTestBase):

    def test_finds_only_orphans_never_the_control_group(self):
        orphan_episode_ids, orphan_file_ids = self.build_fixture(orphan_count=5)
        report = library_repair.detect_orphans(self.db_path)
        self.assertEqual(sorted(report['episode_ids']), sorted(orphan_episode_ids))
        self.assertEqual(sorted(report['file_ids']), sorted(orphan_file_ids))
        self.assertEqual(report['total_episodes'], 5)
        for cid in _CONTROL_EPISODE_IDS:
            self.assertNotIn(cid, report['episode_ids'])

    def test_groups_by_former_idshow_with_example_paths_not_titles(self):
        self.build_fixture(orphan_count=3)
        report = library_repair.detect_orphans(self.db_path)
        self.assertEqual(len(report['groups']), 1)
        group = report['groups'][0]
        self.assertEqual(group['former_idShow'], 999)
        self.assertEqual(group['episode_count'], 3)
        self.assertTrue(all('/tv/A Gone Show/' in p for p in group['example_paths']))


class TestStaleShows(LibraryRepairTestBase):
    """Tests for detect_stale_shows()/repair_stale_shows() -- the second, unrelated bug (a
    show's cached season-folder path no longer exists on disk) folded into the same Repair
    action as a second step. See library_repair.py's own doc for the root cause."""

    def _seed_show_with_season_path(self, id_show, root_path, season_path):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (?, ?)', (id_show, 'Stuck Show'))
            conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (?, ?, NULL)',
                         (100 + id_show, root_path))
            conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (?, ?)',
                         (id_show, 100 + id_show))
            conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (?, ?, ?)',
                         (200 + id_show, season_path, 100 + id_show))
            conn.commit()
        finally:
            conn.close()

    def test_detects_show_whose_season_folder_no_longer_exists(self):
        # Files.GetDirectory returns FULL paths in its 'file' field (confirmed live 2026-09-13,
        # e.g. "smb://host/TV/Show/Season 01/"), matching the DB's own strPath format exactly --
        # the real folder here is padded ("Season 01"), so the DB's unpadded "Season 1" entry
        # doesn't appear in the real listing at all.
        self.build_fixture(orphan_count=0)
        self._seed_show_with_season_path(5, '/tv/Stuck Show/', '/tv/Stuck Show/Season 1/')
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, real_folders={'/tv/Stuck Show/': ['/tv/Stuck Show/Season 01/']}))

        stale = library_repair.detect_stale_shows(self.db_path)

        self.assertEqual([s['tvshowid'] for s in stale], [5])
        self.assertEqual(stale[0]['name'], 'Stuck Show')

    def test_does_not_flag_show_whose_season_folder_still_exists(self):
        self.build_fixture(orphan_count=0)
        self._seed_show_with_season_path(5, '/tv/Stuck Show/', '/tv/Stuck Show/Season 1/')
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, real_folders={'/tv/Stuck Show/': ['/tv/Stuck Show/Season 1/']}))

        stale = library_repair.detect_stale_shows(self.db_path)

        self.assertEqual(stale, [])

    def test_ignores_show_with_no_season_level_paths(self):
        # The control show (from build_fixture) has no season-level path row and no
        # tvshowlinkpath entry at all -- must never be flagged, the same way a legitimately
        # not-yet-aired show (which also has zero episodes) must never be flagged.
        self.build_fixture(orphan_count=0)
        stale = library_repair.detect_stale_shows(self.db_path)
        self.assertEqual(stale, [])

    def test_missing_tvshowlinkpath_table_skips_gracefully_no_crash(self):
        self.build_fixture(orphan_count=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute('DROP TABLE tvshowlinkpath')
        conn.commit()
        conn.close()
        stale = library_repair.detect_stale_shows(self.db_path)
        self.assertEqual(stale, [])

    def test_multi_source_show_counted_only_once(self):
        # A show linked to more than one root path (a valid, if uncommon, Kodi multi-source
        # config) must still yield exactly ONE entry -- otherwise repair_stale_shows() would
        # call RemoveTVShow twice for the same id and the reported counts would double.
        self.build_fixture(orphan_count=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (5, ?)', ('Stuck Show',))
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (105, ?, NULL)',
                     ('/tv/Stuck Show/',))
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (106, ?, NULL)',
                     ('/tv2/Stuck Show/',))
        conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (5, 105)')
        conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (5, 106)')
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (205, ?, 105)',
                     ('/tv/Stuck Show/Season 1/',))
        conn.commit()
        conn.close()
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, real_folders={'/tv/Stuck Show/': ['/tv/Stuck Show/Season 01/']}))

        stale = library_repair.detect_stale_shows(self.db_path)

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]['tvshowid'], 5)

    def test_repair_stale_shows_removes_show_and_triggers_a_scoped_scan_not_a_blind_one(self):
        # Root-caused live (2026-09-18): this used to fire ONE unscoped VideoLibrary.Scan
        # (params={}), which scans every configured video source regardless of content type and
        # cascades into every other addon's own onScanFinished handler. It must now go through
        # own_source_directories()/trigger_scan() instead -- the same scoped mechanism the
        # post-repair scan prompt in default.py already uses -- firing one scoped scan per
        # source folder this addon actually owns (see the fixture's own path rows).
        self.build_fixture()
        removed_ids = []
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, removed_show_ids=removed_ids))

        removed_names = library_repair.repair_stale_shows(
            [{'tvshowid': 5, 'name': 'Stuck Show'}], self.db_path)

        self.assertEqual(removed_names, ['Stuck Show'])
        self.assertEqual(removed_ids, [5])
        scan_calls = [
            json.loads(c.args[0]) for c in xbmc.executeJSONRPC.call_args_list
            if json.loads(c.args[0])['method'] == 'VideoLibrary.Scan'
        ]
        self.assertTrue(scan_calls, 'expected at least one scoped scan call')
        for call in scan_calls:
            self.assertIn('directory', call['params'],
                           'every VideoLibrary.Scan call must be scoped to a specific directory, '
                           'never a blind whole-library scan')

    def test_repair_stale_shows_skips_scan_when_nothing_removed(self):
        # If RemoveTVShow fails for every candidate, triggering a scan afterward would be
        # pointless work on a real device's library.
        self.build_fixture()

        def _handler(request_json):
            request = json.loads(request_json)
            if request['method'] == 'VideoLibrary.RemoveTVShow':
                return json.dumps({'error': {'message': 'nope'}})
            return json.dumps({'result': {}})
        xbmc.executeJSONRPC = MagicMock(side_effect=_handler)

        removed_names = library_repair.repair_stale_shows(
            [{'tvshowid': 5, 'name': 'Stuck Show'}], self.db_path)

        self.assertEqual(removed_names, [])
        scan_calls = [
            c for c in xbmc.executeJSONRPC.call_args_list
            if json.loads(c.args[0])['method'] == 'VideoLibrary.Scan'
        ]
        self.assertEqual(scan_calls, [])

    def test_repair_stale_shows_one_failure_does_not_abort_the_rest_of_the_batch(self):
        self.build_fixture()

        def _handler(request_json):
            request = json.loads(request_json)
            if request['method'] == 'VideoLibrary.RemoveTVShow':
                if request['params']['tvshowid'] == 5:
                    return json.dumps({'error': {'message': 'nope'}})
                return json.dumps({'result': 'OK'})
            return json.dumps({'result': {}})
        xbmc.executeJSONRPC = MagicMock(side_effect=_handler)

        removed_names = library_repair.repair_stale_shows([
            {'tvshowid': 5, 'name': 'Fails To Remove'},
            {'tvshowid': 6, 'name': 'Removes Fine'},
        ], self.db_path)

        self.assertEqual(removed_names, ['Removes Fine'])
        scan_calls = [
            c for c in xbmc.executeJSONRPC.call_args_list
            if json.loads(c.args[0])['method'] == 'VideoLibrary.Scan'
        ]
        self.assertTrue(scan_calls, 'one real removal still means a scan should run')

    def test_repair_stale_shows_skips_entirely_when_a_scan_is_already_running(self):
        self.build_fixture()
        xbmc.getCondVisibility = MagicMock(return_value=True)
        removed_ids = []
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, removed_show_ids=removed_ids))

        removed_names = library_repair.repair_stale_shows(
            [{'tvshowid': 5, 'name': 'Stuck Show'}], self.db_path)

        self.assertEqual(removed_names, [])
        self.assertEqual(removed_ids, [], 'must not touch anything while a scan is active')


class TestStuckFiles(LibraryRepairTestBase):
    """Tests for detect_stuck_files()/repair_stuck_files() -- a THIRD, unrelated bug with the
    same symptom: a `files` row with no matching `episode` row at all, which neither
    detect_orphans() (requires an episode row) nor detect_stale_shows() (requires a season-path
    mismatch) can ever find. See library_repair.py's own doc for the root cause."""

    def _seed_show_with_files(self, id_show, show_name, root_path, season_path,
                               stuck_filenames=(), linked_filenames=()):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (?, ?)', (id_show, show_name))
            root_path_id = 100 + id_show
            season_path_id = 200 + id_show
            conn.execute('INSERT INTO path (idPath, strPath, strScraper, idParentPath) VALUES (?, ?, ?, NULL)',
                         (root_path_id, root_path, 'script.chronicle.scraper.tv'))
            conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (?, ?)', (id_show, root_path_id))
            conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (?, ?, ?)',
                         (season_path_id, season_path, root_path_id))

            next_file_id = [300 + id_show * 100]

            def _add_file(filename, linked):
                file_id = next_file_id[0]
                next_file_id[0] += 1
                conn.execute('INSERT INTO files (idFile, idPath, strFilename) VALUES (?, ?, ?)',
                             (file_id, season_path_id, filename))
                if linked:
                    episode_id = file_id  # any unique id -- not asserted on in these tests
                    conn.execute('INSERT INTO episode (idEpisode, idShow, idFile, idSeason) VALUES (?, ?, ?, 1)',
                                 (episode_id, id_show, file_id))
                return file_id

            stuck_ids = [_add_file(f, linked=False) for f in stuck_filenames]
            for f in linked_filenames:
                _add_file(f, linked=True)
            conn.commit()
            return stuck_ids
        finally:
            conn.close()

    def test_finds_files_with_no_episode_row_scoped_to_this_addons_own_shows(self):
        self.build_fixture(orphan_count=0)
        stuck_ids = self._seed_show_with_files(
            5, 'Lanterns', '/tv/Lanterns/', '/tv/Lanterns/Season 01/',
            stuck_filenames=['S01E04.mkv', 'S01E05.mkv'], linked_filenames=['S01E01.mkv'])

        report = library_repair.detect_stuck_files(self.db_path)

        self.assertEqual(sorted(report['file_ids']), sorted(stuck_ids))
        self.assertEqual(len(report['groups']), 1)
        self.assertEqual(report['groups'][0]['show_name'], 'Lanterns')
        self.assertEqual(report['groups'][0]['file_count'], 2)

    def test_ignores_a_healthy_show_with_every_file_linked(self):
        self.build_fixture(orphan_count=0)
        self._seed_show_with_files(
            5, 'Healthy Show', '/tv/Healthy Show/', '/tv/Healthy Show/Season 01/',
            linked_filenames=['S01E01.mkv', 'S01E02.mkv'])

        report = library_repair.detect_stuck_files(self.db_path)

        self.assertEqual(report['file_ids'], [])
        self.assertEqual(report['groups'], [])

    def test_ignores_files_under_a_path_this_addon_does_not_own(self):
        # A files row with no episode under some OTHER scraper's source (or a movie source
        # entirely) must never be touched -- only strScraper == this addon's own id is in scope.
        self.build_fixture(orphan_count=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (9, ?)', ('Someone Elses Show',))
        conn.execute('INSERT INTO path (idPath, strPath, strScraper, idParentPath) VALUES (109, ?, ?, NULL)',
                     ('/tv/Other/', 'metadata.tvshows.other'))
        conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (9, 109)')
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (209, ?, 109)',
                     ('/tv/Other/Season 01/',))
        conn.execute('INSERT INTO files (idFile, idPath, strFilename) VALUES (999, 209, ?)',
                     ('S01E01.mkv',))
        conn.commit()
        conn.close()

        report = library_repair.detect_stuck_files(self.db_path)

        self.assertEqual(report['file_ids'], [])

    def test_finds_stuck_file_directly_under_the_show_root_for_a_flat_show(self):
        # A single-season/flat show can have its files directly under the root path, not a
        # separate season subfolder -- must still be found.
        self.build_fixture(orphan_count=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (7, ?)', ('Flat Show',))
        conn.execute('INSERT INTO path (idPath, strPath, strScraper, idParentPath) VALUES (107, ?, ?, NULL)',
                     ('/tv/Flat Show/', 'script.chronicle.scraper.tv'))
        conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (7, 107)')
        conn.execute('INSERT INTO files (idFile, idPath, strFilename) VALUES (777, 107, ?)',
                     ('S01E01.mkv',))
        conn.commit()
        conn.close()

        report = library_repair.detect_stuck_files(self.db_path)

        self.assertEqual(report['file_ids'], [777])

    def test_missing_tvshowlinkpath_table_skips_gracefully_no_crash(self):
        self.build_fixture(orphan_count=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute('DROP TABLE tvshowlinkpath')
        conn.commit()
        conn.close()
        report = library_repair.detect_stuck_files(self.db_path)
        self.assertEqual(report, {'file_ids': [], 'groups': []})

    def test_repair_deletes_stuck_files_and_dependents_leaves_linked_files_and_control_group(self):
        orphan_episode_ids, orphan_file_ids = self.build_fixture(orphan_count=0)
        stuck_ids = self._seed_show_with_files(
            5, 'Lanterns', '/tv/Lanterns/', '/tv/Lanterns/Season 01/',
            stuck_filenames=['S01E04.mkv', 'S01E05.mkv'], linked_filenames=['S01E01.mkv'])
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO bookmark (idFile, timeInSeconds) VALUES (?, 10.0)', (stuck_ids[0],))
        conn.execute('INSERT INTO streamdetails (idFile, iStreamType) VALUES (?, 0)', (stuck_ids[0],))
        conn.commit()
        conn.close()

        report = library_repair.detect_stuck_files(self.db_path)
        result = library_repair.repair_stuck_files(self.db_path, report, self.backup_dir)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_files'], 2)
        self.assertEqual(result['deleted_bookmark'], 1)
        self.assertEqual(result['deleted_streamdetails'], 1)
        self.assertIsNotNone(result['backup_path'])

        conn = sqlite3.connect(self.db_path)
        try:
            remaining_file_ids = {r[0] for r in conn.execute('SELECT idFile FROM files')}
        finally:
            conn.close()
        for fid in stuck_ids:
            self.assertNotIn(fid, remaining_file_ids)
        # The control group (from build_fixture) and the linked S01E01 file must both survive.
        for fid in _CONTROL_FILE_IDS:
            self.assertIn(fid, remaining_file_ids)

    def test_repair_no_stuck_files_makes_no_backup(self):
        self.build_fixture(orphan_count=0)
        report = {'file_ids': [], 'groups': []}
        result = library_repair.repair_stuck_files(self.db_path, report, self.backup_dir)
        self.assertIsNone(result['backup_path'])
        self.assertEqual(result['deleted_files'], 0)

    def test_full_repair_flow_includes_stuck_files(self):
        # End-to-end through prepare_repair()/finish_repair(), the same entry points default.py
        # actually calls -- confirms stuck-file detection and repair are wired into the normal
        # Repair action, not just directly callable in isolation.
        self.build_fixture(orphan_count=0)
        self._seed_show_with_files(
            5, 'Lanterns', '/tv/Lanterns/', '/tv/Lanterns/Season 01/',
            stuck_filenames=['S01E04.mkv'], linked_filenames=['S01E01.mkv'])

        db_path, report = library_repair.prepare_repair()
        self.assertEqual(len(report['stuck_files']['file_ids']), 1)
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_stuck_files'], 1)


class TestPreconditions(LibraryRepairTestBase):

    def test_scanning_aborts_before_any_file_access(self):
        self.build_fixture(orphan_count=2)
        xbmc.getCondVisibility = MagicMock(return_value=True)
        with self.assertRaises(library_repair.LibraryRepairError) as ctx:
            library_repair.preview()
        self.assertEqual(ctx.exception.reason_code, 'scanning')

    def test_missing_required_table_aborts_safely(self):
        self.build_fixture(orphan_count=2)
        before = _row_counts(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute('DROP TABLE uniqueid')
        conn.commit()
        conn.close()
        with self.assertRaises(library_repair.LibraryRepairError) as ctx:
            library_repair.preview()
        self.assertEqual(ctx.exception.reason_code, 'schema_mismatch')
        # Compared on the surviving tables only -- uniqueid was deliberately dropped by this
        # test itself (to trigger the schema check), not by the code under test.
        conn = sqlite3.connect(self.db_path)
        try:
            after = {
                table: conn.execute('SELECT COUNT(*) FROM {0}'.format(table)).fetchone()[0]
                for table in before if table != 'uniqueid'
            }
        finally:
            conn.close()
        self.assertEqual(after, {k: v for k, v in before.items() if k != 'uniqueid'})

    def test_mysql_library_detected_aborts(self):
        self.build_fixture(orphan_count=2)
        advanced = self._translate_path('special://profile/advancedsettings.xml')
        with open(advanced, 'w') as f:
            f.write('<advancedsettings><videodatabase><type>mysql</type></videodatabase></advancedsettings>')
        with self.assertRaises(library_repair.LibraryRepairError) as ctx:
            library_repair.preview()
        self.assertEqual(ctx.exception.reason_code, 'mysql_library')

    def test_live_count_mismatch_aborts(self):
        self.build_fixture(orphan_count=2)
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(live_show_count=50))
        with self.assertRaises(library_repair.LibraryRepairError) as ctx:
            library_repair.preview()
        self.assertEqual(ctx.exception.reason_code, 'live_count_mismatch')

    def test_multiple_db_files_refuses_to_guess(self):
        self.build_fixture(orphan_count=2)
        shutil.copyfile(self.db_path, os.path.join(self.db_dir, 'MyVideos999.db'))
        with self.assertRaises(library_repair.LibraryRepairError) as ctx:
            library_repair.find_video_db_path()
        self.assertEqual(ctx.exception.reason_code, 'multiple_db_files')


class TestRepairFlow(LibraryRepairTestBase):

    def test_declining_confirmation_makes_zero_changes(self):
        self.build_fixture(orphan_count=4)
        before = _row_counts(self.db_path)
        db_path, report = library_repair.prepare_repair()
        result = library_repair.finish_repair(db_path, report, execute=False)
        self.assertIsNone(result)
        self.assertEqual(_row_counts(self.db_path), before)
        self.assertFalse(os.path.isdir(self.backup_dir) and os.listdir(self.backup_dir))

    def test_preview_reports_progress_before_returning(self):
        # Per-user request (2026-09-14): a remote click needs SOMETHING to show immediately,
        # not just a result several seconds later. preview()/prepare_repair()/finish_repair()
        # all accept an optional progress_callback for exactly this -- called with a short
        # status string at each real step, never raising even if this test's own probe did.
        self.build_fixture(orphan_count=4)
        messages = []
        library_repair.preview(progress_callback=messages.append)
        self.assertTrue(len(messages) >= 2, 'expected multiple progress updates, got {0}'.format(messages))
        self.assertTrue(all(isinstance(m, str) for m in messages))

    def test_prepare_and_finish_repair_report_progress_through_the_whole_run(self):
        self.build_fixture(orphan_count=4)
        detect_messages = []
        db_path, report = library_repair.prepare_repair(progress_callback=detect_messages.append)
        self.assertTrue(len(detect_messages) >= 2)

        repair_messages = []
        result = library_repair.finish_repair(db_path, report, execute=True, progress_callback=repair_messages.append)
        self.assertFalse(result['aborted'])
        # Must cover both the backup and the delete phase, not just one -- the two real slow
        # steps in run_repair(), and the whole point of this feedback existing at all.
        self.assertTrue(any('backing up' in m.lower() for m in repair_messages), repair_messages)
        self.assertTrue(any('removing' in m.lower() for m in repair_messages), repair_messages)

    def test_progress_callback_exception_never_aborts_a_real_repair(self):
        # A UI-side callback failure (e.g. a closed/torn-down dialog) must never take down the
        # actual repair logic underneath it -- _report() swallows whatever the callback raises.
        self.build_fixture(orphan_count=4)

        def broken_callback(_message):
            raise RuntimeError('dialog already closed')

        db_path, report = library_repair.prepare_repair(progress_callback=broken_callback)
        result = library_repair.finish_repair(db_path, report, execute=True, progress_callback=broken_callback)
        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_episodes'], 4)

    def test_backup_created_and_verified_before_delete_runs(self):
        self.build_fixture(orphan_count=4)
        db_path, report = library_repair.prepare_repair()
        with patch.object(library_repair, '_find_shared_file_ids', side_effect=RuntimeError('boom')):
            result = library_repair.finish_repair(db_path, report, execute=True)
        self.assertTrue(result['aborted'])
        self.assertIsNotNone(result['backup_path'])
        self.assertTrue(os.path.exists(result['backup_path']))
        conn = sqlite3.connect('file:{0}?mode=ro'.format(result['backup_path']), uri=True)
        try:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM episode').fetchone()[0], 6)  # 2 control + 4 orphan
        finally:
            conn.close()
        # And the LIVE db must be untouched -- the delete step raised before doing anything.
        self.assertEqual(_row_counts(self.db_path)['episode'], 6)

    def test_full_repair_removes_orphans_and_dependents_leaves_control_group(self):
        orphan_episode_ids, orphan_file_ids = self.build_fixture(orphan_count=4)
        db_path, report = library_repair.prepare_repair()
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_episodes'], 4)
        self.assertEqual(result['deleted_files'], 4)
        self.assertEqual(result['deleted_art'], 4)
        self.assertEqual(result['deleted_uniqueid'], 4)
        self.assertEqual(result['deleted_bookmark'], 1)
        self.assertEqual(result['deleted_streamdetails'], 1)

        counts = _row_counts(self.db_path)
        self.assertEqual(counts['tvshow'], 1)
        self.assertEqual(counts['episode'], 2)   # only the control group survives
        self.assertEqual(counts['files'], 2)
        self.assertEqual(counts['art'], 2)
        self.assertEqual(counts['uniqueid'], 2)

        conn = sqlite3.connect(self.db_path)
        try:
            remaining_episode_ids = {r[0] for r in conn.execute('SELECT idEpisode FROM episode')}
        finally:
            conn.close()
        self.assertEqual(remaining_episode_ids, set(_CONTROL_EPISODE_IDS))

    def test_zero_orphans_repairs_stale_show_with_no_backup_made(self):
        # Regression: when there are NO orphaned episodes, run_repair() returns before ever
        # creating a backup (result['backup_path'] stays None) -- default.py's results dialog
        # must branch on this (it used to always interpolate backup_path into the "a backup was
        # saved to: {path}" message, printing the literal text "None" when only a stale show was
        # fixed). This test locks in the exact condition that fix depends on: deleted_episodes==0
        # and backup_path is None, while the stale-show repair still runs normally.
        self.build_fixture(orphan_count=0)
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (5, ?)', ('Stuck Show',))
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (105, ?, NULL)',
                     ('/tv/Stuck Show/',))
        conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (5, 105)')
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (205, ?, 105)',
                     ('/tv/Stuck Show/Season 1/',))
        conn.commit()
        conn.close()
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, real_folders={'/tv/Stuck Show/': ['/tv/Stuck Show/Season 01/']}))

        db_path, report = library_repair.prepare_repair()
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_episodes'], 0)
        self.assertIsNone(result['backup_path'])
        self.assertEqual(result['repaired_stale_shows'], ['Stuck Show'])
        self.assertFalse(os.path.isdir(self.backup_dir) and os.listdir(self.backup_dir),
                          'no backup file should exist -- nothing needed one')

    def test_full_repair_also_fixes_stale_shows_as_a_second_step(self):
        # Integration: preview()/prepare_repair()/finish_repair() must run BOTH detections and,
        # on execute=True, act on both -- combined into the one "Repair Stuck Episodes" action
        # per-user request (2026-09-13), not a second button.
        self.build_fixture(orphan_count=2)
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO tvshow (idShow, c00) VALUES (5, ?)', ('Stuck Show',))
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (105, ?, NULL)',
                     ('/tv/Stuck Show/',))
        conn.execute('INSERT INTO tvshowlinkpath (idShow, idPath) VALUES (5, 105)')
        conn.execute('INSERT INTO path (idPath, strPath, idParentPath) VALUES (205, ?, 105)',
                     ('/tv/Stuck Show/Season 1/',))
        conn.commit()
        conn.close()
        removed_ids = []
        xbmc.executeJSONRPC = MagicMock(side_effect=_json_rpc_stub(
            self.live_show_count, real_folders={'/tv/Stuck Show/': ['/tv/Stuck Show/Season 01/']},
            removed_show_ids=removed_ids))

        report = library_repair.preview()
        self.assertEqual([s['tvshowid'] for s in report['stale_shows']], [5])

        db_path, report = library_repair.prepare_repair()
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_episodes'], 2)          # step 1: orphan repair still runs
        self.assertEqual(result['repaired_stale_shows'], ['Stuck Show'])  # step 2: stale-show fix
        self.assertEqual(removed_ids, [5])

    def test_scanning_detected_before_commit_rolls_back(self):
        self.build_fixture(orphan_count=4)
        before = _row_counts(self.db_path)
        # prepare_repair() runs its own scanning check FIRST, against the default (False) mock
        # from setUp -- only installed AFTER that succeeds, so this simulates a scan starting
        # in the window between the confirmation dialog and the actual delete transaction,
        # which is exactly the case run_repair()'s own pre-commit re-check exists to catch.
        db_path, report = library_repair.prepare_repair()
        xbmc.getCondVisibility = MagicMock(return_value=True)
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertTrue(result['aborted'])
        self.assertEqual(result['abort_reason'], 'scan_started_during_repair')
        # Rolled back -- the delete never actually stuck, even though a backup was made.
        self.assertEqual(_row_counts(self.db_path), before)

    def test_locked_database_retries_then_succeeds(self):
        self.build_fixture(orphan_count=2)
        db_path, report = library_repair.prepare_repair()

        real_connect = sqlite3.connect
        state = {'attempts': 0}

        def flaky_connect(target, *args, **kwargs):
            if target == db_path and 'mode=ro' not in target:
                state['attempts'] += 1
                if state['attempts'] <= 2:
                    raise sqlite3.OperationalError('database is locked')
            return real_connect(target, *args, **kwargs)

        with patch('lib.library_repair.sqlite3.connect', side_effect=flaky_connect), \
             patch('lib.library_repair.time.sleep'):
            result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_episodes'], 2)
        self.assertEqual(state['attempts'], 3)

    def test_locked_database_exhausts_retries_aborts_cleanly(self):
        self.build_fixture(orphan_count=2)
        before = _row_counts(self.db_path)
        db_path, report = library_repair.prepare_repair()

        real_connect = sqlite3.connect

        def always_locked(target, *args, **kwargs):
            if target == db_path:
                raise sqlite3.OperationalError('database is locked')
            return real_connect(target, *args, **kwargs)

        with patch('lib.library_repair.sqlite3.connect', side_effect=always_locked), \
             patch('lib.library_repair.time.sleep'):
            result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertTrue(result['aborted'])
        self.assertEqual(result['abort_reason'], 'database_locked')
        self.assertEqual(_row_counts(self.db_path), before)

    def test_shared_idfile_preserves_files_row_but_still_removes_episode_row(self):
        orphan_episode_ids, orphan_file_ids = self.build_fixture(
            orphan_count=3, extra_sql='INSERT INTO movie (idMovie, idFile) VALUES (1, 200);')
        db_path, report = library_repair.prepare_repair()
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['skipped_shared_files'], 1)
        self.assertEqual(result['deleted_episodes'], 3)   # all 3 orphaned episodes still removed
        self.assertEqual(result['deleted_files'], 2)      # but idFile 200 (shared with the movie) survives

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertIsNotNone(conn.execute('SELECT idFile FROM files WHERE idFile=200').fetchone())
            self.assertIsNone(conn.execute('SELECT idEpisode FROM episode WHERE idFile=200').fetchone())
        finally:
            conn.close()

    def test_large_batch_exceeds_sqlite_param_limit(self):
        orphan_episode_ids, orphan_file_ids = self.build_fixture(orphan_count=1200)
        db_path, report = library_repair.prepare_repair()
        self.assertEqual(report['total_episodes'], 1200)
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        self.assertEqual(result['deleted_episodes'], 1200)
        self.assertEqual(result['deleted_files'], 1200)
        self.assertEqual(_row_counts(self.db_path)['episode'], 2)  # only the control group left

    def test_backup_retention_prunes_oldest_first_never_below_cap(self):
        self.build_fixture(orphan_count=1)
        os.makedirs(self.backup_dir, exist_ok=True)
        fake_backups = []
        for i in range(7):
            path = os.path.join(self.backup_dir, 'MyVideos131.db.repair_backup_2026010{0}_000000'.format(i))
            with open(path, 'w') as f:
                f.write('x')
            os.utime(path, (time.time() + i, time.time() + i))
            fake_backups.append(path)

        db_path, report = library_repair.prepare_repair()
        result = library_repair.finish_repair(db_path, report, execute=True)

        self.assertFalse(result['aborted'])
        remaining = sorted(
            p for p in os.listdir(self.backup_dir) if library_repair._BACKUP_SUFFIX_RE.search(p))
        self.assertEqual(len(remaining), 5)
        # The just-created backup must always be among the survivors.
        self.assertIn(os.path.basename(result['backup_path']), remaining)
        # And it must have pruned the OLDEST fakes first, not the newest.
        self.assertNotIn(os.path.basename(fake_backups[0]), remaining)
        self.assertNotIn(os.path.basename(fake_backups[1]), remaining)


class TestUndoLastRepair(LibraryRepairTestBase):

    def test_no_manifest_raises(self):
        self.build_fixture(orphan_count=1)
        self.assertIsNone(library_repair.peek_last_manifest())
        with self.assertRaises(library_repair.LibraryRepairError) as ctx:
            library_repair.run_undo()
        self.assertEqual(ctx.exception.reason_code, 'no_manifest')

    def test_undo_restores_exactly_the_repaired_rows(self):
        orphan_episode_ids, orphan_file_ids = self.build_fixture(orphan_count=3)
        db_path, report = library_repair.prepare_repair()
        repair_result = library_repair.finish_repair(db_path, report, execute=True)
        self.assertFalse(repair_result['aborted'])
        self.assertEqual(_row_counts(self.db_path)['episode'], 2)  # just the control group

        manifest = library_repair.peek_last_manifest()
        self.assertEqual(sorted(manifest['episode_ids']), sorted(orphan_episode_ids))

        undo_result = library_repair.run_undo()
        self.assertFalse(undo_result['aborted'])
        self.assertEqual(undo_result['restored_episodes'], 3)
        self.assertEqual(undo_result['restored_files'], 3)
        self.assertEqual(undo_result['restored_art'], 3)
        self.assertEqual(undo_result['restored_uniqueid'], 3)
        self.assertEqual(undo_result['restored_bookmark'], 1)
        self.assertEqual(undo_result['restored_streamdetails'], 1)

        counts = _row_counts(self.db_path)
        self.assertEqual(counts['episode'], 5)  # 2 control + 3 restored orphans

    def test_undo_does_not_clobber_a_row_added_after_the_repair(self):
        """A show/episode added AFTER the repair ran (e.g. by a scan in between) must survive an
        Undo untouched -- proving the re-insert is additive-and-scoped, never a table-level
        restore of the whole backup."""
        self.build_fixture(orphan_count=2)
        db_path, report = library_repair.prepare_repair()
        library_repair.finish_repair(db_path, report, execute=True)

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO tvshow (idShow, c00) VALUES (5, 'Added After Repair')")
        conn.execute('INSERT INTO files (idFile, idPath, strFilename) VALUES (500, 1, "new.mkv")')
        conn.execute('INSERT INTO episode (idEpisode, idShow, idFile, idSeason) VALUES (500, 5, 500, 1)')
        conn.commit()
        conn.close()

        undo_result = library_repair.run_undo()
        self.assertFalse(undo_result['aborted'])

        conn = sqlite3.connect(self.db_path)
        try:
            still_there = conn.execute('SELECT idEpisode FROM episode WHERE idEpisode=500').fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(still_there, 'a row added after the repair must survive Undo Last Repair')


if __name__ == '__main__':
    unittest.main()
