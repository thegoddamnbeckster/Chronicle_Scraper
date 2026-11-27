# -*- coding: utf-8 -*-
"""ConnectionUi: the shared Edit Connection / Change URL / Test Connection / status flows."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import connection_ui
from lib.connection_ui import ConnectionUi, parse_script_args


class _Addon:
    """A settings-backed fake add-on."""
    def __init__(self, **settings):
        self.settings = dict(settings)
        self.writes = []
        self.opened = 0

    def getSetting(self, key):
        return self.settings.get(key, '')

    def setSetting(self, key, value):
        self.writes.append((key, value))
        self.settings[key] = value

    def openSettings(self):
        self.opened += 1

    def getAddonInfo(self, key):
        return 'script.chronicle.scraper.movie'

    def getLocalizedString(self, string_id):
        return {32120: 'Connected as {0}'}.get(string_id, 's{0}'.format(string_id))


class _Disk:
    """Kodi with the Settings window closed: a new Addon() loads settings.xml, and each setSetting writes that object's WHOLE copy back."""
    def __init__(self, **settings):
        self.settings = dict(settings)
        self.shown = []   # what each opened Settings window was showing

    def addon(self):
        return _SnapshotAddon(self)


class _SnapshotAddon(_Addon):
    def __init__(self, disk):
        super().__init__(**disk.settings)
        self.disk = disk

    def setSetting(self, key, value):
        super().setSetting(key, value)
        self.disk.settings = dict(self.settings)

    def openSettings(self):
        self.disk.shown.append(dict(self.settings))


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Base(unittest.TestCase):
    def setUp(self):
        self.addon = _Addon()
        self.client = mock.MagicMock()
        self.device_auth = mock.MagicMock()
        self.device_auth.run.return_value = True
        self.auth_factory = mock.Mock(return_value=self.device_auth)
        self.connected = mock.Mock()
        self.dialog = mock.MagicMock()
        p = mock.patch.object(connection_ui.xbmcgui, 'Dialog', return_value=self.dialog)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(connection_ui, 'find_shared_chronicle_url', return_value=None)
        self.find_shared = q.start()
        self.addCleanup(q.stop)
        self.ui = self._ui()

    def _ui(self, on_connected=True):
        return ConnectionUi(self.addon, _Log(), lambda: self.client, self.auth_factory, self.connected if on_connected else None,
                            addon_factory=lambda: self.addon)


class TestStatus(_Base):
    def test_not_connected_without_url_or_key(self):
        for settings in ({}, {'chronicle_url': 'http://x'}, {'api_key': 'k'}):
            self.addon.settings = dict(settings)
            self.addon.writes.clear()
            self.ui.refresh_auth_status()
            self.assertEqual(self.addon.writes, [('auth_status', 's32082')], settings)
        self.client.get_current_user.assert_not_called()

    def test_connected_as_the_display_name(self):
        self.addon.settings = {'chronicle_url': 'http://x', 'api_key': 'k'}
        self.client.get_current_user.return_value = {'displayName': 'Ada', 'username': 'ada'}
        self.ui.refresh_auth_status()
        self.assertEqual(self.addon.settings['auth_status'], 'Connected as Ada')
        self.assertEqual(self.addon.settings['connected_display_name'], 'Ada')

    def test_falls_back_to_username_then_last_known_name_then_bare_connected(self):
        self.addon.settings = {'chronicle_url': 'http://x', 'api_key': 'k'}
        self.client.get_current_user.return_value = {'username': 'ada'}
        self.ui.refresh_auth_status()
        self.assertEqual(self.addon.settings['auth_status'], 'Connected as ada')
        self.client.get_current_user.return_value = None   # a network blip keeps the last name
        self.ui.refresh_auth_status()
        self.assertEqual(self.addon.settings['auth_status'], 'Connected as ada')
        self.addon.settings['connected_display_name'] = ''
        self.ui.refresh_auth_status()
        self.assertEqual(self.addon.settings['auth_status'], 's32081')


