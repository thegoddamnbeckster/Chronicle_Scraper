# -*- coding: utf-8 -*-
"""script.chronicle.scraper.tv — Script entry point.

Shown when the user opens the addon from the Kodi add-on browser, or when
Kodi's "Change Content" scraper-configuration screen opens this addon's
Settings. The actual scraping (find/getdetails/getepisodelist/
getepisodedetails) lives in python/tvshow_scraper.py, invoked directly by
Kodi's library scanner -- this file only handles connecting the addon to a
Chronicle account, same UX as Chronicle Scraper (Movies) and
Chronicle_Scrobbler.

Deliberately does NOT duplicate "Rebuild local NFOs from Chronicle" or the
corner status indicator -- those live in the Movies addon's own default.py/
service.py and already cover both movies and TV shows together (see
lib/nfo_rebuild.py and lib/activity_tracker.py there, both keyed by
cross-process signal files under special://temp/chronicle_scraper/, not by
addon id, so either addon's rebuild pass reaches both). Adding a second copy
of that menu entry here would just be two buttons that do the exact same
thing.
"""

import sys
import traceback

import xbmc
import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.device_auth import DeviceAuthManager

ADDON = xbmcaddon.Addon()
log   = Logger('default')


def _is_configured():
    """True once a server URL and API key are both present.

    "Connect to Chronicle" lives as an action button on the Settings page
    itself (resources/settings.xml), not in this menu -- so requiring the key
    here doesn't create a dead end: an unconfigured click goes straight to
    Settings, where the URL field and the connect button sit side by side.
    """
    return bool(ADDON.getSetting('chronicle_url')) and bool(ADDON.getSetting('api_key'))


