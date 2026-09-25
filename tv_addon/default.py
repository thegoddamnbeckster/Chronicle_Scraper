# -*- coding: utf-8 -*-
"""script.chronicle.scraper.tv — Script entry point.

Shown when the user opens the addon from the Kodi add-on browser, or when
Kodi's "Change Content" scraper-configuration screen opens this addon's
Settings. The actual scraping (find/getdetails/getepisodelist/
getepisodedetails) lives in python/tvshow_scraper.py, invoked directly by
Kodi's library scanner -- this file only handles connecting the addon to a
Chronicle account, same UX as Chronicle Scraper (Movies) and
Chronicle_Scrobbler.

Deliberately does NOT duplicate the corner status indicator -- that lives in
the Movies addon's own default.py/service.py (see lib/activity_tracker.py
there, keyed by a cross-process signal file under
special://temp/chronicle_scraper/, not by addon id, so it already covers
both addons). Local NFO writing/rebuilding was a separate feature that
existed in both addons and has since been removed entirely (2026-09-13) --
see git history if you need the old rationale.

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
import time
import traceback

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient, find_shared_chronicle_url
from lib.device_auth import DeviceAuthManager
from lib import library_repair

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
    if args.get('action') == 'library_repair_explain':
        _library_repair_explain()
        return
    if args.get('action') == 'library_repair_preview':
        _library_repair_preview()
        return
    if args.get('action') == 'library_repair':
        _library_repair()
        return
    if args.get('action') == 'library_repair_undo':
        _library_repair_undo()
        return

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


def _progress_bg(heading):
    """Creates an xbmcgui.DialogProgressBG and shows it IMMEDIATELY, before the caller does any
    detection/repair work -- confirmed live (2026-09-14): with nothing shown until that work
    returned, a scan or repair taking even a few seconds on a real library looked exactly like
    the remote click hadn't registered at all. Returns (bg, update) -- `update(message)` is a
    plain function suitable as library_repair's progress_callback (no xbmcgui reference crosses
    into that module; see its own docstring on why), advancing a rough, monotonically increasing
    percent alongside each new status line. The percent is deliberately approximate, not a real
    fraction-of-work-done: the underlying steps (a fast precondition check vs. a multi-second
    library backup) take wildly different real time, and a fake precise percentage across them
    would be more misleading than a steadily-advancing-but-inexact one. Capped short of 100 --
    the caller closes `bg` once its own work is actually done, not this."""
    bg = xbmcgui.DialogProgressBG()
    bg.create(heading, ADDON.getLocalizedString(32177))  # "Starting..." -- visible the instant this returns
    state = {'percent': 10}

    def update(message):
        state['percent'] = min(state['percent'] + 20, 90)
        bg.update(state['percent'], heading, message)

    return bg, update


def _library_repair_explain():
    """"What Is This?" -- shows the full plain-language explanation of Library Repair as an
    actual dialog. Confirmed live (2026-09-13): whatever renders a setting's own help= text
    isn't visible at all on at least one real device/skin this addon runs on, so the
    explanation cannot depend on a user ever seeing that panel -- this is the guaranteed path
    to it instead. Read-only, no lock, no database access of any kind."""
    xbmcgui.Dialog().ok(ADDON.getLocalizedString(32150), ADDON.getLocalizedString(32172))


def _library_repair_preview():
    """Read-only "Repair Stuck Episodes -- Preview" action -- runs detection only and shows
    what was found. Changes nothing, ever; see lib/library_repair.py's own module docstring.

    Shows a background progress indicator (_progress_bg) from the very first line, and the
    result is just the two numbers that actually matter -- total stuck episodes and how many
    shows they're spread across -- not a per-show breakdown of example file paths. Per-user
    request (2026-09-14): immediate feedback that the click registered, and a result that says
    how much needs fixing without making the user read through which shows are affected."""
    heading = ADDON.getLocalizedString(32151)
    bg, update = _progress_bg(heading)
    try:
        report = library_repair.preview(progress_callback=update)
    except library_repair.LibraryRepairError as exc:
        bg.close()
        log.warning('_library_repair_preview: aborted -- {0} ({1})'.format(exc.reason_code, exc.user_message))
        xbmcgui.Dialog().ok(heading, exc.user_message)
        return
    except Exception:
        bg.close()
        log.error('_library_repair_preview: unexpected error:\n{0}'.format(traceback.format_exc()))
        xbmcgui.Dialog().ok(heading, 'Preview failed unexpectedly -- see kodi.log for details.')
        return
    bg.close()

    stale_shows = report['stale_shows']
    stuck_file_ids = report['stuck_files']['file_ids']
    fabricated = report.get('fabricated_watched') or {'episodes': [], 'groups': []}
    if report['total_episodes'] == 0 and not stale_shows and not stuck_file_ids and not fabricated['episodes']:
        message = ADDON.getLocalizedString(32163)  # nothing wrong at all
    elif report['total_episodes'] == 0:
        message = ADDON.getLocalizedString(32176)  # neutral -- a stale-show/stuck-file/watched note follows below
    else:
        message = ADDON.getLocalizedString(32171).format(report['total_episodes'], len(report['groups']))
    if stale_shows:
        message += ADDON.getLocalizedString(32174).format(len(stale_shows))
    if stuck_file_ids:
        message += ADDON.getLocalizedString(32178).format(len(stuck_file_ids))
    if fabricated['episodes']:
        message += ADDON.getLocalizedString(32182).format(
            len(fabricated['episodes']), len({g['show_name'] for g in fabricated['groups']}))
    xbmcgui.Dialog().ok(heading, message)
    ADDON.setSetting('library_repair_last_result', message)


def _library_repair():
    """"Repair Stuck Episodes" -- the real, destructive action. Detects fresh (never reuses a
    result from a separate earlier Preview invocation), shows exactly what it found, and only on
    explicit confirmation runs the actual backup-then-delete pass. See
    lib/library_repair.py's own module docstring for the full safety design.

    Two separate _progress_bg indicators, back to back: one for detection (same "show something
    the instant the remote click lands" reasoning as Preview), and a second one, started fresh,
    for the actual backup-then-delete run after the user confirms -- that phase is the long one
    and per-user request (2026-09-14) needs its own ongoing "this is still running" feedback, not
    just a static heading with no status text."""
    heading = ADDON.getLocalizedString(32153)
    bg, update = _progress_bg(heading)
    try:
        db_path, report = library_repair.prepare_repair(progress_callback=update)
    except library_repair.LibraryRepairError as exc:
        bg.close()
        log.warning('_library_repair: aborted before detection -- {0} ({1})'.format(exc.reason_code, exc.user_message))
        xbmcgui.Dialog().ok(heading, exc.user_message)
        return
    except Exception:
        bg.close()
        log.error('_library_repair: unexpected error during detection:\n{0}'.format(traceback.format_exc()))
        xbmcgui.Dialog().ok(heading, 'Repair failed unexpectedly -- see kodi.log for details.')
        return
    bg.close()

    stale_shows = report['stale_shows']
    stuck_file_ids = report['stuck_files']['file_ids']
    fabricated = report.get('fabricated_watched') or {'episodes': [], 'groups': []}
    if report['total_episodes'] == 0 and not stale_shows and not stuck_file_ids and not fabricated['episodes']:
        library_repair.finish_repair(db_path, report, execute=False)
        message = ADDON.getLocalizedString(32163)
        xbmcgui.Dialog().ok(heading, message)
        ADDON.setSetting('library_repair_last_result', message)
        return

    if report['total_episodes'] == 0:
        confirm_message = ADDON.getLocalizedString(32176)  # neutral -- a stale-show/stuck-file note follows below
    else:
        confirm_message = ADDON.getLocalizedString(32160).format(report['total_episodes'], len(report['groups']))
    if stale_shows:
        confirm_message += ADDON.getLocalizedString(32174).format(len(stale_shows))
    if stuck_file_ids:
        confirm_message += ADDON.getLocalizedString(32178).format(len(stuck_file_ids))
    confirmed = xbmcgui.Dialog().yesno(
        heading, confirm_message,
        yeslabel=ADDON.getLocalizedString(32159),
        nolabel=ADDON.getLocalizedString(32158),
    )

    if not confirmed:
        library_repair.finish_repair(db_path, report, execute=False)
        log.info('_library_repair: user declined -- no changes made')
        return

    bg2, update2 = _progress_bg(heading)
    try:
        result = library_repair.finish_repair(db_path, report, execute=True, progress_callback=update2)
    except Exception:
        # Root-caused live (2026-09-18): this was a bare try/finally with no except -- the ONE
        # call in this whole flow that actually deletes rows had no protection at all, unlike
        # every other library_repair call in this file. An unexpected failure here (run_repair()
        # already catches and reports its OWN known failure modes via result['aborted'] below;
        # this is for anything that slips past that) used to surface as Kodi's raw unhandled-
        # script-error toast, with zero indication of whether the delete ran, partially ran, or
        # didn't, and zero pointer to kodi.log or the backup this module already made before any
        # write. Same friendly-message pattern as every other entry point in this file. (bg2 is
        # closed once, below, by the shared `finally` -- not here too.)
        log.error('_library_repair: unexpected error during the real repair pass:\n{0}'.format(
            traceback.format_exc()))
        xbmcgui.Dialog().ok(
            heading, 'Repair failed unexpectedly -- see kodi.log for details. If anything was '
                     'backed up before the failure, it is under this addon\'s own data folder.')
        return
    finally:
        bg2.close()

    if result['aborted']:
        if result['backup_path']:
            message = ADDON.getLocalizedString(32167).format(result['abort_reason'], result['backup_path'])
        else:
            message = '{0} ({1})'.format(ADDON.getLocalizedString(32166), result['abort_reason'])
        log.warning('_library_repair: aborted mid-run -- {0}'.format(result['abort_reason']))
        xbmcgui.Dialog().ok(heading, message)
        ADDON.setSetting('library_repair_last_result', message)
        return

    if result['deleted_episodes'] > 0:
        message = ADDON.getLocalizedString(32164).format(
            result['deleted_episodes'], len(report['groups']), result['backup_path'])
    else:
        message = ADDON.getLocalizedString(32176)  # no orphan backup was made -- only stale shows/stuck files were fixed
    if result['repaired_stale_shows']:
        message += ADDON.getLocalizedString(32175).format(len(result['repaired_stale_shows']))
    if result.get('deleted_stuck_files'):
        message += ADDON.getLocalizedString(32179).format(result['deleted_stuck_files'])
    watched = result.get('fabricated_watched') or {}
    if result.get('fabricated_watched_error'):
        message += '\n\n' + result['fabricated_watched_error']
    elif watched.get('cleared') or watched.get('skipped'):
        message += ADDON.getLocalizedString(32183).format(watched.get('cleared', 0), watched.get('skipped', 0))
    xbmcgui.Dialog().ok(heading, message)
    ADDON.setSetting('library_repair_last_result', message)

    directories = library_repair.own_source_directories(db_path)
    if directories and xbmcgui.Dialog().yesno(heading, ADDON.getLocalizedString(32165)):
        library_repair.trigger_scan(directories)


def _library_repair_undo():
    """"Undo Last Repair" -- reverses exactly the single most recent "Repair Stuck Episodes"
    run on this device, from the backup it made at the time. See lib/library_repair.py's own
    undo_last_repair() docstring for why this is a scoped re-insert, never a whole-file
    restore."""
    heading = ADDON.getLocalizedString(32155)
    manifest = library_repair.peek_last_manifest()
    watched_manifest = library_repair.peek_fabricated_manifest()
    if not manifest and not watched_manifest:
        xbmcgui.Dialog().ok(heading, 'No repair has been run on this device yet -- there is nothing to undo.')
        return

    if manifest:
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest.get('created_at', 0)))
        confirm_message = ADDON.getLocalizedString(32168).format(len(manifest['episode_ids']), when)
    else:
        confirm_message = ''
    if watched_manifest:
        confirm_message += ADDON.getLocalizedString(32184).format(len(watched_manifest.get('episodes') or []))
    confirmed = xbmcgui.Dialog().yesno(
        heading, confirm_message,
        yeslabel=ADDON.getLocalizedString(32159),
        nolabel=ADDON.getLocalizedString(32158),
    )
    if not confirmed:
        return

    try:
        result = library_repair.run_undo()
    except library_repair.LibraryRepairError as exc:
        log.warning('_library_repair_undo: aborted -- {0} ({1})'.format(exc.reason_code, exc.user_message))
        xbmcgui.Dialog().ok(heading, exc.user_message)
        return
    except Exception:
        log.error('_library_repair_undo: unexpected error:\n{0}'.format(traceback.format_exc()))
        xbmcgui.Dialog().ok(heading, 'Undo failed unexpectedly -- see kodi.log for details.')
        return

    if result['aborted']:
        message = ADDON.getLocalizedString(32170).format(result['abort_reason'])
        log.warning('_library_repair_undo: aborted mid-run -- {0}'.format(result['abort_reason']))
    else:
        message = ADDON.getLocalizedString(32169).format(result['restored_episodes'])
    restored_watched = (result.get('restored_watched') or {}).get('restored', 0)
    if restored_watched:
        message += ADDON.getLocalizedString(32185).format(restored_watched)
    xbmcgui.Dialog().ok(heading, message)
    ADDON.setSetting('library_repair_last_result', message)


if __name__ == '__main__':
    show_menu()
