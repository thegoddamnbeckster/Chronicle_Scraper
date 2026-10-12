# -*- coding: utf-8 -*-
"""
Regression tests for the periodic watch/rating and collection-art passes being starved by scans (2026-10-11).

A movie rated on one Kodi never reached another: that device's watch/rating pass is gated behind "no scan or scraper
activity right now", but the service loop restarted the pass's whole two-hour interval the moment it merely TRIED, so
a tick that landed in a busy moment (and on a device that scans often most do) cost two hours, and the next tick often
landed in another busy moment. The device's last full pass had been 38 hours earlier. A deferred pass must retry
within minutes; only a pass that actually started restarts its interval.
"""
import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

import service

_SERVICE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'service.py')
_INTERVAL = 120 * 60


class TestAnchorAfterAttempt(unittest.TestCase):

    def test_a_pass_that_started_restarts_the_whole_interval(self):
        self.assertEqual(1000.0, service._anchor_after_attempt(1000.0, _INTERVAL, True))

    def test_a_deferred_pass_is_due_again_in_minutes_not_hours(self):
        now = 1000.0
        anchor = service._anchor_after_attempt(now, _INTERVAL, False)

        # The loop fires when now - anchor >= interval.
        due_in = _INTERVAL - (now - anchor)
        self.assertEqual(service._DEFERRED_RETRY_SECONDS, due_in)
        self.assertLessEqual(due_in, 10 * 60)

    def test_the_retry_never_waits_longer_than_the_interval_itself(self):
        # A very short interval (the setting allows 30 minutes, but be safe): never pushed later than a normal tick.
        anchor = service._anchor_after_attempt(1000.0, 60, False)

        self.assertLessEqual(60 - (1000.0 - anchor), 60)

    def test_a_pass_starved_by_scans_runs_as_soon_as_a_quiet_moment_comes(self):
        # Simulate the loop's check every minute: busy for the first 40 minutes, quiet afterwards.
        now, anchor, ran_at = 0.0, 0.0, None
        busy_until = 40 * 60
        anchor = -_INTERVAL   # a tick is due right now
        while now < 3 * 3600 and ran_at is None:
            if now - anchor >= _INTERVAL:
                started = now >= busy_until
                anchor = service._anchor_after_attempt(now, _INTERVAL, started)
                if started:
                    ran_at = now
            now += 60

        self.assertIsNotNone(ran_at)
        # Within one retry period of the quiet moment, not at the next two-hour tick.
        self.assertLessEqual(ran_at - busy_until, service._DEFERRED_RETRY_SECONDS + 60)


class TestRunReturnsWhetherThePassWasDeferred(unittest.TestCase):

    def setUp(self):
        self.monitor = service.ChronicleMonitor()
        self.thread = patch.object(service.threading, 'Thread', return_value=MagicMock())
        self.thread.start()
        self.addCleanup(self.thread.stop)

    def test_watch_rating_sync_reports_a_deferral(self):
        with patch.object(self.monitor, '_should_defer_for_active_scan', return_value=True):
            self.assertIs(False, self.monitor.run_watch_rating_sync('scheduled'))

    def test_watch_rating_sync_reports_that_it_started(self):
        with patch.object(self.monitor, '_should_defer_for_active_scan', return_value=False):
            self.assertIs(True, self.monitor.run_watch_rating_sync('scheduled'))
        self.monitor._watch_rating_lock.release()   # the (stubbed) worker thread never ran to release it

    def test_watch_rating_sync_counts_a_pass_already_running_as_covered(self):
        self.monitor._watch_rating_lock.acquire()
        self.addCleanup(self.monitor._watch_rating_lock.release)
        with patch.object(self.monitor, '_should_defer_for_active_scan', return_value=False):
            self.assertIs(True, self.monitor.run_watch_rating_sync('scheduled'))

    def test_collection_art_sync_reports_a_deferral(self):
        with patch.object(self.monitor, '_should_defer_for_active_scan', return_value=True):
            self.assertIs(False, self.monitor.run_collection_art_sync('scheduled'))

    def test_collection_art_sync_reports_that_it_started(self):
        with patch.object(self.monitor, '_should_defer_for_active_scan', return_value=False):
            self.assertIs(True, self.monitor.run_collection_art_sync('scheduled'))
        self.monitor._collection_art_lock.release()

    def test_collection_art_sync_counts_a_pass_already_running_as_covered(self):
        self.monitor._collection_art_lock.acquire()
        self.addCleanup(self.monitor._collection_art_lock.release)
        with patch.object(self.monitor, '_should_defer_for_active_scan', return_value=False):
            self.assertIs(True, self.monitor.run_collection_art_sync('scheduled'))


class TestTheRunLoopUsesTheResult(unittest.TestCase):
    """The loop is one large function that cannot be run here, so check its shape: every call of a periodic pass must
    feed the anchor helper, never reset the anchor before (and regardless of) the attempt."""

    def setUp(self):
        with open(_SERVICE_PATH, encoding='utf-8') as f:
            self.tree = ast.parse(f.read())
        self.run = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and n.name == 'run')

    def _calls(self, attr):
        return [n for n in ast.walk(self.run) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == attr]

    def _wrapped_in_anchor_helper(self, call):
        for node in ast.walk(self.run):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_anchor_after_attempt':
                if any(call is sub for sub in ast.walk(node)):
                    return True
        return False

    def test_every_watch_rating_attempt_feeds_the_anchor_helper(self):
        calls = self._calls('run_watch_rating_sync')
        self.assertEqual(2, len(calls))   # startup + scheduled
        for call in calls:
            self.assertTrue(self._wrapped_in_anchor_helper(call), 'a watch/rating attempt does not feed _anchor_after_attempt')

    def test_every_collection_art_attempt_feeds_the_anchor_helper(self):
        calls = self._calls('run_collection_art_sync')
        self.assertEqual(2, len(calls))
        for call in calls:
            self.assertTrue(self._wrapped_in_anchor_helper(call), 'a collection art attempt does not feed _anchor_after_attempt')


if __name__ == '__main__':
    unittest.main()
