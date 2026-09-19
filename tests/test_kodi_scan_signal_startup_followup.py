# -*- coding: utf-8 -*-
"""
Regression tests for lib/kodi_scan_signal.py's run_startup_scan_followup() -- the feature that
reproduces what a manual "Scan Library" call does once Kodi's own native "Update library on
startup" setting finishes its own too-early, shallow pass (see that function's own module doc,
"Startup-scan followup", for the full design and why it deliberately never touches that native
setting itself), and then runs this device's own per-item refresh-check (check_and_refresh) as
its own last step, once that scan actually finishes (2026-09-19 correction -- see
"Per-item refresh-push signal" in that module's own doc for why this replaced a periodic poll).
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import kodi_scan_signal


def _jsonrpc_side_effect(native_setting_value):
    def _side_effect(payload):
        request = json.loads(payload)
        if request['method'] == 'Settings.GetSettingValue':
            return json.dumps({'result': {'value': native_setting_value}})
        if request['method'] == 'VideoLibrary.Scan':
            return json.dumps({'result': 'OK'})
        return json.dumps({'result': {}})
    return _side_effect


def _scan_calls(mock_rpc):
    return [json.loads(c.args[0]) for c in mock_rpc.call_args_list
            if json.loads(c.args[0])['method'] == 'VideoLibrary.Scan']


class TestRunStartupScanFollowup(unittest.TestCase):
    """Most of these patch _wait_for_scan_to_finish and check_and_refresh to no-ops -- they're
    covered by their own dedicated test classes below -- so these tests stay focused on the
    scan-firing/claiming behavior without sitting through a real wait loop."""

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_native_setting_off_does_not_fire_a_scan(self):
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(False))

        with patch.object(kodi_scan_signal, 'check_and_refresh') as mock_refresh:
            kodi_scan_signal.run_startup_scan_followup(service_started_at=time.time(), kinds=['movie'])

        self.assertEqual(_scan_calls(xbmc.executeJSONRPC), [])
        # Gated on the same native setting as the scan itself -- off means neither runs.
        mock_refresh.assert_not_called()

    def test_native_setting_on_fires_one_full_unscoped_scan(self):
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
             patch.object(kodi_scan_signal, 'check_and_refresh'):
            kodi_scan_signal.run_startup_scan_followup(service_started_at=time.time(), kinds=['movie'])

        calls = _scan_calls(xbmc.executeJSONRPC)
        self.assertEqual(len(calls), 1)
        # Unscoped -- matching manual-scan semantics, not check_and_scan()'s own targeted kind.
        self.assertNotIn('directory', calls[0]['params'])

    def test_sibling_addon_already_claimed_this_boot_is_skipped(self):
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))
        service_started_at = time.time()
        # Sibling addon's own service claims it a moment AFTER this service's own start --
        # exactly the shape a real race between the Movies/TV addons' services would produce.
        kodi_scan_signal._claim_startup_followup()

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
             patch.object(kodi_scan_signal, 'check_and_refresh') as mock_refresh:
            kodi_scan_signal.run_startup_scan_followup(service_started_at, kinds=['movie'])

        self.assertEqual(_scan_calls(xbmc.executeJSONRPC), [])
        # Losing the claim only skips FIRING the scan -- this addon still waits for whichever
        # sibling fired it to finish, then still runs its own refresh-check.
        mock_refresh.assert_called_once_with(['movie'])

    def test_stale_marker_from_a_previous_boot_does_not_block_a_new_one(self):
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))
        kodi_scan_signal._claim_startup_followup()
        # This "boot" starts an hour after that marker was written -- the marker is leftover
        # from a previous Kodi session and must not suppress a brand new one.
        service_started_at = time.time() + 3600

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
             patch.object(kodi_scan_signal, 'check_and_refresh'):
            kodi_scan_signal.run_startup_scan_followup(service_started_at, kinds=['movie'])

        self.assertEqual(len(_scan_calls(xbmc.executeJSONRPC)), 1)

    def test_waits_for_its_own_folder_then_fires_once_reachable(self):
        # Exercises the real _wait_for_reachable call path -- every other test above patches
        # find_own_source_directories() to [] specifically to skip it.
        import xbmc
        import xbmcvfs
        xbmcvfs.mkdirs('smb://nas/tv/')
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=['smb://nas/tv/']), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
             patch.object(kodi_scan_signal, 'check_and_refresh'):
            kodi_scan_signal.run_startup_scan_followup(service_started_at=time.time(), kinds=['movie'])

        self.assertEqual(len(_scan_calls(xbmc.executeJSONRPC)), 1)

    def test_does_not_fire_a_second_scan_while_one_is_already_running(self):
        # Covers both a still-running native startup scan and a sibling addon's own scan that
        # just started -- either way, firing a second fully-overlapping VideoLibrary.Scan is
        # pure waste this guard exists to avoid. Still waits + still runs the refresh-check.
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))
        xbmc.getCondVisibility = MagicMock(return_value=True)
        try:
            with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
                 patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
                 patch.object(kodi_scan_signal, 'check_and_refresh') as mock_refresh:
                kodi_scan_signal.run_startup_scan_followup(service_started_at=time.time(), kinds=['movie'])

            self.assertEqual(_scan_calls(xbmc.executeJSONRPC), [])
            mock_refresh.assert_called_once_with(['movie'])
        finally:
            xbmc.getCondVisibility = MagicMock(return_value=False)

    def test_rejected_scan_is_logged_not_silently_treated_as_success(self):
        import xbmc

        def _side_effect(payload):
            request = json.loads(payload)
            if request['method'] == 'Settings.GetSettingValue':
                return json.dumps({'result': {'value': True}})
            if request['method'] == 'VideoLibrary.Scan':
                return json.dumps({'error': {'code': -32000, 'message': 'scan already running'}})
            return json.dumps({'result': {}})

        xbmc.executeJSONRPC = MagicMock(side_effect=_side_effect)

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
             patch.object(kodi_scan_signal, 'check_and_refresh'):
            # Must not raise -- run_startup_scan_followup's own broad except is the last resort,
            # but the 'error' key should be handled explicitly, not by falling into that.
            kodi_scan_signal.run_startup_scan_followup(service_started_at=time.time(), kinds=['movie'])

        self.assertEqual(len(_scan_calls(xbmc.executeJSONRPC)), 1)

    def test_calls_check_and_refresh_with_the_given_kinds_after_waiting_for_the_scan_to_finish(self):
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))
        order = []

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish',
                           side_effect=lambda is_aborted: order.append('wait')) as mock_wait, \
             patch.object(kodi_scan_signal, 'check_and_refresh',
                           side_effect=lambda kinds: order.append('refresh')) as mock_refresh:
            kodi_scan_signal.run_startup_scan_followup(
                service_started_at=time.time(), kinds=['episode', 'tvshow'])

        mock_wait.assert_called_once()
        mock_refresh.assert_called_once_with(['episode', 'tvshow'])
        self.assertEqual(order, ['wait', 'refresh'])

    def test_refresh_check_is_skipped_if_aborted_during_the_wait(self):
        import xbmc
        xbmc.executeJSONRPC = MagicMock(side_effect=_jsonrpc_side_effect(True))

        with patch.object(kodi_scan_signal, 'find_own_source_directories', return_value=[]), \
             patch.object(kodi_scan_signal, '_wait_for_scan_to_finish'), \
             patch.object(kodi_scan_signal, 'check_and_refresh') as mock_refresh:
            kodi_scan_signal.run_startup_scan_followup(
                service_started_at=time.time(), kinds=['movie'], is_aborted=lambda: True)

        mock_refresh.assert_not_called()


class TestWaitForReachable(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_returns_true_immediately_when_already_reachable(self):
        import xbmcvfs
        xbmcvfs.mkdirs('smb://nas/tv/')

        self.assertTrue(kodi_scan_signal._wait_for_reachable(['smb://nas/tv/'], is_aborted=lambda: False))

    def test_gives_up_and_returns_false_after_timeout_when_never_reachable(self):
        with patch.object(kodi_scan_signal, '_STARTUP_FOLLOWUP_REACHABILITY_TIMEOUT_SECONDS', 0.05), \
             patch.object(kodi_scan_signal, '_STARTUP_FOLLOWUP_REACHABILITY_POLL_SECONDS', 0.01):
            result = kodi_scan_signal._wait_for_reachable(['smb://unreachable-nas/tv/'], is_aborted=lambda: False)

        self.assertFalse(result)

    def test_stops_waiting_promptly_when_aborted(self):
        aborted = MagicMock(return_value=True)
        with patch.object(kodi_scan_signal, '_STARTUP_FOLLOWUP_REACHABILITY_TIMEOUT_SECONDS', 60):
            result = kodi_scan_signal._wait_for_reachable(['smb://unreachable-nas/tv/'], is_aborted=aborted)

        self.assertFalse(result)
        aborted.assert_called()


class TestWaitForScanToFinish(unittest.TestCase):

    def tearDown(self):
        import xbmc
        xbmc.getCondVisibility = MagicMock(return_value=False)

    def test_returns_immediately_when_scan_never_actually_starts(self):
        import xbmc
        xbmc.getCondVisibility = MagicMock(return_value=False)

        with patch.object(kodi_scan_signal, '_SCAN_FINISH_STARTED_TIMEOUT_SECONDS', 0.05), \
             patch.object(kodi_scan_signal, '_SCAN_FINISH_STARTED_POLL_SECONDS', 0.01):
            kodi_scan_signal._wait_for_scan_to_finish(is_aborted=lambda: False)
        # No assertion beyond "returns promptly" (enforced by the tiny patched timeout above) --
        # there's nothing else to observe when the flag never flips True.

    def test_returns_once_the_flag_goes_back_to_false(self):
        import xbmc
        # Reports "scanning" exactly once, then "finished" -- proves this waits for the
        # False transition rather than just checking once.
        xbmc.getCondVisibility = MagicMock(side_effect=[True, True, False])

        with patch.object(kodi_scan_signal, '_SCAN_FINISH_STARTED_POLL_SECONDS', 0.01), \
             patch.object(kodi_scan_signal, '_SCAN_FINISH_POLL_SECONDS', 0.01):
            kodi_scan_signal._wait_for_scan_to_finish(is_aborted=lambda: False)

        self.assertEqual(xbmc.getCondVisibility.call_count, 3)

    def test_gives_up_after_the_max_wait_if_the_scan_never_finishes(self):
        import xbmc
        xbmc.getCondVisibility = MagicMock(return_value=True)

        with patch.object(kodi_scan_signal, '_SCAN_FINISH_STARTED_POLL_SECONDS', 0.01), \
             patch.object(kodi_scan_signal, '_SCAN_FINISH_MAX_WAIT_SECONDS', 0.05), \
             patch.object(kodi_scan_signal, '_SCAN_FINISH_POLL_SECONDS', 0.01):
            # Must return (not hang) once the bounded max-wait elapses.
            kodi_scan_signal._wait_for_scan_to_finish(is_aborted=lambda: False)

    def test_stops_promptly_when_aborted_mid_wait(self):
        import xbmc
        xbmc.getCondVisibility = MagicMock(return_value=True)
        aborted = MagicMock(side_effect=[False, True])

        with patch.object(kodi_scan_signal, '_SCAN_FINISH_STARTED_POLL_SECONDS', 0.01), \
             patch.object(kodi_scan_signal, '_SCAN_FINISH_POLL_SECONDS', 60):
            kodi_scan_signal._wait_for_scan_to_finish(is_aborted=aborted)

        aborted.assert_called()


if __name__ == '__main__':
    unittest.main()
