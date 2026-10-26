# -*- coding: utf-8 -*-
"""
Regression tests for lib/kodi_scan_signal.py's check_and_refresh() -- the per-item metadata
refresh-push feature (2026-09-18): polls Chronicle's kodi-refresh-signal for items this device
already knows about whose own metadata has changed since it last scraped them, and fires one
local VideoLibrary.Refresh* per item. See that function's own module doc (Per-item refresh-push
signal) for the full design.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import kodi_scan_signal


def _rpc_calls(mock_rpc):
    return [json.loads(c.args[0]) for c in mock_rpc.call_args_list]


class TestCheckAndRefresh(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        import xbmc
        self.addCleanup(setattr, xbmc, 'executeJSONRPC', xbmc.executeJSONRPC)   # these tests replace it

    def test_no_items_due_fires_no_rpc_calls(self):
        mock_client = MagicMock()
        mock_client.get_refresh_signal.return_value = []

        with patch.object(kodi_scan_signal, 'ChronicleClient', return_value=mock_client):
            import xbmc
            xbmc.executeJSONRPC = MagicMock(return_value='{"result": "OK"}')
            kodi_scan_signal.check_and_refresh(['movie'])

        xbmc.executeJSONRPC.assert_not_called()

    def test_movie_item_fires_refresh_movie_with_the_right_param(self):
        mock_client = MagicMock()
        mock_client.get_refresh_signal.return_value = [{'kind': 'movie', 'kodiId': 42}]

        import xbmc
        xbmc.executeJSONRPC = MagicMock(return_value='{"result": "OK"}')
        with patch.object(kodi_scan_signal, 'ChronicleClient', return_value=mock_client):
            kodi_scan_signal.check_and_refresh(['movie'])

        calls = _rpc_calls(xbmc.executeJSONRPC)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['method'], 'VideoLibrary.RefreshMovie')
        self.assertEqual(calls[0]['params'], {'movieid': 42})

    def test_episode_and_tvshow_items_use_their_own_method_and_param(self):
        mock_client = MagicMock()
        mock_client.get_refresh_signal.return_value = [
            {'kind': 'episode', 'kodiId': 7},
            {'kind': 'tvshow', 'kodiId': 3},
        ]

        import xbmc
        xbmc.executeJSONRPC = MagicMock(return_value='{"result": "OK"}')
        with patch.object(kodi_scan_signal, 'ChronicleClient', return_value=mock_client):
            kodi_scan_signal.check_and_refresh(['episode', 'tvshow'])

        calls = _rpc_calls(xbmc.executeJSONRPC)
        methods = {(c['method'], tuple(c['params'].items())) for c in calls}
        self.assertEqual(methods, {
            ('VideoLibrary.RefreshEpisode', (('episodeid', 7),)),
            ('VideoLibrary.RefreshTVShow', (('tvshowid', 3),)),
        })

    def test_unrecognized_kind_is_skipped_not_fatal(self):
        mock_client = MagicMock()
        mock_client.get_refresh_signal.return_value = [
            {'kind': 'album', 'kodiId': 1},  # this addon has no music-refresh mapping
            {'kind': 'movie', 'kodiId': 42},
        ]

        import xbmc
        xbmc.executeJSONRPC = MagicMock(return_value='{"result": "OK"}')
        with patch.object(kodi_scan_signal, 'ChronicleClient', return_value=mock_client):
            kodi_scan_signal.check_and_refresh(['movie', 'album'])

        calls = _rpc_calls(xbmc.executeJSONRPC)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['method'], 'VideoLibrary.RefreshMovie')

    def test_one_item_failing_does_not_stop_the_rest(self):
        mock_client = MagicMock()
        mock_client.get_refresh_signal.return_value = [
            {'kind': 'movie', 'kodiId': 1},
            {'kind': 'movie', 'kodiId': 2},
        ]

        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=[
            Exception('transient network error'),
            '{"result": "OK"}',
        ])
        with patch.object(kodi_scan_signal, 'ChronicleClient', return_value=mock_client):
            kodi_scan_signal.check_and_refresh(['movie'])

        self.assertEqual(xbmc.executeJSONRPC.call_count, 2)

    def test_client_failure_is_caught_not_raised(self):
        mock_client = MagicMock()
        mock_client.get_refresh_signal.side_effect = Exception('Chronicle unreachable')

        with patch.object(kodi_scan_signal, 'ChronicleClient', return_value=mock_client):
            kodi_scan_signal.check_and_refresh(['movie'])  # must not raise


class TestRunStartupRefresh(unittest.TestCase):
    """The once-per-startup refresh: waits for Kodi's OWN startup scan, never starts one."""

    def setUp(self):
        import xbmc
        self.xbmc = xbmc
        self.addCleanup(setattr, xbmc, 'executeJSONRPC', xbmc.executeJSONRPC)
        self.setting = True
        self.scanning = [False]

        def rpc(raw):
            request = json.loads(raw)
            if request['method'] == 'Settings.GetSettingValue':
                return json.dumps({'result': {'value': self.setting}})
            return '{"result": "OK"}'
        xbmc.executeJSONRPC = MagicMock(side_effect=rpc)
        for obj, name, new in ((xbmc, 'getCondVisibility', MagicMock(side_effect=lambda c: self.scanning[0])),
                               (kodi_scan_signal, '_SCAN_FINISH_STARTED_TIMEOUT_SECONDS', 0.1),
                               (kodi_scan_signal, '_SCAN_FINISH_STARTED_POLL_SECONDS', 0.01),
                               (kodi_scan_signal, '_SCAN_FINISH_POLL_SECONDS', 0.01)):
            p = patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(kodi_scan_signal, 'check_and_refresh')
        self.refresh = p.start()
        self.addCleanup(p.stop)

    def methods(self):
        return [json.loads(c.args[0])['method'] for c in self.xbmc.executeJSONRPC.call_args_list]

    def test_refreshes_the_given_kinds_when_the_native_startup_scan_is_on(self):
        kodi_scan_signal.run_startup_refresh(['movie'])
        self.refresh.assert_called_once_with(['movie'])

    def test_never_starts_a_scan(self):
        kodi_scan_signal.run_startup_refresh(['movie'])
        self.assertNotIn('VideoLibrary.Scan', self.methods())

    def test_does_nothing_when_the_native_startup_scan_is_off(self):
        self.setting = False
        kodi_scan_signal.run_startup_refresh(['movie'])
        self.refresh.assert_not_called()

    def test_does_nothing_when_the_setting_cannot_be_read(self):
        self.setting = None
        kodi_scan_signal.run_startup_refresh(['movie'])
        self.refresh.assert_not_called()

    def test_waits_for_a_running_startup_scan_to_finish_first(self):
        states = iter([True, True, False])        # scan seen starting, still running, then done

        def scanning(cond):
            try:
                self.scanning[0] = next(states)
            except StopIteration:
                self.scanning[0] = False
            return self.scanning[0]
        self.xbmc.getCondVisibility.side_effect = scanning
        order = []
        self.refresh.side_effect = lambda kinds: order.append(('refresh', self.scanning[0]))

        kodi_scan_signal.run_startup_refresh(['movie'])

        self.assertEqual(order, [('refresh', False)])

    def test_skipped_if_kodi_is_shutting_down_during_the_wait(self):
        self.scanning[0] = True
        kodi_scan_signal.run_startup_refresh(['movie'], is_aborted=lambda: True)
        self.refresh.assert_not_called()

    def test_a_failure_is_swallowed(self):
        self.refresh.side_effect = RuntimeError('boom')
        kodi_scan_signal.run_startup_refresh(['movie'])


if __name__ == '__main__':
    unittest.main()
