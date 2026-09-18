# -*- coding: utf-8 -*-
"""script.chronicle.scraper.movie — Background service entry point.

Runs continuously once Kodi starts: keeps this device's remote-control
address registered with Chronicle, periodically syncs watch history/ratings
and collection artwork back to Chronicle, and checks Chronicle's "new content
available" signal to trigger a library scan when needed. See
lib/watch_rating_sync.py, lib/collection_art_sync.py, lib/kodi_scan_signal.py
and lib/device_registration.py for what each of those actually does.

Local NFO writing/rebuilding (write_nfo, auto_rebuild_on_scan) was a
separate, removed feature -- see git history for lib/nfo_rebuild.py and this
file's own history if you need the old rationale. Removed per-user direction
(2026-09-13) after it was suspected of interfering with TV show scanning
reliability during a heavy rescan session; Chronicle's own API is the
source of truth Kodi's scraper reads from directly, so a local NFO was never
required for this addon to work.
"""

import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

from lib.logger import Logger
from lib import activity_tracker
from lib import collection_art_sync
from lib import device_registration
from lib import kodi_scan_signal
from lib import watch_rating_sync
from lib.chronicle_client import ChronicleClient

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

# Chronicle's "new content available" flag (lib/kodi_scan_signal.py) is always checked once
# after Kodi starts, regardless of the scan_signal_enabled setting -- most imports land via
# Chronicle's own nightly schedule, so the next Kodi startup already catches them with no
# recurring poll needed. Periodic re-checking while Kodi keeps running is opt-in (default off,
# see settings.xml) at a user-configurable interval (scan_signal_interval_minutes, default 120)
# -- a triggered scan cascades into every OTHER addon's own onScanFinished handler too (confirmed
# live: Chronicle_Scrobbler's VideoLibrary.Clean, SIMKL's own full sync pass), so this defaults
# to conservative rather than "as fast as possible."
_SCAN_SIGNAL_STARTUP_DELAY_SECONDS = 60

# Short delay before the startup-scan followup check (lib/kodi_scan_signal.py's
# run_startup_scan_followup) -- just enough that Kodi's own JSON-RPC server is definitely up;
# the check itself is a single cheap local settings read, and the followup scan it may launch
# already does its own bounded wait for the network/share to actually be reachable.
_STARTUP_SCAN_FOLLOWUP_DELAY_SECONDS = 10

# How often to renew the server-side "a Kodi device is actively scanning" flag (see
# lib/chronicle_client.py's report_scan_active()) while Kodi's own library scan OR this addon's
# own scraper-activity tail is still running. Comfortably shorter than that flag's own 3-minute
# server-side TTL so a renewal is never late enough for the flag to lapse mid-scan, but far
# longer than the 3s idle-loop tick so this isn't a Chronicle POST on every single tick.
_SCAN_ACTIVE_HEARTBEAT_SECONDS = 60


