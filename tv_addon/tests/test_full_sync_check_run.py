# -*- coding: utf-8 -*-
"""TV full_sync_check.run(): the post-scan pass over every episode Kodi holds."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import full_sync_check as fsc


class TestRpcHelpers(unittest.TestCase):
    def _rpc(self, payload=None, exc=None):
        if exc:
            return mock.patch.object(xbmc, 'executeJSONRPC', side_effect=exc)
        return mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps(payload))

    def test_shows_and_episodes_tolerate_every_failure(self):
        with self._rpc({'result': {'tvshows': [{'tvshowid': 1}]}}):
            self.assertEqual(fsc._get_all_tvshows(), [{'tvshowid': 1}])
        with self._rpc({'result': {'episodes': [{'episodeid': 1}]}}):
            self.assertEqual(fsc._get_episodes_for_show(1), [{'episodeid': 1}])
        for call in (lambda: fsc._get_all_tvshows(), lambda: fsc._get_episodes_for_show(1)):
            with self._rpc({'error': {'message': 'no'}}):
                self.assertEqual(call(), [])
            with self._rpc({'result': {}}):
                self.assertEqual(call(), [])
            with self._rpc(exc=RuntimeError('down')):
                self.assertEqual(call(), [])

    def test_set_episode_details(self):
        with self._rpc({'result': 'OK'}) as rpc:
            self.assertTrue(fsc._set_episode_details(7, {'title': 'T'}))
            self.assertEqual(json.loads(rpc.call_args[0][0])['params'], {'title': 'T', 'episodeid': 7})
        with self._rpc({'error': {}}):
            self.assertFalse(fsc._set_episode_details(7, {}))
        with self._rpc(exc=RuntimeError('down')):
            self.assertFalse(fsc._set_episode_details(7, {}))


class TestRun(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.client.test_connection.return_value = (True, '')
        self.episodes = {
            1: [{'episodeid': 11, 'title': 'Pilot', 'season': 1, 'episode': 1, 'file': 'smb://n/TV/Show/S01E01.mkv'},
                {'episodeid': 12, 'title': 'No file', 'season': 1, 'episode': 2}],
            2: [{'episodeid': 21, 'title': 'Unknown', 'season': 1, 'episode': 1, 'file': 'smb://n/TV/Other/S01E01.mkv'}],
        }
        for name, new in (('ChronicleClient', mock.Mock(return_value=self.client)),
                          ('_get_all_tvshows', mock.Mock(return_value=[{'tvshowid': 1}, {'tvshowid': 2}])),
                          ('_get_episodes_for_show', mock.Mock(side_effect=lambda i: self.episodes[i])),
                          ('diff_episode', mock.Mock(return_value={})),
                          ('_set_episode_details', mock.Mock(return_value=True))):
            p = mock.patch.object(fsc, name, new)
            setattr(self, name.lstrip('_'), p.start())
            self.addCleanup(p.stop)
        self.client.get_episode_details_by_file.return_value = None   # nothing known to Chronicle unless a test says so

    def test_unreachable_chronicle_aborts(self):
        self.client.test_connection.return_value = (False, 'refused')
        result = fsc.run()
        self.assertEqual((result['checked'], result['aborted']), (0, 'Chronicle not reachable: refused'))
        self.client.get_episode_details_by_file.assert_not_called()

    def test_files_resolved_by_basename_with_season_and_episode_guards(self):
        fsc.run()
        names = sorted(c[0][0] for c in self.client.get_episode_details_by_file.call_args_list)
        self.assertEqual(names, ['S01E01.mkv', 'S01E01.mkv'])   # the episode without a file is skipped
        self.assertTrue(all(c[1] == {'season': 1, 'episode': 1} for c in self.client.get_episode_details_by_file.call_args_list))

    def test_corrections_written_counted_and_errors_counted(self):
        self.client.get_episode_details_by_file.side_effect = lambda name, season=None, episode=None: {'title': 'x'}
        self.diff_episode.return_value = {'title': 'x'}
        self.set_episode_details.side_effect = [True, False]
        result = fsc.run()
        self.assertEqual((result['checked'], result['updated'], result['errors']), (2, 1, 1))
        self.assertEqual(self.set_episode_details.call_args_list[0][0], (11, {'title': 'x'}))

    def test_matching_episodes_are_checked_but_not_written(self):
        self.client.get_episode_details_by_file.side_effect = lambda name, season=None, episode=None: {'title': 'x'}
        result = fsc.run()
        self.assertEqual((result['checked'], result['updated']), (2, 0))
        self.set_episode_details.assert_not_called()

    def test_progress_and_cancellation(self):
        progress = mock.Mock()
        flags = iter([False, False, False, True, True, True])
        result = fsc.run(is_cancelled=lambda: next(flags, True), progress_callback=progress)
        self.assertTrue(result['cancelled'])
        self.assertEqual(progress.call_args_list[0][0][:2], (0, 3))


if __name__ == '__main__':
    unittest.main()
