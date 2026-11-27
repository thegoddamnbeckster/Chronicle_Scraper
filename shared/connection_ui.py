# -*- coding: utf-8 -*-
"""The "connect this add-on to Chronicle" screens, shared by the movie scraper, the TV scraper and the scrobbler.

All three add-ons offered the same actions from their Settings page -- Edit Connection (the QR device-auth flow), Change URL,
Test Connection, and the "Connected as ..." status line -- and each carried its own copy of the code, with the same fixes made three
times (or, sometimes, not). Each add-on's default.py now builds one ConnectionUi and delegates to it.

Why the URL is entered here and nowhere else: Settings' own chronicle_url text field is read-only. Kodi's on-screen-keyboard edit of
that field was confirmed live (2026-08-27) not to land reliably in the underlying setting, so this flow is the one place the address is
ever typed. An already-connected reconnect skips the prompt and goes straight to the QR window: a working URL is never asked for twice.

Settings are saved the moment they change. Edit Connection and Change URL close the Settings window before they run (option="close" in
each settings.xml) and open it again when they finish: while the window is open, Kodi keeps a script's setSetting() in the window and
writes it to disk only on OK, so a Test Connection run straight after reconnecting read the old key. Every read and write here also
goes through a new xbmcaddon.Addon(): Kodi saves an add-on object's whole copy of the settings on each write, so an object made before
the new key was saved would put the old one back.

String ids used (the same in every add-on's strings.po): 32000 add-on name, 32002 "Chronicle URL", 32012 "Test Connection",
32020 "Connection successful!", 32021 "Connection failed:", 32060 "Connect", 32081 "Connected", 32082 "Not connected",
32083 loopback warning, 32111 change-URL confirmation, 32120 "Connected as {0}".
"""

import traceback

import xbmcaddon
import xbmcgui

from lib.connection_health import UNAUTHORIZED
from lib.http_core import find_shared_chronicle_url, is_loopback, normalize_base_url

# Shown in the Settings status line when Chronicle answers 401/403 to this add-on's key.
REJECTED_STATUS = 'Not connected - Chronicle rejected the API key. Choose Edit Connection to reconnect.'

# Every string id the flow reads, so each add-on's build can be checked for them (see the tests).
STRING_IDS = (32000, 32002, 32012, 32020, 32021, 32060, 32081, 32082, 32083, 32111, 32120)


