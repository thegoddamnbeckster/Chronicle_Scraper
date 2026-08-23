# -*- coding: utf-8 -*-
"""Tests for lib/device_auth.py's chronicle_url settle-retry (BUG-041).

"Connect to Chronicle" is a RunScript action fired from the same Settings screen
as the chronicle_url text field -- Kodi doesn't guarantee that field's just-typed
edit is committed to the addon's settings store before RunScript launches this as
a separate process, so a first read can come back empty even though the user just
typed a real URL. _read_chronicle_url() retries briefly to absorb that race; run()
must distinguish "genuinely not configured" (shows a specific message, never
contacts the server) from "configured but unreachable" (the existing message).
"""

import sys


def _import_device_auth():
    sys.path.insert(0, '.')
    import lib.device_auth as device_auth
    return device_auth


def test_read_chronicle_url_returns_configured_value_immediately(kodi):
    device_auth = _import_device_auth()
    device_auth.ADDON._settings['chronicle_url'] = 'http://chronicle.local:7979/'

    sleep_calls = []
    device_auth.time.sleep = lambda s: sleep_calls.append(s)

    result = device_auth.DeviceAuthManager._read_chronicle_url()

    assert result == 'http://chronicle.local:7979'
    assert sleep_calls == []


def test_read_chronicle_url_retries_when_initially_empty(kodi):
    """The exact BUG-041 race: the setting settles to a real value partway
    through the retry window rather than being present on the first read."""
    device_auth = _import_device_auth()

    reads = ['', '', 'http://chronicle.local:7979']
    call_count = {'n': 0}

    def fake_get_setting(key):
        value = reads[min(call_count['n'], len(reads) - 1)]
        call_count['n'] += 1
        return value

    device_auth.ADDON.getSetting = fake_get_setting
    sleep_calls = []
    device_auth.time.sleep = lambda s: sleep_calls.append(s)

    result = device_auth.DeviceAuthManager._read_chronicle_url()

    assert result == 'http://chronicle.local:7979'
    assert len(sleep_calls) == 2  # slept before the 2nd and 3rd attempts


def test_read_chronicle_url_gives_up_after_max_attempts_all_empty(kodi):
    device_auth = _import_device_auth()
    device_auth.ADDON.getSetting = lambda key: ''

    sleep_calls = []
    device_auth.time.sleep = lambda s: sleep_calls.append(s)

    result = device_auth.DeviceAuthManager._read_chronicle_url()

    assert result == ''
    assert len(sleep_calls) == device_auth._URL_SETTLE_ATTEMPTS - 1


def test_run_shows_url_not_set_message_and_never_contacts_server_when_empty(kodi):
    """The distinct failure path this fix adds: an empty (never-settling) URL
    must show a specific "not set" message and must not attempt _initiate() at
    all -- proving it's not misreported as "could not contact Chronicle"."""
    import xbmcgui
    device_auth = _import_device_auth()
    device_auth.ADDON.getSetting = lambda key: ''
    device_auth.time.sleep = lambda s: None

    def _initiate_should_not_be_called(self, base_url):
        raise AssertionError('_initiate must not be called when chronicle_url never settles')
    device_auth.DeviceAuthManager._initiate = _initiate_should_not_be_called

    manager = device_auth.DeviceAuthManager()
    result = manager.run()

    assert result is False
    assert ('ok', '32060', '32108') in xbmcgui.dialog_calls


def test_run_calls_initiate_with_settled_url_when_configured(kodi):
    device_auth = _import_device_auth()
    device_auth.ADDON._settings['chronicle_url'] = 'http://chronicle.local:7979'

    captured = {}
    def fake_initiate(self, base_url):
        captured['base_url'] = base_url
        return None  # simulate contact failure -- just checking it was invoked correctly
    device_auth.DeviceAuthManager._initiate = fake_initiate

    import xbmcgui
    manager = device_auth.DeviceAuthManager()
    result = manager.run()

    assert result is False
    assert captured['base_url'] == 'http://chronicle.local:7979'
    assert ('ok', '32060', '32065') in xbmcgui.dialog_calls
