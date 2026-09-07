# -*- coding: utf-8 -*-
"""script.chronicle.scraper.movie — Background service entry point.

Runs continuously once Kodi starts, doing nothing until a video library scan
finishes. When the "Automatically rebuild NFOs after every library scan"
setting is on, triggers the same delete-then-refresh rebuild the manual
"Rebuild local NFOs from Chronicle" menu action already performs (see
lib/nfo_rebuild.py) -- except now driven by the scan itself, not a person
remembering to click a button.

Why this has to exist as a service, not just the existing manual action:
confirmed directly (nfo_rebuild.py's own module docstring, from the same
investigation) that Kodi checks for a movie's local NFO before ever invoking
this scraper's find/getdetails, with no settings toggle to change that --
so a movie that already has ANY local NFO never reaches Chronicle_Scraper
again on any future scan, automatic or manual, no matter how many times the
library is rescanned. The old manual-only rebuild could fix the library once
when a person remembered to run it, but Chronicle's own data keeps changing
after that point (re-matches, corrected collections, richer metadata) with
no mechanism to ever push those changes back out again. This service closes
that loop: every scan re-runs the same rebuild, so "every file gets touched"
is actually true going forward, not just true the one time someone clicked
the button.

Deliberately opt-in (default OFF, see resources/settings.xml) and NOT
confirmed per-run the way the manual menu action is -- a background service
has no user present to confirm anything. The tradeoff (a long full-library
pass every time even for a scan that only added one new file -- any other
tool's local NFO/movieset-art data is preserved and folded back in rather
than lost, see lib/legacy_nfo.py and collection_sync.py) is spelled out in
the setting's own help text; turning it on is the user's explicit, informed
acceptance of that tradeoff, not a default anyone gets just by installing
the addon.
"""

import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

from lib.logger import Logger
from lib import activity_tracker
from lib import device_registration
from lib import nfo_rebuild
from lib import settings_upgrade
from lib import watch_rating_sync

ADDON = xbmcaddon.Addon()
log   = Logger('service')

# How long with no recorded scraper activity before the corner status is
# considered idle and hidden again. Comfortably longer than the poll
# interval below and than the gap between one item finishing and the next
# one starting during a real library pass, so the indicator doesn't flicker
# on and off between individual items.
_ACTIVITY_IDLE_TIMEOUT_SECONDS = 30
_POLL_INTERVAL_SECONDS = 3
# How often to re-register this device's own remote-control address with Chronicle (see
# lib/device_registration.py) -- catches a DHCP-renewed LAN IP or a webserver setting toggled
# since the last registration, on a long-running Kodi instance that's never been reconnected
# through default.py's own one-time post-pairing registration. Deliberately not more frequent
# than this: it's a handful of local JSON-RPC calls plus one Chronicle POST, cheap but pointless
# to repeat more often than a LAN IP realistically changes.
_DEVICE_REREGISTER_INTERVAL_SECONDS = 6 * 60 * 60

# How long to wait after service startup before the "sync once shortly after Kodi starts" pass
# begins -- gives Kodi's own startup (library scan, add-on init, network) a little room to settle
# first rather than immediately piling on more JSON-RPC/network activity the moment this
# service's own run() starts.
_WATCH_RATING_STARTUP_DELAY_SECONDS = 60
# Checked against sync_interval_minutes (a user setting, read fresh each time -- see the main
# loop below) to decide when the next periodic pass is due.
_WATCH_RATING_CHECK_INTERVAL_SECONDS = 60