class ChronicleMonitor(xbmc.Monitor):
    def __init__(self):
        super(ChronicleMonitor, self).__init__()
        # Guards against the periodic timer and a manual "Sync Now" (or an on-load pass still
        # running long) overlapping -- watch_rating_sync.run() has no per-item destructive step,
        # so an overlap wouldn't corrupt anything, but it would mean two full passes hammering
        # Chronicle/VideoLibrary at once for no benefit.
        self._watch_rating_lock = threading.Lock()
        # Guards against two check_and_scan() calls overlapping if one is still waiting on a
        # slow/hanging network call when the next poll interval comes due -- cheap insurance,
        # not a response to any observed failure.
        self._scan_signal_lock = threading.Lock()
        # Guards against this service's own periodic timer and a manual "Sync Now" (or an
        # on-load pass still running long) overlapping -- same reasoning as
        # _watch_rating_lock, this task has no destructive per-item step either.
        self._collection_art_lock = threading.Lock()

    def _should_defer_for_active_scan(self, task_label):
        """True if either Kodi's own library scan (Library.IsScanning) or EITHER addon's own
        scraper activity tail (activity_tracker -- shared across both addon packages, see that
        module's own doc) is recent enough to still be considered "in progress". Per-user
        decision (2026-09-12): the movie and TV scrapers' own background maintenance tasks must
        never run at the same time as an active scan/scrape, the same way Chronicle's own
        server-side NfoGenerationService now pauses itself for the same reason (see
        IKodiDeviceService.IsScanActiveAsync's server-side doc) -- both are contending for the
        same limited local (SMB/CPU) or server capacity an active scan needs most. Logged and
        returned as a simple bool (not raised) so callers can just `if deferred: return`.
        """
        kodi_scanning = xbmc.getCondVisibility('Library.IsScanning')
        scraper_active = activity_tracker.is_recently_active(_ACTIVITY_IDLE_TIMEOUT_SECONDS)
        if kodi_scanning or scraper_active:
            log.info('service: {0} deferred -- {1} still in progress'.format(
                     task_label, 'a Kodi library scan' if kodi_scanning else 'scraper activity'))
            return True
        return False


    def run_watch_rating_sync(self, trigger_label):
        """Runs one watch_rating_sync.run() pass, under _watch_rating_lock so this service's own
        startup pass and periodic timer can't overlap each other. trigger_label (e.g.
        "startup", "scheduled", "manual") is purely for the kodi.log line, so it's obvious
        which trigger kicked off a given pass when reading logs later.

        Deliberately NOT coordinated with default.py's manual "Sync Now" action via any
        cross-process lock -- watch_rating_sync.run() only ever calls VideoLibrary.Set*Details
        and Chronicle's own API, no local file I/O, so the worst case of this service's own pass
        and a manual one overlapping is some redundant, wasted work, never corrupted state.
        """
        if self._should_defer_for_active_scan('watch/rating sync ({0})'.format(trigger_label)):
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

    def run_collection_art_sync(self, trigger_label):
        """Runs one collection_art_sync.run() pass -- see that module's own doc for why this
        exists as its own periodic task. Same locking/deferral shape as run_watch_rating_sync
        above: deferred behind an active scan/scrape (see _should_defer_for_active_scan),
        guarded by its own lock so this service's startup pass and periodic timer (or a manual
        "Sync Now") can't overlap each other.
        """
        if self._should_defer_for_active_scan('collection art sync ({0})'.format(trigger_label)):
            return
        if not self._collection_art_lock.acquire(False):
            log.info('service: collection art sync ({0}) skipped -- another pass is already '
                     'running'.format(trigger_label))
            return

        def _do():
            bg = None
            try:
                log.info('service: starting collection art sync ({0})'.format(trigger_label))

                def on_progress(index, total, label):
                    nonlocal bg
                    if bg is None:
                        bg = xbmcgui.DialogProgressBG()
                        bg.create(ADDON.getLocalizedString(32143))
                    percent = min(100, int(index * 100 / total)) if total else 0
                    bg.update(percent, message=label)

                result = collection_art_sync.run(is_cancelled=self.abortRequested, progress_callback=on_progress)
                log.info(
                    'service: collection art sync ({0}) complete -- {1} collection(s) visited, '
                    '{2} error(s){3}'.format(
                        trigger_label, result['collections'], result['errors'],
                        ' (cancelled)' if result['cancelled'] else ''))

                if not result['cancelled']:
                    message = ADDON.getLocalizedString(32147).format(
                        result['collections'],
                        ADDON.getLocalizedString(32136).format(result['errors']) if result['errors'] else '')
                    xbmcgui.Dialog().notification(
                        ADDON.getLocalizedString(32143),
                        message,
                        icon=xbmcgui.NOTIFICATION_INFO if not result['errors'] else xbmcgui.NOTIFICATION_WARNING,
                        time=8000,
                    )
            except Exception as exc:
                log.error('service: collection art sync ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                if bg is not None:
                    bg.close()
                self._collection_art_lock.release()

        threading.Thread(target=_do, name='chronicle-collection-art-sync', daemon=True).start()

    def run_scan_signal_check(self):
        """Runs one kodi_scan_signal.check_and_scan() pass on a background thread -- see that
        module's own doc. Skipped (not queued) if a check is already in flight; the next poll
        interval will simply try again."""
        if not self._scan_signal_lock.acquire(False):
            log.info('service: scan-signal check skipped -- another check is already running')
            return
        try:
            threading.Thread(
                target=lambda: self._run_scan_signal_check_locked(),
                name='chronicle-scan-signal-check', daemon=True,
            ).start()
        except Exception:
            self._scan_signal_lock.release()
            raise

    def _run_scan_signal_check_locked(self):
        try:
            kodi_scan_signal.check_and_scan()
        except Exception as exc:
            log.error('service: scan-signal check failed: {0}'.format(exc))
        finally:
            self._scan_signal_lock.release()

    def run_startup_scan_followup(self, service_started_at):
        threading.Thread(
            target=self._run_startup_scan_followup, args=(service_started_at,),
            name='chronicle-startup-scan-followup', daemon=True,
        ).start()

    def _run_startup_scan_followup(self, service_started_at):
        try:
            kodi_scan_signal.run_startup_scan_followup(service_started_at, is_aborted=self.abortRequested)
        except Exception as exc:
            log.error('service: startup-scan followup failed: {0}'.format(exc))


def run():
    monitor = ChronicleMonitor()
    log.info('service: Chronicle Scraper background service started')

    # Corner status indicator (DialogProgressBG) covering scraper activity
    # Kodi's own "Scanning library" indicator doesn't: that one only ever
    # tracks Kodi's own directory-walk phase,
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

    # Collection art sync timing state -- own anchor, same reasoning as watch/rating sync's own
    # started_at/was_enabled pair above, kept fully independent since the two features have
    # nothing to do with each other.
    collection_art_started_at = time.time()
    collection_art_startup_done = False
    last_collection_art_sync = collection_art_started_at
    last_collection_art_check = 0.0  # forces the very first loop iteration to check
    collection_art_was_enabled = ADDON.getSettingBool('sync_collections_enabled')

    # Own anchor, deliberately not shared with service_started_at above -- that one gets reset
    # on a watch-rating off-to-on transition, which has nothing to do with this feature.
    scan_signal_service_started_at = time.time()
    scan_signal_startup_done = False
    last_scan_signal_check = 0.0  # only consulted once scan_signal_enabled is on
    startup_followup_done = False

    last_scan_active_heartbeat = 0.0  # forces an immediate first heartbeat once scanning starts

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

        # Same shape as the watch/rating sync block above, fully independent state -- see
        # collection_art_started_at's own doc for why.
        if now - last_collection_art_check >= _WATCH_RATING_CHECK_INTERVAL_SECONDS:
            last_collection_art_check = now
            collection_art_enabled = ADDON.getSettingBool('sync_collections_enabled')

            if collection_art_enabled and not collection_art_was_enabled:
                log.info('service: collection art sync re-enabled -- treating this as a fresh start')
                collection_art_started_at = now
                collection_art_startup_done = False
            collection_art_was_enabled = collection_art_enabled

            if collection_art_enabled:
                if not collection_art_startup_done and \
                        now - collection_art_started_at >= _WATCH_RATING_STARTUP_DELAY_SECONDS:
                    collection_art_startup_done = True
                    if ADDON.getSettingBool('sync_collections_on_kodi_start'):
                        last_collection_art_sync = now
                        monitor.run_collection_art_sync('startup')
                if collection_art_startup_done:
                    interval_seconds = max(30, ADDON.getSettingInt('sync_collections_interval_minutes')) * 60
                    if now - last_collection_art_sync >= interval_seconds:
                        last_collection_art_sync = now
                        monitor.run_collection_art_sync('scheduled')
            else:
                last_collection_art_sync = now

        # Startup check: always runs exactly once, regardless of scan_signal_enabled -- see
        # this module's own top-of-file doc for why a recurring poll isn't the default.
        if not scan_signal_startup_done and \
                now - scan_signal_service_started_at >= _SCAN_SIGNAL_STARTUP_DELAY_SECONDS:
            scan_signal_startup_done = True
            monitor.run_scan_signal_check()

        # Independent of scan_signal_enabled above -- see lib/kodi_scan_signal.py's own doc
        # (Startup-scan followup). Always runs exactly once; the function itself is a no-op if
        # Kodi's native "Update library on startup" setting turns out to be off. Anchored to
        # scan_signal_service_started_at (fixed for this service's lifetime), not the
        # watch-rating service_started_at above (which gets reset on an off-to-on transition).
        if not startup_followup_done and \
                now - scan_signal_service_started_at >= _STARTUP_SCAN_FOLLOWUP_DELAY_SECONDS:
            startup_followup_done = True
            monitor.run_startup_scan_followup(scan_signal_service_started_at)

        if ADDON.getSettingBool('scan_signal_enabled'):
            interval_seconds = max(30, ADDON.getSettingInt('scan_signal_interval_minutes')) * 60
            if now - last_scan_signal_check >= interval_seconds:
                last_scan_signal_check = now
                monitor.run_scan_signal_check()

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

        # Covers BOTH content types, even for a user with only this addon installed: the tail
        # this addon's own scraper actions leave behind after Kodi's own directory-walk phase
        # ends (is_active, computed above from the shared activity_tracker signal file) is
        # exactly where NfoGenerationService's own scheduled sweep used to contend with an
        # active scan for the same server/IO capacity -- see report_scan_active()'s own doc.
        if (kodi_scanning or is_active) and \
                time.time() - last_scan_active_heartbeat >= _SCAN_ACTIVE_HEARTBEAT_SECONDS:
            last_scan_active_heartbeat = time.time()
            threading.Thread(
                target=ChronicleClient().report_scan_active,
                name='chronicle-scan-active-heartbeat', daemon=True,
            ).start()

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
