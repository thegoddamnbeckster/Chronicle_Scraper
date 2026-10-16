# -*- coding: utf-8 -*-
"""DeviceAuthManager: the QR pairing flow -- initiate, poll, save the key, and every way it can end."""
import io
import json
import threading
import unittest
import urllib.error
from unittest import mock

import tests.kodi_stubs as stubs

from lib import device_auth
from lib.device_auth import DeviceAuthManager

_GOOD = {'code': 'abcdef0123456789zz', 'displayCode': 'ABCD-1234', 'qrUrl': 'http://c/qr.png',
         'verificationUrl': 'http://c/verify', 'expiresInSeconds': 600}


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.addon = mock.MagicMock()
        self.addon.getSetting.return_value = ''
        self.addon.getLocalizedString.side_effect = lambda i: '#{0}'.format(i)
        for target, new in (('ADDON', self.addon), ('get_device_name', mock.Mock(return_value='Shield'))):
            p = mock.patch.object(device_auth, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.dialog = mock.MagicMock()
        p = mock.patch.object(device_auth.xbmcgui, 'Dialog', return_value=self.dialog)
        p.start()
        self.addCleanup(p.stop)

    def _urlopen(self, *responses):
        queue = list(responses)
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            out = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(out, Exception):
                raise out
            return out
        p = mock.patch.object(device_auth.urllib.request, 'urlopen', side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return calls


class TestInitiate(_Base):
    def test_posts_the_device_name_and_returns_the_data(self):
        calls = self._urlopen(_Resp({'data': _GOOD}))
        data = DeviceAuthManager('chronicle:7979/')._initiate()
        self.assertEqual(data['displayCode'], 'ABCD-1234')
        self.assertEqual(calls[0].full_url, 'http://chronicle:7979/api/v1/auth/device')
        self.assertEqual(json.loads(calls[0].data), {'deviceName': 'Shield'})

    def test_missing_url_and_invalid_url_have_different_messages(self):
        m = DeviceAuthManager('')
        self.assertIsNone(m._initiate())
        self.assertEqual(m._last_error, '#32085')
        m = DeviceAuthManager('file:///etc/passwd')
        self.assertIsNone(m._initiate())
        self.assertIn('not a valid http', m._last_error)

    def test_falls_back_to_the_saved_url_when_none_was_passed(self):
        self.addon.getSetting.side_effect = lambda k: 'http://saved:1' if k == 'chronicle_url' else ''
        calls = self._urlopen(_Resp({'data': _GOOD}))
        DeviceAuthManager()._initiate()
        self.assertTrue(calls[0].full_url.startswith('http://saved:1/'))

    def test_failure_reasons_are_specific(self):
        cases = [
            (urllib.error.HTTPError('u', 500, 'Server Error', {}, io.BytesIO(b'boom')), 'HTTP 500 Server Error'),
            (urllib.error.URLError('Name or service not known'), 'Name or service not known'),
            (TimeoutError('no response'), 'no response'),
            (ValueError('weird'), 'weird'),
        ]
        for exc, expected in cases:
            self._urlopen(exc)
            m = DeviceAuthManager('http://c')
            self.assertIsNone(m._initiate(), exc)
            self.assertIn(expected, m._last_error)

    def test_malformed_response_is_reported_not_raised(self):
        for body in ({'data': None}, {'data': {'code': 'x'}}, {'data': 'string'}):
            self._urlopen(_Resp(body))
            m = DeviceAuthManager('http://c')
            self.assertIsNone(m._initiate())
            self.assertEqual(m._last_error, '#32113')


class TestDownloadQr(_Base):
    def test_saved_through_the_vfs_under_a_per_code_name(self):
        self._urlopen(_Resp(b'PNGDATA'))
        path = DeviceAuthManager('http://c')._download_qr('http://c/qr.png', 'abcdef0123456789zz')
        self.assertEqual(path, 'special://temp/chronicle_qr_abcdef0123456789.png')
        self.assertEqual(stubs._FAKE_FILES[path], b'PNGDATA')

    def test_failure_gives_an_empty_path(self):
        self._urlopen(urllib.error.URLError('down'))
        self.assertEqual(DeviceAuthManager('http://c')._download_qr('http://c/qr.png', 'abc'), '')


class TestPollLoop(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(device_auth, '_POLL_INTERVAL', 0)
        p.start()
        self.addCleanup(p.stop)

    def _poll(self, *bodies, max_polls=5):
        calls = self._urlopen(*[b if isinstance(b, Exception) else _Resp(b) for b in bodies])
        holder, error, stop = [None], [None], threading.Event()
        real_wait = stop.wait
        count = []

        def wait(timeout=None):
            count.append(1)
            if len(count) > max_polls:
                stop.set()
            return real_wait(0)
        stop.wait = wait
        DeviceAuthManager('http://c')._poll_loop('code1', holder, stop, error)
        return holder[0], error[0], calls

    def test_approved_with_key_stores_it_and_stops(self):
        key, error, calls = self._poll({'data': {'status': 'pending'}}, {'data': {'status': 'approved', 'apiKey': 'KEY'}})
        self.assertEqual((key, error), ('KEY', None))
        self.assertEqual(calls[0].full_url, 'http://c/api/v1/auth/device/code1/poll')

    def test_denied_and_expired_stop_without_a_key(self):
        for status in ('denied', 'expired'):
            self.assertEqual(self._poll({'data': {'status': status}})[:2], (None, None), status)

    def test_approved_without_a_key_is_a_terminal_error(self):
        key, error, _ = self._poll({'data': {'status': 'approved', 'apiKey': None}})
        self.assertEqual((key, error), (None, '#32110'))

    def test_transient_errors_keep_polling(self):
        key, _, calls = self._poll(urllib.error.URLError('blip'), {'data': {'status': 'approved', 'apiKey': 'K'}})
        self.assertEqual(key, 'K')
        self.assertEqual(len(calls), 2)


class TestRun(_Base):
    def setUp(self):
        super().setUp()
        self.manager = DeviceAuthManager('http://c')
        self.shown = {}
        outer = self

        class FakeQr:
            def __init__(self, **kw):
                outer.shown.update(kw)

            def doModal(self):
                outer.shown['modal'] = True
        for target, new in (('QRDialog', FakeQr),):
            p = mock.patch.object(device_auth, target, new)
            p.start()
            self.addCleanup(p.stop)

    def _poll_result(self, key=None, error=None):
        def poll(code, holder, stop, poll_error):
            holder[0], poll_error[0] = key, error
        return mock.patch.object(self.manager, '_poll_loop', side_effect=poll)

    def test_initiation_failure_shows_the_reason_and_returns_false(self):
        with mock.patch.object(self.manager, '_initiate', return_value=None):
            self.manager._last_error = None
            self.assertFalse(self.manager.run())
        self.dialog.ok.assert_called_once()

    def test_approved_saves_the_key_and_status(self):
        with mock.patch.object(self.manager, '_initiate', return_value=dict(_GOOD)), \
                mock.patch.object(self.manager, '_download_qr', return_value='special://temp/q.png'), self._poll_result(key='KEY'):
            self.assertTrue(self.manager.run())
        self.addon.setSetting.assert_any_call('api_key', 'KEY')
        self.addon.setSetting.assert_any_call('auth_status', '#32081')
        self.assertEqual((self.shown['display_code'], self.shown['expires_in']), ('ABCD-1234', 600))

    def test_cancelled_or_expired_returns_false_without_saving(self):
        with mock.patch.object(self.manager, '_initiate', return_value=dict(_GOOD)), \
                mock.patch.object(self.manager, '_download_qr', return_value=''), self._poll_result():
            self.assertFalse(self.manager.run())
        self.addon.setSetting.assert_not_called()

    def test_terminal_poll_error_is_shown(self):
        with mock.patch.object(self.manager, '_initiate', return_value=dict(_GOOD)), \
                mock.patch.object(self.manager, '_download_qr', return_value=''), self._poll_result(error='already used'):
            self.assertFalse(self.manager.run())
        self.assertEqual(self.dialog.ok.call_args[0][1], 'already used')


if __name__ == '__main__':
    unittest.main()


class TestQrDialog(unittest.TestCase):
    def _dialog(self, key=None):
        from lib import qr_dialog
        self.qr = qr_dialog
        stop = threading.Event()
        d = qr_dialog.QRDialog('special://temp/q.png', 'ABCD-1234', 'http://c/verify', 600, stop, [key])
        d._status_label, d._countdown_label = mock.MagicMock(), mock.MagicMock()
        d.close = mock.MagicMock()
        return d, stop

    def test_back_and_escape_cancel(self):
        for action_id in (10, 92):
            d, stop = self._dialog()
            d.onAction(mock.Mock(getId=lambda a=action_id: a))
            self.assertTrue(stop.is_set())
            d.close.assert_called_once()

    def test_other_actions_do_nothing(self):
        d, stop = self._dialog()
        d.onAction(mock.Mock(getId=lambda: 7))
        self.assertFalse(stop.is_set())

    def test_cancel_button_cancels_only_for_its_own_id(self):
        d, stop = self._dialog()
        d._cancel_btn_id = 55
        d.onControl(mock.Mock(getId=lambda: 1))
        self.assertFalse(stop.is_set())
        d.onControl(mock.Mock(getId=lambda: 55))
        self.assertTrue(stop.is_set())

    def test_monitor_loop_reports_connected_or_cancelled_then_closes(self):
        for key, expected in (('KEY', '32066'), (None, '32067')):
            d, stop = self._dialog(key)
            stop.set()
            with mock.patch.object(self.qr.ADDON, 'getLocalizedString', side_effect=lambda i: str(i)):
                d._monitor_loop()
            d._status_label.setLabel.assert_called_with(expected)
            d.close.assert_called_once()
