# -*- coding: utf-8 -*-
"""
Regression tests for service.py's wiring (added 2026-10-03).

v3.19.1 shipped with the scan-progress block pasted into ChronicleMonitor._should_defer_for_active_scan
instead of the run() loop it was written for. That method reads `was_scanning`, a local that only run()
defines, so it raised UnboundLocalError on its first call -- which is the first thing every scan-gated
background task does, including the startup watch/rating sync -- and killed the whole service at
startup (seen live on the upstairs Shield: "cannot access local variable 'was_scanning'"). Nothing
imported service.py or called that method, so the 146 existing tests passed.
"""
import ast
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

import service

_SERVICE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'service.py')

# State that exists only inside run()'s idle loop.
_RUN_ONLY_NAMES = {'was_scanning', 'last_scan_progress', 'last_scan_active_heartbeat'}


class TestShouldDeferForActiveScan(unittest.TestCase):
    """The method that crashed: every scan-gated task calls it first."""

    def setUp(self):
        self.monitor = service.ChronicleMonitor()

    def test_runs_and_returns_false_when_nothing_is_happening(self):
        with patch.object(service.xbmc, 'getCondVisibility', return_value=False), \
             patch.object(service.activity_tracker, 'is_recently_active', return_value=False):
            self.assertFalse(self.monitor._should_defer_for_active_scan('watch/rating sync (startup)'))

    def test_defers_during_a_kodi_scan(self):
        with patch.object(service.xbmc, 'getCondVisibility', return_value=True), \
             patch.object(service.activity_tracker, 'is_recently_active', return_value=False), \
             patch.object(service.xbmcgui, 'Dialog'):
            self.assertTrue(self.monitor._should_defer_for_active_scan('watch/rating sync (startup)'))

    def test_defers_during_scraper_activity(self):
        with patch.object(service.xbmc, 'getCondVisibility', return_value=False), \
             patch.object(service.activity_tracker, 'is_recently_active', return_value=True), \
             patch.object(service.xbmcgui, 'Dialog'):
            self.assertTrue(self.monitor._should_defer_for_active_scan('collection art sync (startup)'))


class TestScanProgressIsWiredIntoTheRunLoop(unittest.TestCase):

    def setUp(self):
        with open(_SERVICE_PATH, encoding='utf-8') as f:
            self.tree = ast.parse(f.read())

    def _function(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail('no function named {0}'.format(name))

    @staticmethod
    def _names_used(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def test_run_calls_scan_progress_begin_and_tick(self):
        used = {(n.value.id, n.attr) for n in ast.walk(self._function('run'))
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        self.assertIn(('scan_progress', 'begin'), used)
        self.assertIn(('scan_progress', 'tick'), used)

    def test_run_loop_state_is_not_referenced_by_the_monitor_class(self):
        monitor_class = next(n for n in ast.walk(self.tree)
                             if isinstance(n, ast.ClassDef) and n.name == 'ChronicleMonitor')
        leaked = self._names_used(monitor_class) & _RUN_ONLY_NAMES
        self.assertEqual(leaked, set(),
                         'run()-only locals referenced inside ChronicleMonitor: {0}'.format(sorted(leaked)))


class TestNoUndefinedNames(unittest.TestCase):
    """pyflakes flags exactly this class of mistake (a name used where it is not defined, or read
    before assignment). Skipped where pyflakes isn't installed."""

    def test_service_has_no_undefined_or_unassigned_names(self):
        try:
            from pyflakes import api, reporter
        except ImportError:
            self.skipTest('pyflakes not installed')
        import io
        out, err = io.StringIO(), io.StringIO()
        with open(_SERVICE_PATH, encoding='utf-8') as f:
            api.check(f.read(), _SERVICE_PATH, reporter.Reporter(out, err))
        serious = [ln for ln in out.getvalue().splitlines()
                   if 'undefined name' in ln or 'referenced before assignment' in ln]
        self.assertEqual(serious, [])


class TestPassesAlwaysReportProgress(unittest.TestCase):
    """Per-user requirement (2026-10-03): the passes that write into the library (and so make Kodi's
    screens refresh) must say what they are doing. Each long pass has to feed the shared progress
    reporter; a pass whose run() call has no progress_callback would work silently again."""

    def setUp(self):
        with open(_SERVICE_PATH, encoding='utf-8') as f:
            self.tree = ast.parse(f.read())

    def _calls_to(self, module, func):
        return [n for n in ast.walk(self.tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == func and isinstance(n.func.value, ast.Name) and n.func.value.id == module]

    def test_the_post_scan_check_reports_progress(self):
        calls = self._calls_to('full_sync_check', 'run')
        self.assertTrue(calls, 'service.py no longer calls full_sync_check.run')
        for call in calls:
            self.assertIn('progress_callback', {k.arg for k in call.keywords})

    def test_the_watch_rating_sync_reports_progress(self):
        calls = self._calls_to('watch_rating_sync', 'run')
        self.assertTrue(calls, 'service.py no longer calls watch_rating_sync.run')
        for call in calls:
            self.assertIn('progress_callback', {k.arg for k in call.keywords})

    def test_passes_use_the_shared_reporter(self):
        self.assertGreaterEqual(len(self._calls_to('pass_progress', 'PassProgress')), 2)


class TestFullSyncCheckResultNotification(unittest.TestCase):

    def setUp(self):
        self.monitor = service.ChronicleMonitor()

    def _notify(self, result):
        with patch.object(service.xbmcgui, 'Dialog') as dialog:
            self.monitor._notify_full_sync_check_result(result)
        return dialog.return_value.notification

    def test_a_completed_pass_says_how_many_were_checked_and_corrected(self):
        notification = self._notify({'checked': 700, 'updated': 12, 'errors': 0, 'cancelled': False, 'aborted': None})
        notification.assert_called_once()
        self.assertEqual(notification.call_args.kwargs['icon'], service.xbmcgui.NOTIFICATION_INFO)
        # titled with the add-on name (string 32000), not the "Verifying..." heading of the pass that just ended
        service.ADDON.getLocalizedString.assert_any_call(32000)

    def test_a_pass_with_errors_is_a_warning(self):
        notification = self._notify({'checked': 700, 'updated': 12, 'errors': 3, 'cancelled': False, 'aborted': None})
        self.assertEqual(notification.call_args.kwargs['icon'], service.xbmcgui.NOTIFICATION_WARNING)

    def test_a_pass_stopped_early_still_says_so(self):
        self._notify({'checked': 5, 'updated': 0, 'errors': 0, 'cancelled': True, 'aborted': None}).assert_called_once()

    def test_a_pass_that_could_not_start_is_a_warning(self):
        notification = self._notify({'checked': 0, 'updated': 0, 'errors': 0, 'cancelled': False,
                                     'aborted': 'Chronicle not reachable: timed out'})
        self.assertEqual(notification.call_args.kwargs['icon'], service.xbmcgui.NOTIFICATION_WARNING)

    def test_a_notification_failure_never_propagates(self):
        with patch.object(service.xbmcgui, 'Dialog', side_effect=RuntimeError('no GUI')):
            self.monitor._notify_full_sync_check_result({'checked': 1, 'updated': 0, 'errors': 0,
                                                         'cancelled': False, 'aborted': None})   # must not raise


if __name__ == '__main__':
    unittest.main()