class TestTestConnection(_Base):
    def test_success_and_failure_dialogs(self):
        self.client.test_connection.return_value = (True, '')
        self.ui.test_connection()
        self.assertEqual(self.dialog.ok.call_args[0], ('s32012', 's32020'))
        self.client.test_connection.return_value = (False, 'HTTP 401')
        self.ui.test_connection()
        self.assertEqual(self.dialog.ok.call_args[0], ('s32012', 's32021\nHTTP 401'))


class TestConnect(_Base):
    def test_prompts_for_url_normalises_saves_and_runs_device_auth(self):
        self.dialog.input.return_value = ' chronicle:7979/ '
        self.ui.connect()
        self.assertEqual(self.addon.settings['chronicle_url'], 'http://chronicle:7979')
        self.auth_factory.assert_called_once_with('http://chronicle:7979')   # handed over, not re-read from settings
        self.connected.assert_called_once()

    def test_prompt_is_prefilled_from_a_sibling_addon(self):
        self.find_shared.return_value = 'http://sibling:7979'
        self.dialog.input.return_value = ''
        self.ui.connect()
        self.assertEqual(self.dialog.input.call_args[1]['defaultt'], 'http://sibling:7979')
        self.auth_factory.assert_not_called()

    def test_cancelled_prompt_does_nothing(self):
        self.dialog.input.return_value = None
        self.ui.connect()
        self.assertEqual(self.addon.writes, [('auth_status', 's32082')])   # only the status line is refreshed
        self.auth_factory.assert_not_called()
        self.assertEqual(self.addon.opened, 1)

    def test_invalid_address_is_refused_and_not_saved(self):
        self.dialog.input.return_value = 'file:///etc/passwd'
        self.ui.connect()
        self.assertNotIn('chronicle_url', self.addon.settings)
        self.dialog.ok.assert_called_once()
        self.auth_factory.assert_not_called()

    def test_already_configured_goes_straight_to_the_qr_flow(self):
        self.addon.settings['chronicle_url'] = 'http://x'
        self.ui.connect()
        self.dialog.input.assert_not_called()
        self.auth_factory.assert_called_once_with('http://x')

    def test_loopback_url_declined_clears_it_and_stops(self):
        self.dialog.input.return_value = 'localhost:7979'
        self.dialog.yesno.return_value = False
        self.ui.connect()
        self.assertEqual(self.addon.settings['chronicle_url'], '')
        self.auth_factory.assert_not_called()

    def test_loopback_url_kept_continues(self):
        self.dialog.input.return_value = 'localhost:7979'
        self.dialog.yesno.return_value = True
        self.ui.connect()
        self.auth_factory.assert_called_once_with('http://localhost:7979')

    def test_failed_flow_refreshes_status_and_skips_the_hook(self):
        self.addon.settings['chronicle_url'] = 'http://x'
        self.device_auth.run.return_value = False
        self.ui.connect()
        self.connected.assert_not_called()
        self.assertIn(('auth_status', 's32082'), self.addon.writes)

    def test_crashing_flow_is_reported_not_raised(self):
        self.addon.settings['chronicle_url'] = 'http://x'
        self.device_auth.run.side_effect = RuntimeError('qr broke')
        self.ui.connect()
        self.dialog.ok.assert_called_once()
        self.connected.assert_not_called()

    def test_no_hook_is_fine(self):
        self.addon.settings['chronicle_url'] = 'http://x'
        self._ui(on_connected=False).connect()


