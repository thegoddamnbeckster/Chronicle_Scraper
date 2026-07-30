# -*- coding: utf-8 -*-
"""metadata.chronicle.python — Script entry point.

Shown when the user opens the addon from the Kodi add-on browser, or when
Kodi's "Change Content" scraper-configuration screen opens this addon's
Settings. The actual scraping (find/getdetails) lives in python/scraper.py,
invoked directly by Kodi's library scanner -- this file only handles
connecting the addon to a Chronicle account, same UX as Chronicle_Scrobbler.
"""

import sys

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.device_auth import DeviceAuthManager
from lib import nfo_rebuild

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
    """Parse action=... from RunScript(metadata.chronicle.python,action=...) calls."""
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
        ADDON.getLocalizedString(32000),   # "Chronicle Scraper"
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
    if args.get('action') == 'rebuild_nfos':
        _rebuild_nfos()
        return

    if not _is_configured():
        _refresh_auth_status()
        xbmcgui.Dialog().notification(
            ADDON.getLocalizedString(32000),   # "Chronicle Scraper"
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
        ADDON.getLocalizedString(32093),  # Rebuild local NFOs from Chronicle
        ADDON.getLocalizedString(32013),  # Open Settings
    ]

    dialog = xbmcgui.Dialog()
    choice = dialog.select(ADDON.getLocalizedString(32000), options)

    if choice == 0:
        _test_connection()
    elif choice == 1:
        _connect_to_chronicle()
    elif choice == 2:
        _rebuild_nfos()
    elif choice == 3:
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
    """Launch the QR device-auth flow to obtain an API key."""
    DeviceAuthManager().run()


def _rebuild_nfos():
    """Warns the user clearly, then -- only on explicit confirmation -- runs
    nfo_rebuild.run(): permanently deletes every local .nfo and movieset-*
    file the whole movie library has, and refreshes each movie so Chronicle
    repopulates them. See nfo_rebuild.py's module docstring for why this has
    to be a deliberate, explicit action rather than automatic."""
    dialog = xbmcgui.Dialog()
    confirmed = dialog.yesno(
        ADDON.getLocalizedString(32000),      # "Chronicle Scraper"
        ADDON.getLocalizedString(32094),      # warning text
        nolabel=ADDON.getLocalizedString(32096),
        yeslabel=ADDON.getLocalizedString(32095),
    )
    if not confirmed:
        return

    progress = xbmcgui.DialogProgress()
    progress.create(ADDON.getLocalizedString(32093))  # "Rebuild local NFOs from Chronicle"

    def on_progress(index, total, label):
        percent = int(index * 100 / total) if total else 0
        progress.update(percent, '{0}/{1}: {2}'.format(index + 1, total, label))

    try:
        processed, nfo_deleted, movieset_deleted, refresh_errors = nfo_rebuild.run(
            progress_callback=on_progress, is_cancelled=progress.iscanceled)
    finally:
        progress.close()

    dialog.ok(
        ADDON.getLocalizedString(32093),
        ADDON.getLocalizedString(32097).format(processed, nfo_deleted, movieset_deleted, refresh_errors),
    )


if __name__ == '__main__':
    show_menu()