class ChronicleMonitor(xbmc.Monitor):
    def __init__(self):
        super(ChronicleMonitor, self).__init__()
        # Guards against a second scan finishing (e.g. video then a fast
        # re-scan) while a rebuild from the first one is still running --
        # nfo_rebuild.run() waits for each movie's own NFO to actually
        # reappear before moving on, so a large library can easily take much
        # longer than the scan itself, and still be going when the next
        # scan-finished fires.
        self._rebuild_lock = threading.Lock()
        # Guards against the periodic timer and a manual "Sync Now" (or an on-load pass still
        # running long) overlapping -- watch_rating_sync.run() has no per-item destructive step
        # the way nfo_rebuild.run() does, so an overlap wouldn't corrupt anything, but it would
        # mean two full passes hammering Chronicle/VideoLibrary at once for no benefit.
        self._watch_rating_lock = threading.Lock()

    def onScanFinished(self, library):
        # Kodi fires this for both 'video' and 'music' library scans --
        # nfo_rebuild.py only knows how to rebuild movie/TV NFOs, so a music
        # scan is a silent no-op rather than an error.
        if library != 'video':
            return

        if not ADDON.getSettingBool('auto_rebuild_on_scan'):
            return

        # Guards against the exact data-loss shape the manual "Rebuild local NFOs" action
        # already warns about (see default.py's _rebuild_nfos and string #32108): nfo_rebuild.py
        # deletes every local NFO/movieset file unconditionally, and only get_details()/
        # get_episode_details() actually rewrite one -- gated on THIS setting. The manual
        # action offers to turn write_nfo on right before running for exactly this reason; this
        # automatic path has no user present to ask, so it must refuse instead of silently
        # deleting a library's local NFOs and never writing them back. Surfaced as a
        # notification, not just a log line -- turning auto_rebuild on while write_nfo is off
        # is a real configuration someone could end up in (e.g. a secondary Kodi instance that
        # intentionally keeps write_nfo off), and a silent no-op scan-after-scan would be
        # exactly the kind of invisible background behaviour this setting pairing exists to
        # avoid.
        if not ADDON.getSettingBool('write_nfo'):
            log.warning('service: auto_rebuild_on_scan is on but write_nfo is off -- skipping this '
                        'rebuild entirely (would delete local NFOs and never rewrite them)')
            xbmcgui.Dialog().notification(
                ADDON.getLocalizedString(32000),
                ADDON.getLocalizedString(32123),
                icon=xbmcgui.NOTIFICATION_WARNING,
                time=15000,
            )
            return

        if not self._rebuild_lock.acquire(False):
            log.info('service: rebuild already in progress -- skipping this scan-finished trigger')
            return

        # Run on a background thread: onScanFinished is a Monitor callback on
        # Kodi's own event-handling thread, and nfo_rebuild.run() can take
        # well over an hour for a large library (one Refresh* JSON-RPC
        # round-trip per movie/show/episode, deliberately paced). Blocking
        # that thread for the whole rebuild would stall Kodi's own event
        # processing for as long as the rebuild runs, not just this addon.
        threading.Thread(target=self._run_rebuild, name='chronicle-nfo-rebuild', daemon=True).start()

    def _run_rebuild(self):
        # Same visibility the manual "Rebuild local NFOs" action already gives the user
        # (default.py's _rebuild_nfos: a heads-up notification, a live DialogProgressBG, and a
        # completion notification) -- this automatic path used to give none of that, only a
        # kodi.log line, which is exactly the "no obvious way to tell this is happening" gap.
        # No confirmation dialog here (unlike the manual action) since there's no one present to
        # confirm; the settings' own help text (#32099) is where that consent already lives.
        #
        # Everything (including the notification/progress-bar setup itself) runs inside this
        # try/finally, not just nfo_rebuild.run() -- the lock acquired in onScanFinished() must
        # release no matter what fails here, or every future scan-finished trigger would see
        # "rebuild already in progress" forever.
        bg = None
        try:
            xbmcgui.Dialog().notification(
                ADDON.getLocalizedString(32000),
                ADDON.getLocalizedString(32122),
                icon=xbmcgui.NOTIFICATION_INFO,
                time=8000,
            )
            bg = xbmcgui.DialogProgressBG()
            bg.create(ADDON.getLocalizedString(32093))
            start_time = time.time()

            def on_progress(index, total, label):
                # See default.py's identical clamp for why -- defensive, not covering a
                # currently-reachable case.
                percent = min(100, int(index * 100 / total)) if total else 0
                message = ADDON.getLocalizedString(32103).format(index + 1, total, label)
                if index > 0 and total:
                    avg_per_item = (time.time() - start_time) / index
                    message += ADDON.getLocalizedString(32121).format(
                        nfo_rebuild.format_duration(avg_per_item * (total - index)))
                bg.update(percent, message=message)

            log.info('service: video library scan finished, auto-rebuild is on -- starting NFO rebuild')
            result = nfo_rebuild.run(progress_callback=on_progress, is_cancelled=self.abortRequested)
            log.info(
                'service: NFO rebuild complete -- {0}/{1} items processed, {2} confirmed rewritten, '
                '{3} nfo deleted, {4} movieset file(s) deleted, {5} refresh error(s)'.format(
                    result['processed'], result['total'], result['nfo_confirmed'], result['nfo_deleted'],
                    result['movieset_deleted'], result['refresh_errors']))

            problem_count = result['unconfirmed_count'] + result['refresh_errors']
            if problem_count == 0:
                message = ADDON.getLocalizedString(32097).format(result['total'])
            else:
                message = ADDON.getLocalizedString(32101).format(
                    result['nfo_confirmed'], result['pending_total'], problem_count)
            xbmcgui.Dialog().notification(
                ADDON.getLocalizedString(32093),
                message,
                icon=xbmcgui.NOTIFICATION_INFO,
                time=10000,
            )
        except Exception as exc:
            log.error('service: NFO rebuild failed: {0}'.format(exc))
            xbmcgui.Dialog().notification(
                ADDON.getLocalizedString(32000),
                ADDON.getLocalizedString(32000) + ': ' + str(exc),
                icon=xbmcgui.NOTIFICATION_ERROR,
                time=15000,
            )
        finally:
            if bg is not None:
                bg.close()
            self._rebuild_lock.release()

    def run_watch_rating_sync(self, trigger_label):
        """Runs one watch_rating_sync.run() pass with the same corner-progress/notification
        visibility the NFO rebuild gets, under _watch_rating_lock so this service's own
        startup pass and periodic timer can't overlap each other. trigger_label (e.g.
        "startup", "scheduled", "manual") is purely for the kodi.log line, so it's obvious
        which trigger kicked off a given pass when reading logs later.

        Deliberately NOT coordinated with default.py's manual "Sync Now" action via any
        cross-process lock (unlike nfo_rebuild.py's rebuild_state, which guards genuinely
        destructive local-file writes) -- watch_rating_sync.run() only ever calls
        VideoLibrary.Set*Details and Chronicle's own API, no local file I/O, so the worst case
        of this service's own pass and a manual one overlapping is some redundant, wasted work,
        never corrupted state.
        """
        if self._rebuild_lock.locked():
            # Purely a screen-real-estate courtesy, not a correctness requirement (the two
            # passes don't conflict with each other the way two rebuild passes would) --
            # without this, an NFO rebuild's own DialogProgressBG and this pass's own could
            # both be on screen at once, competing for the same corner of the UI. The rebuild
            # is the rarer, more deliberate action; deferring the sync costs nothing since it'll
            # simply be checked again at the next _WATCH_RATING_CHECK_INTERVAL_SECONDS tick.
            log.info('service: watch/rating sync ({0}) deferred -- an NFO rebuild is in '
                     'progress'.format(trigger_label))
            return
        if not self._watch_rating_lock.acquire(False):
            log.info('service: watch/rating sync ({0}) skipped -- another pass is already '
                     'running'.format(trigger_label))
            return

        def _do():
            bg = None
            try:
                log.info('service: starting watch/rating sync ({0})'.format(trigger_label))

                def on_progress(index, total, label):
                    nonlocal bg
                    if bg is None:
                        bg = xbmcgui.DialogProgressBG()
                        bg.create(ADDON.getLocalizedString(32134))
                    percent = min(100, int(index * 100 / total)) if total else 0
                    bg.update(percent, message=label)

                result = watch_rating_sync.run(is_cancelled=self.abortRequested, progress_callback=on_progress)
                log.info(
                    'service: watch/rating sync ({0}) complete -- {1} movie(s), {2} show(s), '
                    '{3} episode(s) visited, {4} error(s){5}'.format(
                        trigger_label, result['movies'], result['shows'], result['episodes'],
                        result['errors'], ' (cancelled)' if result['cancelled'] else ''))

                if not result['cancelled']:
                    message = ADDON.getLocalizedString(32135).format(
                        result['movies'], result['shows'], result['episodes'],
                        ADDON.getLocalizedString(32136).format(result['errors']) if result['errors'] else '')
                    xbmcgui.Dialog().notification(
                        ADDON.getLocalizedString(32134),
                        message,
                        icon=xbmcgui.NOTIFICATION_INFO if not result['errors'] else xbmcgui.NOTIFICATION_WARNING,
                        time=8000,
                    )
            except Exception as exc:
                log.error('service: watch/rating sync ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                if bg is not None:
                    bg.close()
                self._watch_rating_lock.release()

        threading.Thread(target=_do, name='chronicle-watch-rating-sync', daemon=True).start()


def run():
    settings_upgrade.ensure_defaults_migrated()

    monitor = ChronicleMonitor()
    log.info('service: Chronicle Scraper background service started')

    # Corner status indicator (DialogProgressBG, the same widget the manual
    # "Rebuild local NFOs" action already uses for its own background phase)
    # covering scraper activity Kodi's own "Scanning library" indicator
    # doesn't: that one only ever tracks Kodi's own directory-walk phase,
    # not the tail of per-item find/getdetails/getartwork calls that follows
    # it and can run far longer -- confirmed directly (2026-08-21) that Kodi
    # refuses "Clean Library" the whole time that tail is still running,
    # with nothing on screen explaining why. activity_tracker.py is the
    # cross-process signal this reads, since each scraper action is its own
    # short-lived process this service doesn't otherwise see into.
    bg = None
    last_shown_count = None

    # Registered once at service startup (covers a fresh Kodi boot picking up a changed LAN
    # IP or webserver setting) and every _DEVICE_REREGISTER_INTERVAL_SECONDS thereafter -- see
    # lib/device_registration.py. Best-effort and silent when there's nothing to register
    # (remote control off, or not yet connected to Chronicle at all).
    threading.Thread(target=device_registration.register, name='chronicle-device-register',
                      daemon=True).start()
    last_device_register = time.time()

    # Watch/rating sync timing state -- see the loop below for how these combine.
    # started_at anchors BOTH the one-time "on Kodi start" delay and the first periodic
    # interval, so a fresh Kodi boot doesn't fire the on-load pass AND immediately also
    # consider a periodic pass "due" the moment that delay elapses. was_enabled tracks the
    # setting's own previous value so an off-to-on transition can reset started_at/
    # startup_sync_done to behave like a fresh "startup", not fire mislabeled as "startup"
    # using a start time from potentially hours or days earlier (confirmed live 2026-09-06:
    # without this, re-enabling after being off for a while fired a pass logged as "startup"
    # long after the actual service start, and permanently used up that Kodi-restart-only
    # opportunity even if "sync on start" happened to be off at that exact moment).
    service_started_at = time.time()
    startup_sync_done = False
    last_watch_rating_sync = service_started_at
    last_watch_rating_check = 0.0  # forces the very first loop iteration to check
    watch_rating_was_enabled = ADDON.getSettingBool('sync_watch_ratings_enabled')

    # Standard Kodi service idle loop: sleep in short increments so
    # abortRequested() (set on Kodi shutdown) is noticed promptly instead of
    # blocking in one long sleep.
    while not monitor.abortRequested():
        if time.time() - last_device_register >= _DEVICE_REREGISTER_INTERVAL_SECONDS:
            threading.Thread(target=device_registration.register, name='chronicle-device-register',
                              daemon=True).start()
            last_device_register = time.time()

        # Checked at most once every _WATCH_RATING_CHECK_INTERVAL_SECONDS, not every single
        # ~3s idle-loop tick -- reading three settings and doing this arithmetic is cheap, but
        # there's no reason to do it 20x more often than the coarsest granularity (30-minute
        # steps) the interval setting itself offers.
        now = time.time()
        if now - last_watch_rating_check >= _WATCH_RATING_CHECK_INTERVAL_SECONDS:
            last_watch_rating_check = now
            watch_rating_enabled = ADDON.getSettingBool('sync_watch_ratings_enabled')

            if watch_rating_enabled and not watch_rating_was_enabled:
                log.info('service: watch/rating sync re-enabled -- treating this as a fresh start')
                service_started_at = now
                startup_sync_done = False
            watch_rating_was_enabled = watch_rating_enabled

            if watch_rating_enabled:
                # Two independent checks, not if/else -- once the startup phase has passed,
                # periodic checking must run on every tick from then on, not just "instead of"
                # the one-time startup check on whichever single tick first noticed it.
                if not startup_sync_done and now - service_started_at >= _WATCH_RATING_STARTUP_DELAY_SECONDS:
                    startup_sync_done = True
                    if ADDON.getSettingBool('sync_on_kodi_start'):
                        last_watch_rating_sync = now
                        monitor.run_watch_rating_sync('startup')
                if startup_sync_done:
                    interval_seconds = max(30, ADDON.getSettingInt('sync_interval_minutes')) * 60
                    if now - last_watch_rating_sync >= interval_seconds:
                        last_watch_rating_sync = now
                        monitor.run_watch_rating_sync('scheduled')
            else:
                # Keeps the anchor from drifting into the past while the master switch is off,
                # so re-enabling it doesn't immediately fire a sync that "should have" run
                # during however long it was off.
                last_watch_rating_sync = now

        activity = activity_tracker.read_activity()
        is_active = activity is not None and \
            (time.time() - activity.get('timestamp', 0)) < _ACTIVITY_IDLE_TIMEOUT_SECONDS

        # Suppress the corner status while Kodi's own "Scanning library"
        # indicator is still up -- two progress indicators competing for
        # attention during the walk phase is confusing to look at. This is
        # a display-only gate: activity_tracker keeps recording normally
        # underneath (mark_active() doesn't check this), so the count isn't
        # paused or lost, only hidden -- the moment Kodi's own indicator
        # goes away, this one picks straight back up showing whatever total
        # already accumulated during the walk, not starting over from zero.
        kodi_scanning = xbmc.getCondVisibility('Library.IsScanning')
        show_now = is_active and not kodi_scanning

        if show_now:
            count = activity.get('count', 0)
            label = activity.get('last_label') or ''
            suffix = ' -- {0}'.format(label) if label else ''
            message = ADDON.getLocalizedString(32107).format(count, suffix)
            if bg is None:
                bg = xbmcgui.DialogProgressBG()
                bg.create(ADDON.getLocalizedString(32000), message)
                log.info('service: scraper activity detected -- showing corner status')
            elif count != last_shown_count:
                bg.update(0, message=message)
            last_shown_count = count
        elif bg is not None:
            bg.close()
            bg = None
            last_shown_count = None
            log.info('service: {0} -- hiding corner status'.format(
                     'Kodi library scan still running' if kodi_scanning else 'scraper activity gone idle'))
            if not is_active:
                activity_tracker.reset()

        if monitor.waitForAbort(_POLL_INTERVAL_SECONDS):
            break

    if bg is not None:
        bg.close()
    log.info('service: Chronicle Scraper background service stopped')


if __name__ == '__main__':
    run()
