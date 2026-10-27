# -*- coding: utf-8 -*-
"""The scraper add-ons' ChronicleClient: request shapes, the {success, data} envelope, retries and "never raises".

This file is identical in the movie and TV add-ons' test folders (their clients share most endpoints); methods one client lacks are skipped.
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import chronicle_client, http_core
from lib.chronicle_client import ChronicleClient


class _Resp:
    def __init__(self, body=None, status=200):
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(url='chronicle:7979/', key='k'):
    addon = mock.MagicMock()
    addon.getSetting.side_effect = lambda k: {'chronicle_url': url, 'api_key': key}.get(k, '')
    with mock.patch.object(chronicle_client, 'ADDON', addon):
        c = ChronicleClient()
    return c, addon


def _http_error(code):
    return urllib.error.HTTPError('http://c', code, 'reason', {}, io.BytesIO(b''))


class _Net(unittest.TestCase):
    def setUp(self):
        # no real sleeping between retries
        p = mock.patch.object(http_core, 'RETRY_BACKOFF_SECONDS', (0, 0))
        p.start()
        self.addCleanup(p.stop)

    def _urlopen(self, *outcomes):
        """Each call returns/raises the next outcome (the last repeats)."""
        queue, calls = list(outcomes), []

        def fake(req, timeout=None):
            calls.append((req, timeout))
            out = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(out, Exception):
                raise out
            return out
        p = mock.patch.object(chronicle_client.urllib.request, 'urlopen', side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def _need(self, c, name):
        if not hasattr(c, name):
            self.skipTest('{0} not on this add-on\'s client'.format(name))


class TestConnectionSettings(unittest.TestCase):
    def test_url_normalised_key_read_and_refreshable(self):
        c, addon = _client('chronicle:7979/', 'k1')
        self.assertEqual((c._base_url, c._api_key), ('http://chronicle:7979', 'k1'))
        addon.getSetting.side_effect = lambda k: {'chronicle_url': 'http://b', 'api_key': 'k2'}.get(k, '')
        with mock.patch.object(chronicle_client, 'ADDON', addon):
            c.refresh_settings()
        self.assertEqual((c._base_url, c._api_key), ('http://b', 'k2'))

    def test_non_http_url_is_dropped(self):
        self.assertEqual(_client('file:///etc/passwd')[0]._base_url, '')

    def test_unconfigured_client_never_touches_the_network(self):
        c, _ = _client('', '')
        with mock.patch.object(chronicle_client.urllib.request, 'urlopen') as urlopen:
            self.assertIsNone(c.search_movie('Heat', 1995)) if hasattr(c, 'search_movie') else None
            self.assertIsNone(c.search_show('Show'))
            self.assertIsNone(c.get_show_details(1))
            self.assertIsNone(c.get_current_user())
            self.assertIsNone(c.get_changed_collections())
            c.report_kodi_id(1, 'movie', 2)
            c.report_resolved_file(1, 'a.mkv')
            c.report_scan_active()
            c.push_watched(1, None)
            c.push_resume(1, 5.0, None)
            self.assertEqual(c.test_connection()[0], False)
        urlopen.assert_not_called()


class TestGetEndpoints(_Net):
    def test_request_carries_key_agent_and_timezone(self):
        c, _ = _client()
        calls = self._urlopen(_Resp({'data': {'id': 4}}))
        self.assertEqual(c.get_show_details(4), {'id': 4})
        req, timeout = calls[0]
        self.assertEqual(req.full_url, 'http://chronicle:7979/api/v1/scraper/tv/details?id=4')
        self.assertEqual(req.get_header('X-api-key'), 'k')
        self.assertIn('Kodi/Chronicle', req.get_header('User-agent'))
        self.assertTrue(any(h.lower() == chronicle_client.timeutil.TIMEZONE_HEADER.lower() for h in req.headers))
        self.assertEqual(timeout, 20)

    def test_search_urls_are_encoded_and_use_the_long_timeout(self):
        c, _ = _client()
        self._need(c, 'search_movie')
        calls = self._urlopen(_Resp({'data': {'id': 1}}))
        c.search_movie('Alien: Derelict & Co', 1979, 'Alien - Derelict (1979).mkv')
        url = calls[0][0].full_url
        self.assertIn('title=Alien%3A%20Derelict%20%26%20Co', url)
        self.assertIn('&year=1979', url)
        self.assertIn('&fileName=Alien%20-%20Derelict%20%281979%29.mkv', url)
        self.assertEqual(calls[0][1], 90)

    def test_search_show_and_resolve_by_external_id(self):
        c, _ = _client()
        calls = self._urlopen(_Resp({'data': {'id': 1}}))
        c.search_show('Rick & Morty', 2013)
        c.resolve_show_by_external_id('tvdb', '275274')
        self.assertIn('/api/v1/scraper/tv/search?title=Rick%20%26%20Morty&year=2013', calls[0][0].full_url)
        self.assertIn('resolve-by-external-id?source=tvdb&externalId=275274', calls[1][0].full_url)

    def test_by_file_lookups_send_optional_guards_only_when_known(self):
        c, _ = _client()
        calls = self._urlopen(_Resp({'data': {}}))
        c.get_episode_details_by_file('S01E02.mkv', 1, 2)
        c.get_episode_details_by_file('S01E02.mkv')
        c.get_episode_details_by_file('S00E00.mkv', 0, 0)
        self.assertTrue(calls[0][0].full_url.endswith('fileName=S01E02.mkv&season=1&episode=2'))
        self.assertTrue(calls[1][0].full_url.endswith('fileName=S01E02.mkv'))
        self.assertTrue(calls[2][0].full_url.endswith('&season=0&episode=0'))   # specials: 0 is a real value, not "unknown"
        if hasattr(c, 'get_movie_details_by_file'):
            c.get_movie_details_by_file('Heat.mkv', 1995)
            c.get_movie_details_by_file('Heat.mkv')
            self.assertTrue(calls[3][0].full_url.endswith('fileName=Heat.mkv&year=1995'))
            self.assertTrue(calls[4][0].full_url.endswith('fileName=Heat.mkv'))

    def test_failures_give_none_never_raise(self):
        c, _ = _client()
        for exc in (_http_error(401), urllib.error.URLError('down'), TimeoutError('slow'), ValueError('bad json')):
            self._urlopen(exc)
            self.assertIsNone(c.get_episode_list(3), exc)

    def test_transient_failures_are_retried_and_a_401_is_not(self):
        c, _ = _client()
        calls = self._urlopen(urllib.error.URLError('blip'), _http_error(503), _Resp({'data': [1]}))
        self.assertEqual(c.get_episode_list(3), [1])
        self.assertEqual(len(calls), 3)
        calls = self._urlopen(_http_error(401))
        self.assertIsNone(c.get_episode_list(3))
        self.assertEqual(len(calls), 1)

    def test_envelope_without_data_is_none(self):
        c, _ = _client()
        self._urlopen(_Resp({'success': True}))
        self.assertIsNone(c.get_episode_details(9))

    def test_get_current_user_is_short(self):
        c, _ = _client()
        calls = self._urlopen(_Resp({'data': {'username': 'u'}}))
        self.assertEqual(c.get_current_user(), {'username': 'u'})
        self.assertEqual(calls[0][1], 5)


class TestSignals(_Net):
    def test_refresh_signal_returns_items_or_empty(self):
        c, _ = _client()
        self._need(c, 'get_refresh_signal')
        calls = self._urlopen(_Resp({'data': {'items': [{'id': 1}]}}))
        self.assertEqual(c.get_refresh_signal(['episode', 'tvshow']), [{'id': 1}])
        self.assertIn('kinds=episode%2Ctvshow', calls[0][0].full_url)
        self._urlopen(_Resp({'data': None}))
        self.assertEqual(c.get_refresh_signal(['movie']), [])
        self._urlopen(urllib.error.URLError('down'))
        self.assertEqual(c.get_refresh_signal(['movie']), [])

    def test_changed_collections_returns_the_listing_or_none(self):
        c, _ = _client()
        calls = self._urlopen(_Resp({'data': {'asOf': '2026-10-24T01:00:00Z', 'collections': [{'id': 1}]}}))
        self.assertEqual(c.get_changed_collections(),
                         {'asOf': '2026-10-24T01:00:00Z', 'collections': [{'id': 1}]})
        self.assertEqual(calls[0][0].full_url, 'http://chronicle:7979/api/v1/scraper/movies/collections/changed')
        # An empty list is an answer; a failure (or anything that is not a listing) is not.
        self._urlopen(_Resp({'data': {'asOf': 'x', 'collections': []}}))
        self.assertEqual(c.get_changed_collections()['collections'], [])
        self._urlopen(urllib.error.URLError('down'))
        self.assertIsNone(c.get_changed_collections())
        self._urlopen(_Resp({'data': None}))
        self.assertIsNone(c.get_changed_collections())

    def test_changed_collections_sends_this_devices_own_time_as_since(self):
        c, _ = _client()
        calls = self._urlopen(_Resp({'data': {'asOf': 'x', 'collections': []}}))
        c.get_changed_collections(since='2026-10-24T01:00:00.123Z')
        self.assertTrue(calls[0][0].full_url.endswith(
            '/movies/collections/changed?since=2026-10-24T01%3A00%3A00.123Z'))


class TestBestEffortPosts(_Net):
    def _cases(self, c):
        cases = [
            ('report_resolved_file', (7, 'Heat.mkv'), '/api/v1/scraper/movies/7/resolved-file', {'fileName': 'Heat.mkv'}),
            ('report_kodi_id', (7, 'movie', 99), '/api/v1/scraper/report-kodi-id', {'mediaItemId': 7, 'kind': 'movie', 'kodiId': 99}),
            ('report_scan_active', (), '/api/v1/scraper/scan-active', {}),
            ('push_watched', (7, '2026-10-01T00:00:00Z'), '/api/v1/scrobble',
             {'mediaItemId': 7, 'progressPercent': 100, 'markedAsWatched': True, 'timestamp': '2026-10-01T00:00:00Z'}),
            ('push_resume', (7, 42.5, '2026-10-01T00:00:00Z'), '/api/v1/scrobble',
             {'mediaItemId': 7, 'progressPercent': 42.5, 'timestamp': '2026-10-01T00:00:00Z'}),
        ]
        if hasattr(c, 'register_device'):
            cases.append(('register_device', ('Shield', 'Chronicle Scraper (Movies)', 'script.chronicle.scraper.movie', '3.25.0', '21.1'),
                          '/api/v1/scraper/devices/kodi/register',
                          {'name': 'Shield', 'addonName': 'Chronicle Scraper (Movies)', 'addonId': 'script.chronicle.scraper.movie',
                           'addonVersion': '3.25.0', 'kodiVersion': '21.1'}))
        return [case for case in cases if hasattr(c, case[0])]

    def test_each_posts_the_documented_body(self):
        c, _ = _client()
        for method, args, path, expected in self._cases(c):
            calls = self._urlopen(_Resp(b'', status=200))
            getattr(c, method)(*args)
            req = calls[0][0]
            self.assertEqual((req.full_url, req.get_method()), ('http://chronicle:7979' + path, 'POST'), method)
            sent = json.loads(req.data)
            for key, value in expected.items():
                self.assertEqual(sent[key], value, '{0}.{1}'.format(method, key))

    def test_each_swallows_every_failure(self):
        c, _ = _client()
        for method, args, _path, _expected in self._cases(c):
            for outcome in (_http_error(500), urllib.error.URLError('down'), TimeoutError(), ValueError(), _Resp(b'', status=202)):
                self._urlopen(outcome)
                getattr(c, method)(*args)   # must not raise

    def test_registration_sends_who_is_asking_and_nothing_about_the_device_address(self):
        c, _ = _client()
        calls = self._urlopen(_Resp(b'', status=200))
        c.register_device('Shield', 'Chronicle Scraper (Movies)', 'script.chronicle.scraper.movie', '3.25.0', '21.1')
        sent = json.loads(calls[0][0].data)
        self.assertEqual(sorted(sent), ['addonId', 'addonName', 'addonVersion', 'kodiVersion', 'name'])

    def test_push_without_timestamp_omits_it(self):
        c, _ = _client()
        calls = self._urlopen(_Resp(b'', status=200))
        c.push_watched(7, None)
        c.push_resume(7, 5.0, None)
        for req, _ in calls:
            self.assertNotIn('timestamp', json.loads(req.data))

    def test_reset_watch_progress_reports_success(self):
        c, _ = _client()
        self._need(c, 'reset_watch_progress')
        calls = self._urlopen(_Resp(b'', status=200))
        self.assertTrue(c.reset_watch_progress(7))
        self.assertTrue(calls[0][0].full_url.endswith('/api/v1/library/by-media/7/reset-watch-progress'))
        for outcome in (_http_error(500), urllib.error.URLError('down'), ValueError(), _Resp(b'', status=202)):
            self._urlopen(outcome)
            self.assertFalse(c.reset_watch_progress(7))


class TestConnectionTest(_Net):
    def test_results(self):
        c, _ = _client()
        self._urlopen(_Resp(b'', status=200))
        self.assertEqual(c.test_connection(), (True, ''))
        self._urlopen(_Resp(b'', status=204))
        self.assertEqual(c.test_connection(), (False, 'Unexpected HTTP 204'))
        self._urlopen(_http_error(401))
        self.assertEqual(c.test_connection(), (False, 'HTTP 401: reason'))
        self._urlopen(urllib.error.URLError('refused'))
        ok, message = c.test_connection()
        self.assertFalse(ok)
        self.assertIn('refused', message)
        self.assertEqual(_client('', 'k')[0].test_connection(), (False, 'Chronicle URL is not configured.'))


class TestSharedUrl(unittest.TestCase):
    def test_uses_this_addons_id(self):
        addon = mock.MagicMock()
        addon.getAddonInfo.return_value = 'script.chronicle.scraper.movie'
        with mock.patch.object(chronicle_client, 'ADDON', addon), \
                mock.patch.object(chronicle_client, '_find_shared_url', return_value='http://x') as find:
            self.assertEqual(chronicle_client.find_shared_chronicle_url(), 'http://x')
        self.assertEqual(find.call_args[0][0], 'script.chronicle.scraper.movie')


if __name__ == '__main__':
    unittest.main()
