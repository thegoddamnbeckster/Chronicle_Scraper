# -*- coding: utf-8 -*-
"""
Tests for lib/scan_progress.py -- the once-a-minute "where is the scan" log line (added 2026-10-02
after a scan that ended with most of the library missing couldn't be followed: Kodi names the
folder it is on only at debug level, and the addon only logged per scraped movie).
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import scan_progress
from lib.scan_progress import ScanProgress, locate, parse_scanned_paths

_LOG = scan_progress._LOG_PATH
_V3 = 'smb://10.0.0.162/Video3/Movies/'
_V4 = 'smb://10.0.0.162/Video4/Movies/'


def _scan_line(kind, path, tail=''):
    return "2026-10-02 22:00:00.000 T:408 debug <general>: VideoInfoScanner: {0} dir '{1}'{2}\n".format(
        kind, path, tail)


def _write_log(text):
    kodi_stubs._FAKE_FILES[_LOG] = text.encode('utf-8')


def _append_log(text):
    kodi_stubs._FAKE_FILES[_LOG] = kodi_stubs._FAKE_FILES.get(_LOG, b'') + text.encode('utf-8')


def _rpc_counts(movies, shows, episodes):
    totals = {'VideoLibrary.GetMovies': movies, 'VideoLibrary.GetTVShows': shows,
              'VideoLibrary.GetEpisodes': episodes}

    def _fake(request_json):
        method = json.loads(request_json)['method']
        return json.dumps({'result': {'limits': {'total': totals[method]}}})
    return _fake


class TestParsing(unittest.TestCase):

    def test_recognises_the_three_decision_kinds_and_ignores_everything_else(self):
        text = (
            _scan_line('Scanning', _V3 + 'Red Planet (2000)/', ' as not in the database')
            + _scan_line('Skipping', _V3 + 'Rogue One (2016)/', ' due to no change (fasthash)')
            + _scan_line('Rescanning', _V4 + '27 Dresses (2008)/', ' due to change')
            + "2026-10-02 22:00:01.000 T:408 info <general>: VideoInfoScanner: Finished adding information from dir smb://x/y\n"
            + "2026-10-02 22:00:01.000 T:408 info <general>: something else entirely\n")

        self.assertEqual(parse_scanned_paths(text), [
            _V3 + 'Red Planet (2000)/', _V3 + 'Rogue One (2016)/', _V4 + '27 Dresses (2008)/'])

    def test_locate_attributes_a_path_to_its_source_and_top_level_folder(self):
        roots = [_V3, _V4]
        self.assertEqual(locate(_V3 + 'Red Planet (2000)/', roots), (_V3, 'Red Planet (2000)'))
        # a sub-folder (".actors", "extrathumbs", a season) belongs to its top-level folder
        self.assertEqual(locate(_V3 + 'Red Planet (2000)/.actors/', roots), (_V3, 'Red Planet (2000)'))

    def test_locate_is_case_insensitive_and_ignores_the_root_itself_and_strangers(self):
        self.assertEqual(locate('SMB://10.0.0.162/video3/movies/Shrek (2001)/', [_V3]), (_V3, 'Shrek (2001)'))
        self.assertEqual(locate(_V3, [_V3]), (None, None))
        self.assertEqual(locate('smb://elsewhere/share/X/', [_V3]), (None, None))

    def test_locate_prefers_the_longest_matching_root(self):
        outer, inner = 'smb://nas/Video/', 'smb://nas/Video/Movies/'
        self.assertEqual(locate(inner + 'A (2020)/', [outer, inner]), (inner, 'A (2020)'))


class TestConsumeLog(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_only_lines_after_the_scan_began_are_counted(self):
        _write_log(_scan_line('Skipping', _V3 + 'Old Folder (1999)/'))   # before this scan
        progress = ScanProgress()
        progress.begin()

        progress._consume_log([_V3])                                     # first look: just notes the end
        self.assertEqual(progress.seen, {})

        _append_log(_scan_line('Scanning', _V3 + 'Red Planet (2000)/')
                    + _scan_line('Scanning', _V3 + 'Red Planet (2000)/.actors/'))
        progress._consume_log([_V3])

        self.assertEqual(progress.seen, {_V3: {'Red Planet (2000)'}})   # the sub-folder is not a second folder
        self.assertEqual(progress.last_path, _V3 + 'Red Planet (2000)/.actors/')

    def test_a_service_that_starts_mid_scan_can_backfill_what_was_already_examined(self):
        _write_log(_scan_line('Skipping', _V3 + 'A (2000)/') + _scan_line('Skipping', _V3 + 'B (2001)/'))
        progress = ScanProgress()
        progress.begin(backfill=True)

        progress._consume_log([_V3])

        self.assertEqual(progress.seen, {_V3: {'A (2000)', 'B (2001)'}})

    def test_an_unfinished_last_line_is_read_whole_on_the_next_tick(self):
        _write_log('')
        progress = ScanProgress()
        progress.begin()
        progress._consume_log([_V3])

        line = _scan_line('Scanning', _V3 + 'Half Written (2020)/')
        _append_log(line[:40])                                           # Kodi is mid-write
        progress._consume_log([_V3])
        self.assertEqual(progress.seen, {})

        _append_log(line[40:])
        progress._consume_log([_V3])
        self.assertEqual(progress.seen, {_V3: {'Half Written (2020)'}})

    def test_a_restarted_log_is_read_from_the_top(self):
        _write_log(_scan_line('Skipping', _V3 + ('x' * 200) + '/'))
        progress = ScanProgress()
        progress.begin()
        progress._consume_log([_V3])                                     # offset is now the old, longer size

        _write_log(_scan_line('Scanning', _V3 + 'New Run (2026)/'))     # Kodi restarted: a shorter, new log
        progress._consume_log([_V3])

        self.assertIn('New Run (2026)', progress.seen[_V3])


class TestTickOutput(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def _tick(self, progress, counts, final=False, listings=None):
        listings = listings or {_V3: ['d'] * 123, _V4: ['d'] * 150}
        lines = []
        with patch.object(scan_progress.movie_art_sync, 'get_video_sources', return_value=list(listings)), \
             patch.object(scan_progress.movie_art_sync, 'list_source_dirs_cached',
                          side_effect=lambda root, **kw: listings[root]), \
             patch.object(scan_progress.xbmc, 'executeJSONRPC', side_effect=_rpc_counts(*counts)), \
             patch.object(scan_progress.activity_tracker, 'read_activity',
                          return_value={'timestamp': 0, 'count': 7, 'last_label': 'Red Planet'}), \
             patch.object(scan_progress.log, 'info', side_effect=lines.append):
            progress.tick(final=final)
        return lines

    def test_reports_examined_of_total_the_current_path_and_library_growth(self):
        _write_log('')
        progress = ScanProgress()
        progress.begin()
        self._tick(progress, (700, 173, 3843))                           # first tick: baseline

        _append_log(_scan_line('Skipping', _V3 + 'A (2000)/')
                    + _scan_line('Scanning', _V3 + 'B (2001)/')
                    + _scan_line('Scanning', _V3 + 'B (2001)/.actors/'))
        lines = self._tick(progress, (712, 173, 3850))

        self.assertIn('examined 2 of 273 top-level folders', lines[0])
        self.assertIn('at: ' + _V3 + 'B (2001)/.actors/', lines[0])
        self.assertIn('712 movies (+12)', lines[0])
        self.assertIn('3850 episodes (+7)', lines[0])
        self.assertIn('7 calls', lines[0])
        self.assertIn('(Red Planet)', lines[0])
        self.assertEqual(lines[1], 'scan progress by source: 10.0.0.162/Video3/Movies 2/123 | '
                                   '10.0.0.162/Video4/Movies 0/150')

    def test_without_debug_logging_the_folder_figures_say_unknown_but_the_rest_is_still_reported(self):
        _write_log('')
        progress = ScanProgress()
        progress.begin()
        lines = self._tick(progress, (719, 173, 3843))

        self.assertIn('examined: unknown of 273 top-level folders', lines[0])
        self.assertIn('debug logging', lines[0])
        self.assertIn('719 movies', lines[0])
        self.assertEqual(len(lines), 1)                                  # no per-source breakdown to show

    def test_the_final_line_says_finished(self):
        _write_log('')
        progress = ScanProgress()
        progress.begin()
        lines = self._tick(progress, (719, 173, 3843), final=True)

        self.assertTrue(lines[0].startswith('scan finished ('))

    def test_a_failure_inside_a_tick_is_logged_not_raised(self):
        progress = ScanProgress()
        progress.begin()
        warnings = []
        with patch.object(scan_progress.movie_art_sync, 'get_video_sources', side_effect=RuntimeError('boom')), \
             patch.object(scan_progress.log, 'warning', side_effect=warnings.append):
            progress.tick()                                              # must not raise

        self.assertEqual(len(warnings), 1)
        self.assertIn('boom', warnings[0])

    def test_a_tick_is_skipped_while_the_previous_one_is_still_running(self):
        progress = ScanProgress()
        progress.begin()
        progress._lock.acquire()
        try:
            with patch.object(scan_progress.movie_art_sync, 'get_video_sources') as sources:
                progress.tick()
            sources.assert_not_called()
        finally:
            progress._lock.release()


if __name__ == '__main__':
    unittest.main()
