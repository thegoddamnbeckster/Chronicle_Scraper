# -*- coding: utf-8 -*-
"""script.chronicle.scraper.movie — Script entry point.

Shown when the user opens the addon from the Kodi add-on browser, or when
Kodi's "Change Content" scraper-configuration screen opens this addon's
Settings. The actual scraping (find/getdetails) lives in python/scraper.py,
invoked directly by Kodi's library scanner -- this file only handles
connecting the addon to a Chronicle account, same UX as Chronicle_Scrobbler.
"""

import sys
import threading

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.connection_ui import ConnectionUi, parse_script_args
from lib.device_auth import DeviceAuthManager
from lib import collection_art_sync
from lib import device_registration
from lib import watch_rating_sync

ADDON = xbmcaddon.Addon()
log   = Logger('default')


def _on_connected():
    # Best-effort, on a background thread -- one Chronicle POST, nothing this UI flow should wait
    # for. See lib/device_registration.py.
    threading.Thread(target=device_registration.register, name='chronicle-device-register', daemon=True).start()


_ui = ConnectionUi(ADDON, log, ChronicleClient, lambda base_url: DeviceAuthManager(base_url=base_url), _on_connected)


def _get_args():
    return parse_script_args(sys.argv)


_refresh_auth_status = _ui.refresh_auth_status
_test_connection = _ui.test_connection
_connect_to_chronicle = _ui.connect
_change_chronicle_url = _ui.change_url


def show_menu():
    """Entry point for both a plain addon-browser launch (no action= arg --
    goes straight to Settings) and every RunScript(...,action=X) call a
    Settings action button makes (dispatched below, each returning before
    Settings would otherwise open). "Edit Connection" works even when
    unconfigured, since it's the one reliable place the URL ever gets
    entered -- see _connect_to_chronicle()'s own docstring for why the
    Settings screen no longer does that job itself. Per-user correction
    (2026-08-29): "I only ever want them to open the regular settings
    window, not whatever [the old action-list menu] is."
    """
    args = _get_args()
    if args.get('action') == 'auth':
        _connect_to_chronicle()
        return
    if args.get('action') == 'change_url':
        _change_chronicle_url()
        return
    if args.get('action') == 'sync_watch_ratings':
        _sync_watch_ratings_now()
        return
    if args.get('action') == 'sync_collections':
        _sync_collections_now()
        return
    if args.get('action') == 'test_connection':
        _test_connection()
        return

    _refresh_auth_status()
    ADDON.openSettings()


def _sync_watch_ratings_now():
    """Manual "Sync Watch History and Ratings Now" action -- runs the same
    watch_rating_sync.run() pass the background service triggers automatically (on load, and on
    its own interval), immediately and on demand. No confirmation dialog: this never touches a
    local file, only Kodi's own VideoLibrary fields and Chronicle's API, so there's nothing
    destructive to warn about."""
    bg = xbmcgui.DialogProgressBG()
    bg.create(ADDON.getLocalizedString(32134))

    def on_progress(index, total, label):
        percent = min(100, int(index * 100 / total)) if total else 0
        bg.update(percent, message=label)

    try:
        result = watch_rating_sync.run(progress_callback=on_progress)
    finally:
        bg.close()

    # A button press expects an answer, so a pass that changed nothing says so (the background pass stays silent).
    message = (watch_rating_sync.describe_result(result, ADDON.getLocalizedString)
               or ADDON.getLocalizedString(32165))
    xbmcgui.Dialog().notification(
        ADDON.getLocalizedString(32134),
        message,
        icon=xbmcgui.NOTIFICATION_INFO if not result['errors'] else xbmcgui.NOTIFICATION_WARNING,
        time=8000,
    )


def _sync_collections_now():
    """Manual "Sync Collection Art Now" action -- compares EVERY collection with Kodi's (not just
    the ones Chronicle says changed), immediately and on demand, and writes only the art that
    differs. No confirmation dialog, same reasoning as _sync_watch_ratings_now above: it sets
    art on Kodi's movie sets and touches no file."""
    bg = None

    def on_progress(index, total, label):
        nonlocal bg
        if bg is None:
            bg = xbmcgui.DialogProgressBG()
            bg.create(ADDON.getLocalizedString(32143))
        percent = min(100, int(index * 100 / total)) if total else 0
        bg.update(percent, message=label)

    try:
        result = collection_art_sync.run(progress_callback=on_progress, everything=True)
    finally:
        if bg is not None:
            bg.close()

    # A button press expects an answer, so a pass that changed nothing says so.
    message = (collection_art_sync.describe_result(result, ADDON.getLocalizedString)
               or ADDON.getLocalizedString(32165))
    xbmcgui.Dialog().notification(
        ADDON.getLocalizedString(32143),
        message,
        icon=xbmcgui.NOTIFICATION_INFO if not result['errors'] else xbmcgui.NOTIFICATION_WARNING,
        time=8000,
    )


if __name__ == '__main__':
    show_menu()
