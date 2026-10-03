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


if __name__ == '__main__':
    unittest.main()