class TestChangeUrl(_Base):
    def test_confirmed_change_clears_url_and_key_then_reconnects(self):
        self.addon.settings.update(chronicle_url='http://old', api_key='k')
        self.dialog.yesno.return_value = True
        self.dialog.input.return_value = 'http://new:1'
        self.ui.change_url()
        self.assertEqual(self.addon.settings['chronicle_url'], 'http://new:1')
        self.assertEqual(self.addon.settings['api_key'], '')

    def test_a_stale_read_after_clearing_still_asks_and_saves_the_url_with_the_key(self):
        # Seen live on the Shield: right after setSetting('chronicle_url', '') Kodi still answered the old address, so the flow
        # reconnected to it without asking and left the URL blank on disk next to a fresh key ("not connected").
        self.addon.settings.update(chronicle_url='http://old:8888', api_key='k')
        stale = self.addon.getSetting
        self.addon.getSetting = lambda key: 'http://old:8888' if key == 'chronicle_url' else stale(key)
        self.dialog.yesno.return_value = True
        self.dialog.input.return_value = 'http://old:8888'
        self.ui.change_url()
        self.assertEqual(self.dialog.input.call_args[1]['defaultt'], 'http://old:8888')   # asked, starting from the old address
        self.auth_factory.assert_called_once_with('http://old:8888')
        self.assertEqual(self.addon.settings['chronicle_url'], 'http://old:8888')        # saved again after the key arrived
        self.assertEqual([w for w in self.addon.writes if w[0] == 'chronicle_url'][-1], ('chronicle_url', 'http://old:8888'))

    def test_declined_change_keeps_everything(self):
        self.addon.settings.update(chronicle_url='http://old', api_key='k')
        self.dialog.yesno.return_value = False
        self.ui.change_url()
        self.assertEqual((self.addon.settings['chronicle_url'], self.addon.settings['api_key']), ('http://old', 'k'))
        self.auth_factory.assert_not_called()

    def test_with_no_saved_url_it_just_connects(self):
        self.dialog.input.return_value = 'http://new:1'
        self.ui.change_url()
        self.dialog.yesno.assert_not_called()
        self.auth_factory.assert_called_once_with('http://new:1')


class TestSettingsOnDisk(unittest.TestCase):
    """Edit Connection and Change URL run with the Settings window closed, so what they write must reach disk and stay there."""
    def setUp(self):
        self.disk = _Disk(chronicle_url='http://old', api_key='OLD')
        self.dialog = mock.MagicMock()
        for target, new in (('Dialog', mock.Mock(return_value=self.dialog)),):
            p = mock.patch.object(connection_ui.xbmcgui, target, new)
            p.start()
            self.addCleanup(p.stop)
        q = mock.patch.object(connection_ui, 'find_shared_chronicle_url', return_value=None)
        q.start()
        self.addCleanup(q.stop)
        self.client = mock.MagicMock()
        self.client.get_current_user.return_value = {'displayName': 'Ada'}
        self.device_auth = mock.MagicMock()
        self.device_auth.run.side_effect = self._approve
        # made before anything runs, like default.py's module-level ADDON
        self.ui = ConnectionUi(self.disk.addon(), _Log(), lambda: self.client, lambda url: self.device_auth,
                               addon_factory=self.disk.addon)

    def _approve(self):
        self.disk.addon().setSetting('api_key', 'NEW')   # what DeviceAuthManager does
        return True

    def test_reconnect_leaves_the_new_key_on_disk_and_reopens_settings_with_it(self):
        self.ui.connect()
        self.assertEqual(self.disk.settings['api_key'], 'NEW')
        self.assertEqual(len(self.disk.shown), 1)
        self.assertEqual((self.disk.shown[0]['api_key'], self.disk.shown[0]['auth_status']), ('NEW', 'Connected as Ada'))

    def test_change_url_leaves_the_new_url_and_key_on_disk_and_opens_settings_once(self):
        self.dialog.yesno.return_value = True
        self.dialog.input.return_value = 'http://new:1'
        self.ui.change_url()
        self.assertEqual((self.disk.settings['chronicle_url'], self.disk.settings['api_key']), ('http://new:1', 'NEW'))
        self.assertEqual(len(self.disk.shown), 1)

    def test_a_cancelled_flow_keeps_the_old_key_and_reopens_settings(self):
        self.device_auth.run.side_effect = None
        self.device_auth.run.return_value = False
        self.ui.connect()
        self.assertEqual(self.disk.settings['api_key'], 'OLD')
        self.assertEqual(len(self.disk.shown), 1)

    def test_declining_change_url_reopens_settings(self):
        self.dialog.yesno.return_value = False
        self.ui.change_url()
        self.assertEqual((self.disk.settings['chronicle_url'], self.disk.settings['api_key']), ('http://old', 'OLD'))
        self.assertEqual(len(self.disk.shown), 1)


class TestParseScriptArgs(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_script_args(['default.py', 'action=auth', 'x=a=b', 'bare']), {'action': 'auth', 'x': 'a=b'})
        self.assertEqual(parse_script_args(['default.py']), {})


if __name__ == '__main__':
    unittest.main()
