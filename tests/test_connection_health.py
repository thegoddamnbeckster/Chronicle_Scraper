# -*- coding: utf-8 -*-
"""connection_health: the warning the add-ons raise when they are not really talking to Chronicle."""
import io
import unittest
import urllib.error

import tests.kodi_stubs  # noqa: F401

from lib import connection_health as ch
from lib import http_core


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(m)


class _Store:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, '')

    def set(self, key, value):
        self.values[key] = value


class TestConnectionHealth(unittest.TestCase):
    def setUp(self):
        self.shown = []
        self.now = [1000.0]
        self.store = _Store()
        self.health = ch.ConnectionHealth(
            'script.x', 'Chronicle X', _Log(), base_url_getter=lambda: 'http://c:8888',
            notify=lambda heading, text: self.shown.append((heading, text)), store=self.store, clock=lambda: self.now[0])

    def test_a_configured_add_on_reports_nothing(self):
        self.assertTrue(self.health.not_configured('http://c', 'key'))
        self.assertEqual(self.shown, [])

    def test_a_missing_key_or_address_is_reported(self):
        self.assertFalse(self.health.not_configured('http://c', ''))
        self.assertFalse(self.health.not_configured('', 'key'))
        self.assertEqual(len(self.shown), 1)             # the second call is the same problem, still quiet
        self.assertIn('Edit Connection', self.shown[0][1])
        self.assertEqual(self.health.current_problem(), ch.NOT_CONFIGURED)

    def test_a_rejected_key_is_reported_once_then_again_after_the_quiet_period(self):
        self.health.observe('http', 401)
        self.health.observe('http', 403)
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.health.current_problem(), ch.UNAUTHORIZED)
        self.now[0] += ch.RENOTIFY_AFTER_SECONDS + 1
        self.health.observe('http', 401)
        self.assertEqual(len(self.shown), 2)

    def test_other_http_errors_are_not_a_connection_problem(self):
        self.health.observe('http', 404)
        self.health.observe('http', 500)
        self.assertEqual(self.shown, [])

    def test_unreachable_needs_two_in_a_row(self):
        self.health.observe('unreachable', OSError('x'))
        self.assertEqual(self.shown, [])
        self.health.observe('ok')
        self.health.observe('unreachable', OSError('x'))
        self.assertEqual(self.shown, [])                 # the success in between reset the count
        self.health.observe('unreachable', OSError('x'))
        self.assertEqual(len(self.shown), 1)
        self.assertIn('http://c:8888', self.shown[0][1])

    def test_success_clears_the_current_problem_without_repeating_the_warning(self):
        self.health.observe('http', 401)
        self.health.observe('ok')                        # e.g. an unauthenticated health check
        self.assertEqual(self.health.current_problem(), '')
        self.health.observe('http', 401)
        self.assertEqual(len(self.shown), 1)

    def test_a_failing_notification_does_not_break_the_caller(self):
        def boom(heading, text):
            raise RuntimeError('no window')
        health = ch.ConnectionHealth('script.x', 'X', _Log(), notify=boom, store=_Store())
        health.observe('http', 401)                      # must not raise


class TestObserverWiring(unittest.TestCase):
    def tearDown(self):
        http_core.set_observer(None)

    def test_call_with_timeout_reports_each_outcome(self):
        seen = []
        http_core.set_observer(lambda kind, detail=None: seen.append((kind, detail)))
        http_core.call_with_timeout(lambda: 1, 1)

        def unauthorized():
            raise urllib.error.HTTPError('http://x', 401, 'Unauthorized', {}, io.BytesIO(b''))
        with self.assertRaises(urllib.error.HTTPError):
            http_core.call_with_timeout(unauthorized, 1)

        def down():
            raise urllib.error.URLError('refused')
        with self.assertRaises(urllib.error.URLError):
            http_core.call_with_timeout(down, 1)
        self.assertEqual([k for k, _ in seen], ['ok', 'http', 'unreachable'])
        self.assertEqual(seen[1][1], 401)

    def test_a_broken_observer_changes_nothing(self):
        def broken(kind, detail=None):
            raise RuntimeError('boom')
        http_core.set_observer(broken)
        self.assertEqual(http_core.call_with_timeout(lambda: 7, 1), 7)


if __name__ == '__main__':
    unittest.main()
