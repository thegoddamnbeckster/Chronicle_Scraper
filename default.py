# -*- coding: utf-8 -*-
"""script.chronicle.scraper.movie — Script entry point.

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


def _format_duration(seconds):
    """Renders a countdown as 'Xh Ym' / 'Ym' / 'less than a minute', for the
    NFO rebuild's wait-phase ETA -- always rounds up so a shown estimate
    never expires before the thing it's estimating actually can."""
    minutes = int(seconds // 60) + (1 if seconds % 60 else 0)
    if minutes <= 0:
        return ADDON.getLocalizedString(32104)  # "less than a minute"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return '{0}h {1}m'.format(hours, minutes)
    if hours:
        return '{0}h'.format(hours)
    return '{0}m'.format(minutes)


def _is_configured():
    """True once a server URL and API key are both present.

    "Connect to Chronicle" lives as an action button on the Settings page
    itself (resources/settings.xml), not in this menu -- so requiring the key
    here doesn't create a dead end: an unconfigured click goes straight to
    Settings, where the URL field and the connect button sit side by side.
    """
    return bool(ADDON.getSetting('chronicle_url')) and bool(ADDON.getSetting('api_key'))


def _get_args():
    """Parse action=... from RunScript(script.chronicle.scraper.movie,action=...) calls."""
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
    entered = xbmcgui.Dialog().input(
        ADDON.getLocalizedString(32002), defaultt=current)  # "Chronicle URL"
    entered = (entered or '').strip()

    if not entered:
        if not current:
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32085),  # "Chronicle URL is not set..."
            )
        return  # cancelled, or genuinely nothing to connect to

    if entered != current:
        ADDON.setSetting('chronicle_url', entered)
        log.info('Chronicle URL updated via Connect flow: {0}'.format(entered))

    DeviceAuthManager().run()


def _rebuild_nfos():
    """Warns the user clearly, then -- only on explicit confirmation -- runs
    nfo_rebuild.run(): deletes every local .nfo/tvshow.nfo and movieset-*
    file across the whole movie and TV library (preserving whatever data
    they held first -- see lib/legacy_nfo.py and
    collection_sync.preserve_local_movieset_file), and refreshes each item
    so Chronicle repopulates them. See nfo_rebuild.py's module docstring for
    why this has to be a deliberate, explicit action rather than automatic."""
    if not ADDON.getSettingBool('write_nfo'):
        xbmcgui.Dialog().ok(
            ADDON.getLocalizedString(32000),  # "Chronicle Scraper"
            ADDON.getLocalizedString(32108),
        )
        return

    dialog = xbmcgui.Dialog()
    confirmed = dialog.yesno(
        ADDON.getLocalizedString(32000),      # "Chronicle Scraper"
        ADDON.getLocalizedString(32094),      # warning text
        nolabel=ADDON.getLocalizedString(32096),
        yeslabel=ADDON.getLocalizedString(32095),
    )
    if not confirmed:
        return

    # Issue phase: a real foreground DialogProgress, Cancel available -- the
    # addon script is actively doing something here (issuing delete+refresh
    # per movie) that's worth a Cancel button and worth blocking on.
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON.getLocalizedString(32093))  # "Rebuild local NFOs from Chronicle"
    state = {'modal_closed': False, 'bg': None}

    def on_progress(index, total, label):
        percent = int(index * 100 / total) if total else 0
        progress.update(percent, '{0}/{1}: {2}'.format(index + 1, total, label))

    def on_issuing_complete(pending_total, budget_seconds):
        # From here on, the only thing left is Kodi's own library queue
        # draining -- nothing the user could usefully Cancel, and nothing
        # that needs them watching. Close the modal, tell them once, then
        # switch to a background (non-blocking) indicator so Kodi stays
        # fully usable -- including playback -- for however long this takes.
        progress.close()
        state['modal_closed'] = True
        xbmcgui.Dialog().ok(
            ADDON.getLocalizedString(32093),
            ADDON.getLocalizedString(32100).format(
                pending_total, _format_duration(budget_seconds)),
        )
        bg = xbmcgui.DialogProgressBG()
        bg.create(ADDON.getLocalizedString(32093))
        state['bg'] = bg

    def on_wait_progress(confirmed, pending_total, waited_seconds, budget_seconds):
        bg = state['bg']
        if bg is None:
            return
        # budget_seconds is nfo_rebuild's own ceiling, already sized off the
        # pending count (floor + N seconds/movie), so "budget - elapsed" is
        # an honest worst-case remaining estimate -- it can only under-run,
        # never blow past what's shown, since confirmations that arrive
        # faster than the per-movie budget just end the wait early.
        remaining = max(0, budget_seconds - waited_seconds)
        percent = int(confirmed * 100 / pending_total) if pending_total else 100
        bg.update(percent, message=ADDON.getLocalizedString(32103).format(
            confirmed, pending_total, _format_duration(remaining)))

    try:
        result = nfo_rebuild.run(
            progress_callback=on_progress, is_cancelled=progress.iscanceled,
            wait_progress_callback=on_wait_progress, on_issuing_complete=on_issuing_complete)
    finally:
        if not state['modal_closed']:
            progress.close()
        if state['bg'] is not None:
            state['bg'].close()

    # Two genuinely different outcomes get genuinely different messages,
    # rather than one line trying to carry every number at once (deleted
    # counts, confirmed counts, errors) with no explanation of how they
    # relate -- that was the actual problem with the old single summary.
    if result['cancelled']:
        # An explicit interruption, not a routine background finish -- worth
        # a modal the user has to acknowledge, since it's telling them
        # something they didn't expect (the run stopped short) rather than
        # just confirming something they were already told to expect.
        xbmcgui.Dialog().ok(
            ADDON.getLocalizedString(32093),
            ADDON.getLocalizedString(32102).format(result['processed'], result['total']),
        )
        return

    problem_count = result['unconfirmed_count'] + result['refresh_errors']
    if problem_count == 0:
        message = ADDON.getLocalizedString(32097).format(result['total'])
    else:
        message = ADDON.getLocalizedString(32101).format(
            result['nfo_confirmed'], result['pending_total'], problem_count)

    # A notification, not a modal dialog.ok() -- by the time this fires the
    # user was told to go do something else, quite possibly playback, and a
    # blocking dialog here would rudely interrupt whatever that turned out
    # to be. Kodi's own notification popup is enough to confirm it's done.
    xbmcgui.Dialog().notification(
        ADDON.getLocalizedString(32093),
        message,
        icon=xbmcgui.NOTIFICATION_INFO,
        time=10000,
    )


if __name__ == '__main__':
    show_menu()
