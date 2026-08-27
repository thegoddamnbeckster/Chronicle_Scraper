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

## Chronicle URL: one entry point, not two

Settings' own chronicle_url text field is READ-ONLY (enable="false" in
resources/settings.xml) -- it exists only to show what's currently saved,
not to accept edits. That was a deliberate change (2026-08-27), not an
oversight: Kodi's on-screen-keyboard edit to that field was confirmed live
to not reliably land in the underlying setting at all on some setups, even
after the whole Settings dialog was fully closed -- not a timing issue, an
entry-not-committing one, upstream of anything this addon controls. Asking
the user to fight that control, and then potentially retype the same URL
into a fallback prompt when it silently failed, was worse than just having
one single, always-reliable entry point.

"Edit Connection" (_connect_to_chronicle(), reachable from both the main
menu and the Settings action button) is now that one entry point: it only
ever prompts for a URL when there isn't already a saved one, via
xbmcgui.Dialog().input() -- a dialog with its own explicit confirm step,
proven reliable where the Settings text field wasn't. An already-connected
reconnect skips the prompt entirely and goes straight to the QR window, so
a working URL is never asked for twice. To actually change an existing URL,
clear api_key first (or just run through "Edit Connection" again after a
server move -- a non-empty-but-wrong URL will simply fail Test Connection,
at which point re-running Connect after clearing chronicle_url via this same
flow is the fix).
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


def _get_args():
    """Parse action=... from RunScript(script.chronicle.scraper.tv,action=...) calls."""
    args = {}
    for arg in sys.argv[1:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            args[key] = value
    return args


def _refresh_auth_status():
    """Keep the read-only Settings status field honest before Settings is shown.

    Requires BOTH chronicle_url and api_key. Previously checked api_key alone,
    so it kept showing "Connected" purely because an api_key from a PAST
    successful connection was still saved, even while chronicle_url sat empty
    and every actual Connect attempt was failing outright. Confirmed live
    (2026-08-27): status showed "Connected" immediately after a Connect
    attempt that never got past "URL not set."
    """
    connected = bool(ADDON.getSetting('chronicle_url')) and bool(ADDON.getSetting('api_key'))
    ADDON.setSetting(
        'auth_status',
        ADDON.getLocalizedString(32081 if connected else 32082),  # "Connected" / "Not connected"
    )


_LOOPBACK_MARKERS = ('localhost', '127.0.0.1', '::1')


def _warn_if_localhost():
    """Catch a URL that will only work if Chronicle runs on this same device --
    Kodi and Chronicle are commonly on separate machines. Runs right after
    Connect saves a new URL.
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
    """Display the main action menu. No auto-bounce to Settings on first run --
    "Edit Connection" is directly reachable from here even when unconfigured,
    since it's the one reliable place the URL ever gets entered. See this
    module's own docstring for why the Settings screen no longer does that job.
    """
    args = _get_args()
    if args.get('action') == 'auth':
        _connect_to_chronicle()
        return

    _refresh_auth_status()

    options = [
        ADDON.getLocalizedString(32012),  # Test Connection
        ADDON.getLocalizedString(32061),  # Edit Connection
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
    """"Edit Connection" -- launches the QR device-auth flow to obtain an API key.

    Prompts for the Chronicle URL directly, via a reliable modal dialog, but
    ONLY when there isn't already a saved one -- see this module's own
    docstring for why Settings' text field is no longer trusted for this at
    all. An already-connected reconnect (chronicle_url already set) skips the
    prompt entirely and goes straight to the QR window: a working URL is
    never asked for twice.
    """
    current = ADDON.getSetting('chronicle_url')
    log.info('_connect_to_chronicle: invoked; chronicle_url on disk = {0!r}'.format(current))

    if not current:
        entered = xbmcgui.Dialog().input(ADDON.getLocalizedString(32002), defaultt='')  # "Chronicle URL"
        entered = (entered or '').strip()
        log.info('_connect_to_chronicle: URL prompt returned {0!r}'.format(entered))
        if not entered:
            log.info('_connect_to_chronicle: cancelled -- aborting')
            return
        ADDON.setSetting('chronicle_url', entered)
        log.info('_connect_to_chronicle: saved new URL {0!r}'.format(entered))
        _warn_if_localhost()

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
    _refresh_auth_status()


if __name__ == '__main__':
    show_menu()