class ConnectionUi:
    def __init__(self, addon, log, client_factory, device_auth_factory, on_connected=None, health=None, addon_factory=None):
        """addon: the add-on's xbmcaddon.Addon, for its id and strings. client_factory(): a ChronicleClient. device_auth_factory(base_url): a
        DeviceAuthManager whose run() returns True once an API key was saved. on_connected(): optional, called after a successful
        connect (the movie scraper registers the device with Chronicle). health: the add-on's ConnectionHealth, so the status line can
        say the key was rejected instead of showing the last name that worked. addon_factory(): a new xbmcaddon.Addon(), used for every
        settings read and write."""
        self._addon = addon
        self._log = log
        self._client_factory = client_factory
        self._device_auth_factory = device_auth_factory
        self._on_connected = on_connected
        self._health = health
        self._addon_factory = addon_factory or xbmcaddon.Addon

    def _text(self, string_id):
        return self._addon.getLocalizedString(string_id)

    def _get(self, key):
        return self._addon_factory().getSetting(key)

    def _set(self, key, value):
        self._addon_factory().setSetting(key, value)

    # ── status ──────────────────────────────────────────────────────────────────

    def refresh_auth_status(self):
        """Keeps the read-only Settings status field honest, and says WHO is connected.

        Requires BOTH chronicle_url and api_key (a key left over from a past connection must not read "Connected" while the URL is
        empty and every attempt fails). When configured, the identity is fetched (GET /users/me, 5 s bound so opening the menu never
        feels stuck) so the status reads "Connected as {name}"; connected_display_name (a hidden setting) keeps the last name that
        worked so a network blip does not regress it to the bare fallback. A key revoked on the server shows up here too.
        """
        if not (self._get('chronicle_url') and self._get('api_key')):
            self._set('auth_status', self._text(32082))
            return

        user = self._client_factory().get_current_user()
        if self._health is not None and self._health.current_problem() == UNAUTHORIZED:
            self._set('auth_status', REJECTED_STATUS)
            return
        name = None
        if user:
            name = user.get('displayName') or user.get('username')
            if name:
                self._set('connected_display_name', name)
        if not name:
            name = self._get('connected_display_name')

        self._set('auth_status', self._text(32120).format(name) if name else self._text(32081))

    # ── actions ─────────────────────────────────────────────────────────────────

    def test_connection(self):
        """Tests connectivity to Chronicle and shows the result."""
        ok, message = self._client_factory().test_connection()
        dialog = xbmcgui.Dialog()
        if ok:
            dialog.ok(self._text(32012), self._text(32020))
        else:
            dialog.ok(self._text(32012), '{0}\n{1}'.format(self._text(32021), message))

    def warn_if_localhost(self, url):
        """After a new URL is entered: True when it is fine to go on with it (nothing to warn about, or the user chose to keep it).
        An address that only works if Chronicle runs on this very device is usually a mistake -- Kodi and Chronicle are commonly on
        different machines. When the user declines, the saved URL is cleared and False is returned so the caller stops."""
        if not url or not is_loopback(url):
            return True
        keep = xbmcgui.Dialog().yesno(self._text(32000), self._text(32083))
        if not keep:
            self._set('chronicle_url', '')
        return keep

    def show_settings(self):
        """Opens the add-on's Settings window with an up-to-date status line and what is saved now."""
        self.refresh_auth_status()
        self._addon_factory().openSettings()

    def connect(self, ask_url=False, prefill=None):
        """"Edit Connection": prompts for the URL when none is saved (or always, with ask_url), then runs the QR device-auth flow to
        obtain an API key. prefill: the address the prompt starts with (Change URL passes the old one, so a typo is a quick fix).
        The Settings window is opened again afterwards, however the flow ended."""
        self._connect(ask_url, prefill)
        self.show_settings()

    def _connect(self, ask_url, prefill):
        log = self._log
        # ask_url never re-reads the setting: Change URL has just cleared it and wants the address typed.
        current = '' if ask_url else self._get('chronicle_url')
        log.info('connect: invoked; chronicle_url on disk = {0!r}, ask_url = {1}'.format(current, ask_url))

        if not current:
            shared_url = prefill or find_shared_chronicle_url(self._addon.getAddonInfo('id'), log)
            if shared_url:
                log.info('connect: pre-filling the URL prompt with {0!r}'.format(shared_url))
            entered = (xbmcgui.Dialog().input(self._text(32002), defaultt=shared_url or '') or '').strip()
            log.info('connect: URL prompt returned {0!r}'.format(entered))
            if not entered:
                log.info('connect: cancelled -- aborting')
                return
            url = normalize_base_url(entered)
            if not url:
                xbmcgui.Dialog().ok(self._text(32060), 'That is not a valid http:// or https:// address: {0}'.format(entered))
                return
            self._set('chronicle_url', url)
            log.info('connect: saved new URL {0!r}'.format(url))
            if not self.warn_if_localhost(url):
                log.info('connect: user declined a loopback URL -- aborting')
                return
            current = url

        log.info('connect: starting the device-auth flow for {0!r}'.format(current))
        connected = False
        try:
            connected = self._device_auth_factory(current).run()
        except Exception:  # noqa: BLE001 -- a RunScript script has no visible crash surface; log it and say so
            log.error('connect: the device-auth flow raised:\n{0}'.format(traceback.format_exc()))
            xbmcgui.Dialog().ok(self._text(32060), 'Connect failed unexpectedly -- see kodi.log for details.')
        log.info('connect: device-auth flow returned {0}'.format(connected))

        if connected:
            # The address the key was issued by is saved again with it, so a key can never be left on disk without its URL.
            self._set('chronicle_url', current)
            if self._on_connected:
                self._on_connected()

    def change_url(self):
        """The one way to correct a saved-but-wrong URL (server moved, a typo): confirms first, since the old API key belongs to the
        old server and is cleared with it, then connects again."""
        current = self._get('chronicle_url')
        if current:
            if not xbmcgui.Dialog().yesno(self._text(32000), self._text(32111).format(current)):
                self.show_settings()
                return
            self._set('chronicle_url', '')
            self._set('api_key', '')
            self._log.info('change_url: cleared saved URL {0!r} and the API key'.format(current))
        self.connect(ask_url=True, prefill=current or None)


def parse_script_args(argv):
    """key=value arguments of a RunScript(<addon id>,action=...) call (argv[0] is the script itself)."""
    args = {}
    for arg in argv[1:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            args[key] = value
    return args
