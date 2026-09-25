# -*- coding: utf-8 -*-
"""
Tests for lib/library_repair.py's fabricated-watched pass -- episodes Kodi reports as watched
whose lastplayed is identical, to the second, across several episodes of one show. Confirmed live
(2026-09-23): shows the user never watched carried 6 of 9 / 17 of 24 episodes stamped with one
identical instant, and Chronicle-side-only cleanups were undone within minutes because
watch_rating_sync's reconciliation trusts Kodi's local playcount -- so the repair clears the bad
marks at the source (Kodi) AND in Chronicle in the same run, Chronicle first.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcvfs

from lib import library_repair


def _ep(episodeid, playcount, lastplayed, season=1, episode=None, file=None):
    return {'episodeid': episodeid, 'title': 'E{0}'.format(episodeid), 'season': season,
            'episode': episode or episodeid, 'playcount': playcount, 'lastplayed': lastplayed,
            'file': file or 'smb://nas/tv/Show/Season 1/S01E{0:02d}.mkv'.format(episodeid)}


def _fake_jsonrpc(shows):
    """shows: {tvshowid: (title, [episodes])} -- serves GetTVShows/GetEpisodes and records
    every SetEpisodeDetails call in .set_calls."""
    calls = []

    def fake(method, params):
        if method == 'VideoLibrary.GetTVShows':
            return {'tvshows': [{'tvshowid': i, 'title': t} for i, (t, _) in shows.items()]}
        if method == 'VideoLibrary.GetEpisodes':
            return {'episodes': shows[params['tvshowid']][1]}
        if method == 'VideoLibrary.SetEpisodeDetails':
            calls.append(params)
            return {}
        return {}
    fake.set_calls = calls
    return fake


class TestDetect(unittest.TestCase):

    def test_three_episodes_sharing_one_instant_are_flagged(self):
        shows = {1: ('Stuart', [_ep(i, 1, '2026-09-19 17:10:40') for i in range(1, 4)] + [_ep(4, 0, '')])}
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc(shows)):
            report = library_repair.detect_fabricated_watched()

        self.assertEqual(sorted(e['episodeid'] for e in report['episodes']), [1, 2, 3])
        self.assertEqual(report['groups'], [{'show_name': 'Stuart', 'count': 3, 'lastplayed': '2026-09-19 17:10:40'}])

    def test_two_sharing_an_instant_is_not_enough(self):
        shows = {1: ('Show', [_ep(1, 1, '2026-09-19 17:10:40'), _ep(2, 1, '2026-09-19 17:10:40')])}
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc(shows)):
            self.assertEqual(library_repair.detect_fabricated_watched()['episodes'], [])

    def test_distinct_real_watch_times_never_flagged(self):
        shows = {1: ('Show', [_ep(i, 1, '2026-09-1{0} 20:00:00'.format(i)) for i in range(1, 6)])}
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc(shows)):
            self.assertEqual(library_repair.detect_fabricated_watched()['episodes'], [])

    def test_only_the_colliding_group_is_flagged_genuine_watches_left_alone(self):
        # Spider-Noir's real shape: 3 stamped with one instant, 2 genuinely watched separately.
        eps = [_ep(1, 1, '2026-07-29 12:20:07'), _ep(2, 1, '2026-07-29 12:20:08'),
               _ep(3, 1, '2026-09-14 11:43:37'), _ep(4, 1, '2026-09-14 11:43:37'), _ep(5, 1, '2026-09-14 11:43:37')]
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc({1: ('Spider-Noir', eps)})):
            report = library_repair.detect_fabricated_watched()

        self.assertEqual(sorted(e['episodeid'] for e in report['episodes']), [3, 4, 5])

    def test_same_instant_across_different_shows_is_not_grouped_together(self):
        shows = {1: ('A', [_ep(1, 1, '2026-09-19 17:10:40'), _ep(2, 1, '2026-09-19 17:10:40')]),
                 2: ('B', [_ep(3, 1, '2026-09-19 17:10:40'), _ep(4, 1, '2026-09-19 17:10:40')])}
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc(shows)):
            self.assertEqual(library_repair.detect_fabricated_watched()['episodes'], [])


class TestRepairAndUndo(unittest.TestCase):

    def setUp(self):
        self.backup_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.backup_dir, True)
        p = patch('lib.library_repair._backup_dir', return_value=self.backup_dir)
        p.start()
        self.addCleanup(p.stop)
        self.report = {'episodes': [
            {'episodeid': 10 + i, 'season': 1, 'episode': i, 'playcount': 1,
             'lastplayed': '2026-09-19 17:10:40', 'file': 'smb://nas/tv/Show/Season 1/S01E0{0}.mkv'.format(i)}
            for i in range(1, 4)]}

    def _client(self, reachable=True, reset_ok=True, media_item_ids=None):
        client = MagicMock()
        client.test_connection.return_value = (reachable, '' if reachable else 'down')
        ids = media_item_ids or {}
        client.get_episode_details_by_file.side_effect = \
            lambda name, season=None, episode=None: {'mediaItemId': ids.get(episode, 500 + episode)}
        client.reset_watch_progress.return_value = reset_ok
        return client

    def test_chronicle_is_reset_before_kodi_is_cleared_for_every_episode(self):
        order = []
        client = self._client()
        client.reset_watch_progress.side_effect = lambda mid: order.append(('chronicle', mid)) or True
        fake = _fake_jsonrpc({})
        real = fake

        def recording(method, params):
            if method == 'VideoLibrary.SetEpisodeDetails':
                order.append(('kodi', params['episodeid']))
            return real(method, params)

        with patch('lib.library_repair._jsonrpc', recording):
            result = library_repair.repair_fabricated_watched(self.report, self.backup_dir, client=client)

        self.assertEqual(result, {'cleared': 3, 'chronicle_reset': 3, 'skipped': 0})
        # Each episode: its Chronicle reset strictly precedes its own Kodi clear.
        for i in range(1, 4):
            self.assertLess(order.index(('chronicle', 500 + i)), order.index(('kodi', 10 + i)))

    def test_kodi_is_cleared_with_playcount_zero_and_empty_lastplayed(self):
        fake = _fake_jsonrpc({})
        with patch('lib.library_repair._jsonrpc', fake):
            library_repair.repair_fabricated_watched(self.report, self.backup_dir, client=self._client())

        self.assertEqual(fake.set_calls[0], {'episodeid': 11, 'playcount': 0, 'lastplayed': ''})

    def test_failed_chronicle_reset_leaves_that_episode_untouched_in_kodi(self):
        # Clearing Kodi anyway would let the next reconciliation push Chronicle's still-watched
        # state straight back down.
        fake = _fake_jsonrpc({})
        with patch('lib.library_repair._jsonrpc', fake):
            result = library_repair.repair_fabricated_watched(
                self.report, self.backup_dir, client=self._client(reset_ok=False))

        self.assertEqual(result, {'cleared': 0, 'chronicle_reset': 0, 'skipped': 3})
        self.assertEqual(fake.set_calls, [])

    def test_episode_chronicle_has_no_record_of_is_still_cleared_in_kodi(self):
        client = self._client()
        client.get_episode_details_by_file.side_effect = lambda *a, **k: None
        fake = _fake_jsonrpc({})
        with patch('lib.library_repair._jsonrpc', fake):
            result = library_repair.repair_fabricated_watched(self.report, self.backup_dir, client=client)

        self.assertEqual(result, {'cleared': 3, 'chronicle_reset': 0, 'skipped': 0})
        client.reset_watch_progress.assert_not_called()

    def test_unreachable_chronicle_aborts_before_any_change(self):
        fake = _fake_jsonrpc({})
        with patch('lib.library_repair._jsonrpc', fake):
            with self.assertRaises(library_repair.LibraryRepairError) as ctx:
                library_repair.repair_fabricated_watched(
                    self.report, self.backup_dir, client=self._client(reachable=False))

        self.assertEqual(ctx.exception.reason_code, 'chronicle_unreachable')
        self.assertEqual(fake.set_calls, [])
        self.assertFalse(os.path.exists(library_repair._fabricated_manifest_path(self.backup_dir)))

    def test_scan_in_progress_aborts_before_any_change(self):
        fake = _fake_jsonrpc({})
        with patch('lib.library_repair._jsonrpc', fake), patch('lib.library_repair._is_scanning', return_value=True):
            with self.assertRaises(library_repair.LibraryRepairError) as ctx:
                library_repair.repair_fabricated_watched(self.report, self.backup_dir, client=self._client())

        self.assertEqual(ctx.exception.reason_code, 'scanning')
        self.assertEqual(fake.set_calls, [])

    def test_manifest_records_prior_values_before_anything_changes(self):
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc({})):
            library_repair.repair_fabricated_watched(self.report, self.backup_dir, client=self._client())

        manifest = json.load(open(library_repair._fabricated_manifest_path(self.backup_dir)))
        self.assertEqual(manifest['episodes'][0], {'episodeid': 11, 'playcount': 1, 'lastplayed': '2026-09-19 17:10:40'})
        self.assertEqual(len(manifest['episodes']), 3)

    def test_undo_restores_prior_values_and_removes_the_manifest(self):
        with patch('lib.library_repair._jsonrpc', _fake_jsonrpc({})):
            library_repair.repair_fabricated_watched(self.report, self.backup_dir, client=self._client())
        fake = _fake_jsonrpc({})
        with patch('lib.library_repair._jsonrpc', fake):
            result = library_repair.undo_fabricated_watched(self.backup_dir)

        self.assertEqual(result, {'restored': 3, 'errors': 0})
        self.assertEqual(fake.set_calls[0], {'episodeid': 11, 'playcount': 1, 'lastplayed': '2026-09-19 17:10:40'})
        self.assertIsNone(library_repair.peek_fabricated_manifest())

    def test_run_undo_with_nothing_to_undo_still_raises_no_manifest(self):
        with patch('lib.library_repair._acquire_lock'), patch('lib.library_repair._release_lock'), \
             patch('lib.library_repair.find_video_db_path', return_value=None):
            with self.assertRaises(library_repair.LibraryRepairError):
                library_repair.run_undo()


class TestPicker(unittest.TestCase):

    REPORT = {
        'episodes': [
            {'episodeid': 1, 'show_name': 'Ted Lasso', 'lastplayed': '2026-08-04 08:42:02'},
            {'episodeid': 2, 'show_name': 'Ted Lasso', 'lastplayed': '2026-08-04 08:42:02'},
            {'episodeid': 3, 'show_name': 'Ted Lasso', 'lastplayed': '2026-08-04 08:42:02'},
            {'episodeid': 4, 'show_name': 'Stuart', 'lastplayed': '2026-09-19 17:10:40'},
            {'episodeid': 5, 'show_name': 'Stuart', 'lastplayed': '2026-09-19 17:10:40'},
            {'episodeid': 6, 'show_name': 'Stuart', 'lastplayed': '2026-09-19 17:10:40'},
            {'episodeid': 7, 'show_name': 'Stuart', 'lastplayed': '2026-09-19 17:10:40'},
        ],
        'groups': [{'show_name': 'Ted Lasso', 'count': 3, 'lastplayed': 'x'},
                   {'show_name': 'Stuart', 'count': 4, 'lastplayed': 'y'}],
    }

    def test_summary_is_per_show_biggest_first_with_date(self):
        rows = library_repair.summarize_fabricated_by_show(self.REPORT)
        self.assertEqual(rows, [{'show_name': 'Stuart', 'count': 4, 'when': '2026-09-19'},
                                {'show_name': 'Ted Lasso', 'count': 3, 'when': '2026-08-04'}])

    def test_filter_keeps_only_chosen_shows(self):
        out = library_repair.filter_fabricated_to_shows(self.REPORT, ['Stuart'])
        self.assertEqual([e['episodeid'] for e in out['episodes']], [4, 5, 6, 7])
        self.assertEqual([g['show_name'] for g in out['groups']], ['Stuart'])

    def test_choosing_nothing_leaves_nothing_to_reset(self):
        self.assertEqual(library_repair.filter_fabricated_to_shows(self.REPORT, [])['episodes'], [])


if __name__ == '__main__':
    unittest.main()
