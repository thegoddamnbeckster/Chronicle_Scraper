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

    Confirms the Chronicle URL right here rather than trusting whatever is
    already on disk in chronicle_url -- when this is reached via the
    "Connect to Chronicle" action button INSIDE the still-open Settings
    dialog (RunScript launches this as a brand new process while Settings
    is still up), a URL the user just typed into that same dialog is not
    guaranteed to have been flushed to the addon's on-disk settings.xml yet
    -- ADDON.getSetting('chronicle_url') here could then read the OLD value
    even though the new one is visibly still sitting in the field a few
    pixels away. Confirmed live complaint: switching URLs and immediately
    clicking Connect built the QR code against the stale one. Asking
    explicitly here -- rather than only ever trusting whatever settings.xml
    happens to already contain -- sidesteps that GUI-commit-timing question
    entirely, and doubles as the fix for a fresh install with no URL yet:
    the user is prompted for it right here instead of needing Settings to
    already have a value flushed before Connect can do anything useful with it.
    """
    current = ADDON.getSetting('chronicle_url')
    log.info('_connect_to_chronicle: invoked; chronicle_url on disk = {0!r}'.format(current))

    entered = xbmcgui.Dialog().input(
        ADDON.getLocalizedString(32002), defaultt=current)  # "Chronicle URL"
    log.info('_connect_to_chronicle: URL prompt returned {0!r}'.format(entered))
    entered = (entered or '').strip()

    if not entered:
        if not current:
            log.warning('_connect_to_chronicle: no URL entered and none already set -- aborting')
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32085),  # "Chronicle URL is not set..."
            )
        else:
            log.info('_connect_to_chronicle: prompt returned empty (cancelled) -- '
                      'keeping existing URL {0!r}, aborting Connect'.format(current))
        return  # cancelled, or genuinely nothing to connect to

    if entered != current:
        ADDON.setSetting('chronicle_url', entered)
        log.info('_connect_to_chronicle: URL changed {0!r} -> {1!r}, saved'.format(current, entered))
    else:
        log.info('_connect_to_chronicle: URL unchanged, proceeding')

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
