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
a working URL is never asked for twice. To actually change an existing URL
(e.g. after a server move), use "Change Chronicle URL" -- a separate menu
item (_change_chronicle_url()) that explicitly clears the saved URL and API
key after confirmation, then falls through to this same entry point.
"""

import sys
import traceback

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient, find_shared_chronicle_url
from lib.device_auth import DeviceAuthManager
from lib import settings_mirror

ADDON = xbmcaddon.Addon()
log   = Logger('default')

# The sibling package's addon id -- see lib/settings_mirror.py's own
# docstring for why write_nfo/write_streamdetails are worth offering to
# mirror there.
_SIBLING_ADDON_ID = 'script.chronicle.scraper.movie'


def _get_args():
    """Parse action=... from RunScript(script.chronicle.scraper.tv,action=...) calls."""
    args = {}
    for arg in sys.argv[1:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            args[key] = value
    return args


def _refresh_auth_status():
    """Keep the read-only Settings status field (and show_menu()'s own dialog
    heading -- see there) honest, and say WHO before offering to reconnect.

    Requires BOTH chronicle_url and api_key. Previously checked api_key alone,
    so it kept showing "Connected" purely because an api_key from a PAST
    successful connection was still saved, even while chronicle_url sat empty
    and every actual Connect attempt was failing outright. Confirmed live
    (2026-08-27): status showed "Connected" immediately after a Connect
    attempt that never got past "URL not set."

    When configured, also fetches the connected identity (GET /users/me,
    5s-bounded -- see ChronicleClient.get_current_user()'s own docstring for
    why this can't be allowed to make menu-opening feel stuck) so the status
    reads "Connected as {name}" instead of a bare "Connected" -- per-user
    request (2026-08-28): know WHO is connected before doing anything else,
    not just whether a key happens to be saved. A saved key that's actually
    been revoked server-side surfaces here too (the lookup fails, falling
    back to the last-known name -- see below -- rather than silently keeping
    a stale "Connected").

    connected_display_name (hidden setting) caches the last successful
    lookup so a transient network blip doesn't regress an already-known name
    back to the bare fallback -- only a NEVER-yet-successful lookup (fresh
    install, or a key that's never actually worked) falls all the way back
    to the generic "Connected" with no name.
    """
    connected = bool(ADDON.getSetting('chronicle_url')) and bool(ADDON.getSetting('api_key'))
    if not connected:
        ADDON.setSetting('auth_status', ADDON.getLocalizedString(32082))  # "Not connected"
        return

    user = ChronicleClient().get_current_user()
    name = None
    if user:
        name = user.get('displayName') or user.get('username')
        if name:
            ADDON.setSetting('connected_display_name', name)

    if not name:
        name = ADDON.getSetting('connected_display_name')  # last-known, if any

    if name:
        ADDON.setSetting('auth_status', ADDON.getLocalizedString(32120).format(name))  # "Connected as {0}"
    else:
        ADDON.setSetting('auth_status', ADDON.getLocalizedString(32081))  # "Connected"


_LOOPBACK_MARKERS = ('localhost', '127.0.0.1', '::1')


def _warn_if_localhost():
    """Catch a URL that will only work if Chronicle runs on this same device --
    Kodi and Chronicle are commonly on separate machines. Runs right after
    Connect saves a new URL.

    Returns True when it's fine to proceed with the URL just saved (nothing to
    warn about, or the user chose to keep it anyway), False when the user
    declined -- chronicle_url is cleared on disk in that case, and the caller
    must stop using the URL it just read rather than proceeding with it
    regardless. Previously this returned nothing and the caller pressed on
    with the declined URL anyway; see _connect_to_chronicle()'s own docstring.
    """
    url = ADDON.getSetting('chronicle_url').lower()
    if not url or not any(marker in url for marker in _LOOPBACK_MARKERS):
        return True

    keep = xbmcgui.Dialog().yesno(
        ADDON.getLocalizedString(32000),   # "Chronicle Scraper (TV)"
        ADDON.getLocalizedString(32083),   # loopback warning text
    )
    if not keep:
        ADDON.setSetting('chronicle_url', '')
    return keep


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
    if args.get('action') == 'test_connection':
        _test_connection()
        return

    _refresh_auth_status()
    before = settings_mirror.snapshot(ADDON)
    ADDON.openSettings()
    settings_mirror.offer_mirror(ADDON, before, _SIBLING_ADDON_ID)


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

    Passes the resolved URL directly into DeviceAuthManager rather than
    letting it re-read chronicle_url from settings itself. Confirmed live
    (2026-08-27): a DIFFERENT xbmcaddon.Addon() instance's own getSetting()
    call -- device_auth.py's own module-level ADDON, not this one -- did NOT
    see the setSetting() this function had just done, even one line earlier
    in the same process. Handing the value over directly sidesteps that
    cross-instance consistency question entirely.
    """
    current = ADDON.getSetting('chronicle_url')
    log.info('_connect_to_chronicle: invoked; chronicle_url on disk = {0!r}'.format(current))

    if not current:
        shared_url = find_shared_chronicle_url()
        if shared_url:
            log.info('_connect_to_chronicle: pre-filling URL prompt from a sibling addon: {0!r}'.format(shared_url))
        entered = xbmcgui.Dialog().input(
            ADDON.getLocalizedString(32002), defaultt=shared_url or '')  # "Chronicle URL"
        entered = (entered or '').strip()
        log.info('_connect_to_chronicle: URL prompt returned {0!r}'.format(entered))
        if not entered:
            log.info('_connect_to_chronicle: cancelled -- aborting')
            return
        ADDON.setSetting('chronicle_url', entered)
        log.info('_connect_to_chronicle: saved new URL {0!r}'.format(entered))
        if not _warn_if_localhost():
            log.info('_connect_to_chronicle: user declined loopback URL -- aborting')
            return
        current = entered

    log.info('_connect_to_chronicle: calling DeviceAuthManager(base_url={0!r}).run()'.format(current))
    connected = False
    try:
        connected = DeviceAuthManager(base_url=current).run()
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
    log.info('_connect_to_chronicle: DeviceAuthManager().run() returned {0}'.format(connected))
    if not connected:
        # A successful run() already wrote auth_status="Connected" itself, through
        # its OWN module-level Addon() instance. Re-deriving it here immediately
        # afterward would re-read api_key through THIS module's own (different)
        # Addon() instance -- not guaranteed to see that just-written value yet,
        # the same cross-instance staleness this session already hit once (see
        # DeviceAuthManager.__init__'s own docstring). Only re-sync status on a
        # non-success, where nothing was just written and there's nothing to race.
        _refresh_auth_status()


def _change_chronicle_url():
    """Explicit escape hatch for a saved-but-wrong chronicle_url (server moved, a
    typo, a decommissioned host) -- the ONLY way to correct a non-empty
    chronicle_url anywhere in this addon: _connect_to_chronicle() only prompts
    for a URL when chronicle_url is currently empty, and Settings' own field is
    read-only (see resources/settings.xml). Confirms first since this also
    clears api_key -- the old key belongs to whatever server chronicle_url used
    to point at, not to wherever the user is about to point it next.
    """
    current = ADDON.getSetting('chronicle_url')
    if current:
        confirmed = xbmcgui.Dialog().yesno(
            ADDON.getLocalizedString(32000),
            ADDON.getLocalizedString(32111).format(current),
        )
        if not confirmed:
            return
        ADDON.setSetting('chronicle_url', '')
        ADDON.setSetting('api_key', '')
        log.info('_change_chronicle_url: cleared saved URL {0!r} and api_key'.format(current))
    _connect_to_chronicle()


if __name__ == '__main__':
    show_menu()