def _get_args():
    """Parse action=... from RunScript(script.chronicle.scraper.tv,action=...) calls."""
    args = {}
    for arg in sys.argv[1:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            args[key] = value
    return args


def _refresh_auth_status():
    """Keep the read-only Settings status field honest before Settings is shown."""
    connected = bool(ADDON.getSetting('api_key'))
    ADDON.setSetting(
        'auth_status',
        ADDON.getLocalizedString(32081 if connected else 32082),  # "Connected" / "Not connected"
    )


_LOOPBACK_MARKERS = ('localhost', '127.0.0.1', '::1')


def _warn_if_localhost():
    """Catch a URL that will only work if Chronicle runs on this same device --
    Kodi and Chronicle are commonly on separate machines. Runs right after
    Settings closes rather than reactively on every settings change, so it
    only fires once per visit instead of on every unrelated toggle.
    """
    url = ADDON.getSetting('chronicle_url').lower()
    if not url or not any(marker in url for marker in _LOOPBACK_MARKERS):
        return

    keep = xbmcgui.Dialog().yesno(
        ADDON.getLocalizedString(32000),   # "Chronicle Scraper (TV)"
        ADDON.getLocalizedString(32083),   # loopback warning text
    )
    if not keep:
        ADDON.setSetting('chronicle_url', '')


def show_menu():
    """Display the main action menu, or jump straight to Settings on first run."""
    args = _get_args()
    if args.get('action') == 'auth':
        _connect_to_chronicle()
        return

    if not _is_configured():
        _refresh_auth_status()
        xbmcgui.Dialog().notification(
            ADDON.getLocalizedString(32000),   # "Chronicle Scraper (TV)"
            ADDON.getLocalizedString(32079),   # "Not configured yet — opening settings…"
            xbmcgui.NOTIFICATION_INFO,
            4000,
        )
        ADDON.openSettings()
        _warn_if_localhost()
        return

    options = [
        ADDON.getLocalizedString(32012),  # Test Connection
        ADDON.getLocalizedString(32061),  # Connect to Chronicle
        ADDON.getLocalizedString(32013),  # Open Settings
    ]

    dialog = xbmcgui.Dialog()
    choice = dialog.select(ADDON.getLocalizedString(32000), options)

    if choice == 0:
        _test_connection()
    elif choice == 1:
        _connect_to_chronicle()
    elif choice == 2:
        _refresh_auth_status()
        ADDON.openSettings()
        _warn_if_localhost()


def _test_connection():
    """Test connectivity to Chronicle and display a result dialog."""
    client  = ChronicleClient()
    dialog  = xbmcgui.Dialog()
    ok, msg = client.test_connection()

    if ok:
        dialog.ok(
            ADDON.getLocalizedString(32012),
            ADDON.getLocalizedString(32020),   # Connection successful!
        )
    else:
        dialog.ok(
            ADDON.getLocalizedString(32012),
            '{0}\n{1}'.format(ADDON.getLocalizedString(32021), msg),   # Connection failed: <msg>
        )


def _connect_to_chronicle():
    """Launch the QR device-auth flow to obtain an API key.

    Closes the Settings dialog first, if it's the one that's actually open.
    Root-caused (2026-08-27) by reading settings.xml directly moments after a
    failed Connect attempt: chronicle_url was still sitting at its empty
    default a full minute-plus after the user had typed a URL and could see
    it in the field. Not a timing race -- an ORDERING one. Kodi's addon
    Settings dialog only writes settings.xml to disk when the whole dialog
    closes, not per-field as focus moves between controls. "Connect to
    Chronicle" is a button INSIDE that same still-open dialog (RunScript
    launches this as a brand-new script instance while Settings is still
    up), so no amount of waiting afterward can see a value that was never
    written in the first place -- an earlier fix here (v1.1.4) tried a
    settle-wait before reading and confirmed exactly that: still empty after
    waiting, because there was nothing to wait FOR yet.

    Window.IsActive(addonsettings) gates the Action(Back) so this only ever
    closes something when Settings is confirmed to actually be the active
    window -- reached from the main menu (Settings already closed) this is a
    no-op, never risking closing the wrong window.

    Reopens Settings right after closing it (Addon.OpenSettings, NOT
    ADDON.openSettings() -- the builtin fires the window open and returns
    immediately, where the Python method blocks this script until the user
    closes it again) so the close is a quick visual blip instead of dumping
    the user back at whatever was behind Settings for the rest of the QR
    flow. Kodi's own window manager remembers the last-focused control per
    window, so this should land back on "Connect to Chronicle" automatically
    -- not something this addon controls directly, so if it doesn't restore
    focus exactly as expected on a given skin/Kodi version, that's a Kodi
    behavior to note, not a sign this step failed to run.
    """
    settings_open = xbmc.getCondVisibility('Window.IsActive(addonsettings)')
    log.info('_connect_to_chronicle: invoked; Settings dialog open = {0}'.format(settings_open))
    if settings_open:
        log.info('_connect_to_chronicle: closing Settings (Action(Back)) to force a flush')
        xbmc.executebuiltin('Action(Back)')
        xbmc.sleep(400)   # let the close + settings.xml write actually complete
        log.info('_connect_to_chronicle: chronicle_url on disk = {0!r}'.format(
                 ADDON.getSetting('chronicle_url')))
        log.info('_connect_to_chronicle: reopening Settings (Addon.OpenSettings) as a quick blip')
        xbmc.executebuiltin('Addon.OpenSettings({0})'.format(ADDON.getAddonInfo('id')))
        xbmc.sleep(300)   # let the reopened Settings window actually render before the
                          # QR overlay appears on top of it
    else:
        log.info('_connect_to_chronicle: chronicle_url on disk = {0!r}'.format(
                 ADDON.getSetting('chronicle_url')))

    log.info('_connect_to_chronicle: calling DeviceAuthManager().run()')
    try:
        DeviceAuthManager().run()
    except Exception:
        # RunScript-launched scripts have no visible crash surface -- an unhandled
        # exception here would otherwise look EXACTLY like "the connection window
        # never showed up" to the user, with nothing in the log tying the two
        # together unless this is caught and logged explicitly with a traceback.
        log.error('_connect_to_chronicle: DeviceAuthManager().run() raised:\n{0}'.format(
                  traceback.format_exc()))
        xbmcgui.Dialog().ok(
            ADDON.getLocalizedString(32060),
            'Connect failed unexpectedly -- see kodi.log for details.',
        )
    log.info('_connect_to_chronicle: DeviceAuthManager().run() returned')


if __name__ == '__main__':
    show_menu()
