# -*- coding: utf-8 -*-
"""http_core: the watchdog, the retry policy, URL normalisation and the sibling-URL lookup every Chronicle Kodi add-on shares."""
import io
import threading
import unittest
import urllib.error
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import http_core


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(('info', m))

    def warning(self, m):
        self.lines.append(('warning', m))


def _http_error(code):
    return urllib.error.HTTPError('http://x', code, 'msg', {}, io.BytesIO(b''))


class TestCallWithTimeout(unittest.TestCase):
    def test_returns_the_value(self):
        self.assertEqual(http_core.call_with_timeout(lambda: 42, 1), 42)

    def test_reraises_the_callers_exception_type(self):
        def boom():
            raise ValueError('nope')
        with self.assertRaises(ValueError):
            http_core.call_with_timeout(boom, 1)

    def test_a_hung_call_raises_timeout_after_the_grace(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(http_core, 'WATCHDOG_GRACE_SECONDS', 0):
            with self.assertRaises(TimeoutError):
                http_core.call_with_timeout(lambda: release.wait(30), 0.05)

    def test_system_exit_in_the_worker_surfaces_to_the_caller(self):
        def quit_():
            raise SystemExit(3)
        with self.assertRaises(SystemExit):
            http_core.call_with_timeout(quit_, 1)


class TestCallWithRetries(unittest.TestCase):
    def setUp(self):
        self.log = _Log()
        self.sleeps = []

    def _run(self, fn, backoff=(1, 3)):
        return http_core.call_with_retries(fn, 1, 'label', self.log, sleep=self.sleeps.append, backoff=backoff)

    def test_success_first_time_does_not_sleep(self):
        self.assertEqual(self._run(lambda: 'ok'), 'ok')
        self.assertEqual(self.sleeps, [])

    def test_connection_failures_are_retried_with_backoff_then_succeed(self):
        outcomes = [urllib.error.URLError('down'), TimeoutError('slow'), 'ok']

        def fn():
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        self.assertEqual(self._run(fn), 'ok')
        self.assertEqual(self.sleeps, [1, 3])

    def test_gives_up_after_the_last_attempt_with_the_original_error(self):
        attempts = []

        def fn():
            attempts.append(1)
            raise ConnectionError('refused')
        with self.assertRaises(ConnectionError):
            self._run(fn)
        self.assertEqual((len(attempts), self.sleeps), (3, [1, 3]))

    def test_http_errors_that_will_not_change_are_not_retried(self):
        attempts = []

        def fn():
            attempts.append(1)
            raise _http_error(401)
        with self.assertRaises(urllib.error.HTTPError):
            self._run(fn)
        self.assertEqual((len(attempts), self.sleeps), (1, []))

    def test_server_not_serving_statuses_are_retried(self):
        for code in http_core.RETRYABLE_HTTP_STATUSES:
            outcomes = [_http_error(code), 'ok']

            def fn():
                o = outcomes.pop(0)
                if isinstance(o, Exception):
                    raise o
                return o
            self.sleeps.clear()
            self.assertEqual(self._run(fn), 'ok', code)
            self.assertEqual(self.sleeps, [1], code)

    def test_a_persistently_unavailable_server_raises_the_http_error(self):
        def fn():
            raise _http_error(503)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._run(fn)
        self.assertEqual(ctx.exception.code, 503)

    def test_unrelated_exceptions_propagate_immediately(self):
        attempts = []

        def fn():
            attempts.append(1)
            raise KeyError('bug')
        with self.assertRaises(KeyError):
            self._run(fn)
        self.assertEqual(len(attempts), 1)


class TestNormalizeBaseUrl(unittest.TestCase):
    def test_accepts_and_cleans(self):
        for typed, expected in (
                ('http://chronicle:7979', 'http://chronicle:7979'),
                ('  https://chronicle.example.com/  ', 'https://chronicle.example.com'),
                ('chronicle:7979', 'http://chronicle:7979'),
                ('10.0.0.162:7979//', 'http://10.0.0.162:7979'),
                ('HTTP://Host', 'HTTP://Host')):
            self.assertEqual(http_core.normalize_base_url(typed), expected, typed)

    def test_rejects_non_http_schemes_and_junk(self):
        for typed in ('file:///etc/passwd', 'ftp://host', 'javascript:alert(1)', '', '   ', None, 'http://', 'http:///path'):
            self.assertEqual(http_core.normalize_base_url(typed), '', repr(typed))


class TestIsLoopback(unittest.TestCase):
    def test_loopback_addresses(self):
        for url in ('http://localhost:7979', 'localhost', 'http://127.0.0.1', 'http://127.5.5.5:1', 'http://[::1]:7979', 'LOCALHOST'):
            self.assertTrue(http_core.is_loopback(url), url)

    def test_other_addresses(self):
        for url in ('http://10.0.0.162', 'chronicle.internal', 'http://192.168.1.2:7979', '', 'http://127example.com'):
            self.assertFalse(http_core.is_loopback(url), url)


class TestHeadersAndAgents(unittest.TestCase):
    def test_user_agent_per_addon_with_default(self):
        self.assertEqual(http_core.user_agent('script.chronicle.scraper.movie'), 'Kodi/Chronicle-Scraper/1.0')
        self.assertEqual(http_core.user_agent('script.chronicle.scraper.tv'), 'Kodi/Chronicle-Scraper/1.0')
        self.assertEqual(http_core.user_agent('service.chronicle.scrobbler'), 'Kodi/Chronicle-Scrobbler/1.0')
        self.assertEqual(http_core.user_agent('script.chronicle.rating'), 'Kodi/Chronicle-Rating/1.0')
        self.assertEqual(http_core.user_agent('something.else'), 'Kodi/Chronicle/1.0')

    def test_api_headers(self):
        self.assertEqual(http_core.api_headers('k', 'UA'), {'X-Api-Key': 'k', 'User-Agent': 'UA', 'Content-Type': 'application/json'})
        self.assertNotIn('Content-Type', http_core.api_headers('k', 'UA', json_body=False))


class TestFindSharedUrl(unittest.TestCase):
    def _factory(self, settings):
        def factory(addon_id):
            if addon_id not in settings:
                raise RuntimeError('not installed')
            addon = mock.MagicMock()
            addon.getSetting.return_value = settings[addon_id]
            return addon
        return factory

    def test_first_non_blank_sibling_wins_and_skips_self(self):
        settings = {'script.chronicle.scraper.movie': 'http://mine', 'script.chronicle.scraper.tv': '  ',
                    'service.chronicle.scrobbler': ' http://theirs '}
        found = http_core.find_shared_chronicle_url('script.chronicle.scraper.movie', _Log(), self._factory(settings))
        self.assertEqual(found, 'http://theirs')

    def test_none_when_nothing_is_set_or_installed(self):
        self.assertIsNone(http_core.find_shared_chronicle_url('x', _Log(), self._factory({})))
        self.assertIsNone(http_core.find_shared_chronicle_url('x', _Log(), self._factory({'service.chronicle.scrobbler': ''})))

    def test_rating_addon_is_not_a_url_source(self):
        self.assertNotIn('script.chronicle.rating', http_core.SIBLING_ADDON_IDS)


if __name__ == '__main__':
    unittest.main()
