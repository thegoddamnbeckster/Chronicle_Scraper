# -*- coding: utf-8 -*-
"""script.chronicle.scraper.movie — Background service entry point.

Runs continuously once Kodi starts: registers this device with Chronicle, periodically
syncs watch history/ratings and collection artwork between Chronicle and Kodi, and after each
Kodi library scan runs one pass of both. It never starts a library scan -- Kodi pulls from
Chronicle when IT scans. See lib/watch_rating_sync.py, lib/collection_art_sync.py,
lib/kodi_scan_signal.py and lib/device_registration.py for what each of those actually does.

Chronicle's own API is the source of truth Kodi's scraper reads from directly.
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
from lib import install_marker
from lib import pass_progress
from lib import recent_play
from lib import scan_progress
from lib import status_display
from lib import sync_baton
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
# Default for the "seconds to wait after Kodi starts" setting (startup_delay_seconds): how long after
# service start before any start-up work begins (the watch/rating and collection art start-up passes and
# the refresh check), so Kodi's own start-up -- add-on init, network, mounting the drives -- gets room
# first. Read fresh each time, so changing it needs no restart.
_DEFAULT_STARTUP_DELAY_SECONDS = 10


def _startup_delay_seconds():
    try:
        return max(0, xbmcaddon.Addon().getSettingInt('startup_delay_seconds'))
    except Exception:
        return _DEFAULT_STARTUP_DELAY_SECONDS


# Checked against sync_interval_minutes (a user setting, read fresh each time -- see the main
# loop below) to decide when the next periodic pass is due.
_WATCH_RATING_CHECK_INTERVAL_SECONDS = 60
# How soon a periodic pass that was DEFERRED (a scan or scraper activity was in progress when its turn came) tries
# again. The pass used to restart its full interval the moment it was merely attempted, so on a device that scans
# often (every scan, rebuild and post-scan check counts as activity) every attempt could land in a busy moment and
# the pass would go many hours without running -- a rating or watched status set elsewhere reached this device only
# when a tick happened to fall in a quiet gap. (Reported 2026-10-11: a movie rated 6 downstairs was still unrated
# upstairs after several scans; its last full pass had been 38 hours earlier.)
_DEFERRED_RETRY_SECONDS = 5 * 60


def _anchor_after_attempt(now, interval_seconds, started):
    """The value to store as a periodic task's "last ran" time after trying it. A pass that started (or that another
    pass is already covering) restarts the full interval from now; a pass that was deferred behind a scan is due
    again in _DEFERRED_RETRY_SECONDS, not a whole interval later."""
    if started:
        return now
    return now - interval_seconds + min(_DEFERRED_RETRY_SECONDS, interval_seconds)

# How often to renew the server-side "a Kodi device is actively scanning" flag (see
# lib/chronicle_client.py's report_scan_active()) while Kodi's own library scan OR this addon's
# own scraper-activity tail is still running. Comfortably shorter than that flag's own 3-minute
# server-side TTL so a renewal is never late enough for the flag to lapse mid-scan, but far
# longer than the 3s idle-loop tick so this isn't a Chronicle POST on every single tick.
_SCAN_ACTIVE_HEARTBEAT_SECONDS = 60

# How often to log "where is the scan" while Kodi scans the video library (lib/scan_progress.py).
_SCAN_PROGRESS_INTERVAL_SECONDS = 60

# onScanFinished fires the instant a scan ends, which is exactly when this addon's own
# just-finished per-item scraper activity is still inside _ACTIVITY_IDLE_TIMEOUT_SECONDS -- see
# run_post_scan_pass's own doc. Bounded so a genuinely stuck/long-running scrape can't wait
# forever; generous since a real scan/scrape tail can legitimately run long on a big library.
_POST_SCAN_DEFER_WAIT_TIMEOUT_SECONDS = 20 * 60
_POST_SCAN_DEFER_POLL_SECONDS = 10

# One native/manual scan can legitimately fire onScanFinished several times in quick succession as
# different addons' own scans each complete in turn. This keeps a completed post-scan pass from
# re-running on every one of those, not just the first.
_POST_SCAN_MIN_SECONDS_BETWEEN_RUNS = 5 * 60


class ChronicleMonitor(xbmc.Monitor):
    def __init__(self):
        super(ChronicleMonitor, self).__init__()
        # Guards against the periodic timer and a manual "Sync Now" (or an on-load pass still
        # running long) overlapping -- watch_rating_sync.run() has no per-item destructive step,
        # so an overlap wouldn't corrupt anything, but it would mean two full passes hammering
        # Chronicle/VideoLibrary at once for no benefit.
        self._watch_rating_lock = threading.Lock()
        # Guards against this service's own periodic timer and a manual "Sync Now" (or an
        # on-load pass still running long) overlapping -- same reasoning as
        # _watch_rating_lock, this task has no destructive per-item step either.
        self._collection_art_lock = threading.Lock()
        # Guards against onScanFinished firing again (a second scan completing) while a
        # previous post-scan pass is still running -- about not wasting duplicate work, not
        # data safety.
        self._post_scan_lock = threading.Lock()
        # See _POST_SCAN_MIN_SECONDS_BETWEEN_RUNS's own doc -- 0.0 so the very first
        # onScanFinished this session always runs regardless of when the service itself started.
        self._post_scan_last_completed_at = 0.0
        # True between Kodi's onCleanStarted and onCleanFinished, so the corner status can say so.
        self.cleaning = False
        self._defer_notice_shown = False

    def _should_defer_for_active_scan(self, task_label):
        """True if either Kodi's own library scan (Library.IsScanning) or EITHER addon's own
        scraper activity tail (activity_tracker -- shared across both addon packages, see that
        module's own doc) is recent enough to still be considered "in progress". Per-user
        decision (2026-09-12): the movie and TV scrapers' own background maintenance tasks must
        never run at the same time as an active scan/scrape -- both contend for the same
        limited local (SMB/CPU) or server capacity an active scan needs most. Logged and
        returned as a simple bool (not raised) so callers can just `if deferred: return`.
        """
        kodi_scanning = xbmc.getCondVisibility('Library.IsScanning')
        scraper_active = activity_tracker.is_recently_active(_ACTIVITY_IDLE_TIMEOUT_SECONDS)
        if kodi_scanning or scraper_active:
            log.info('service: {0} deferred -- {1} still in progress'.format(
                     task_label, 'a Kodi library scan' if kodi_scanning else 'scraper activity'))
            # Say so on screen ONCE per busy period (re-armed when the library goes idle): a task that
            # quietly does nothing looks broken, but repeating it for hours of scan is just noise.
            if not self._defer_notice_shown:
                self._defer_notice_shown = True
                xbmcgui.Dialog().notification(
                    ADDON.getLocalizedString(32000),
                    ADDON.getLocalizedString(32153).format(task_label),
                    icon=xbmcgui.NOTIFICATION_INFO, time=6000)
            return True
        self._defer_notice_shown = False
        return False


    def _hold_baton(self, what):
        """The turn-taking with every other background sync on this Kodi (the TV add-on's included): see lib/sync_baton.py. Re-entrant
        within a thread, so a thread that holds it across several passes keeps its place."""
        return sync_baton.hold(sync_baton.owner_id(ADDON.getAddonInfo('id')), what, log, is_aborted=self.abortRequested)

    def _watch_rating_pass(self, trigger_label, is_cancelled=None):
        with self._hold_baton('the watch/rating sync') as got:
            if got:
                self._watch_rating_pass_held(trigger_label, is_cancelled)

    def _collection_art_pass(self, trigger_label, is_cancelled=None):
        with self._hold_baton('the collection art sync') as got:
            if got:
                self._collection_art_pass_held(trigger_label, is_cancelled)

    def _watch_rating_pass_held(self, trigger_label, is_cancelled=None):
        """One watch_rating_sync.run() pass with its on-screen progress and notification. The
        caller holds _watch_rating_lock."""
        # Opened the moment the pass starts and kept up to date through it (see lib/pass_progress.py):
        # this pass writes into the library, which makes Kodi's screens refresh, and that must never
        # happen without something on screen saying why.
        progress = pass_progress.PassProgress(
            ADDON.getLocalizedString(32134), ADDON.getLocalizedString(32157), log)
        try:
            log.info('service: starting watch/rating sync ({0})'.format(trigger_label))
            progress.start()

            result = watch_rating_sync.run(is_cancelled=is_cancelled or self.abortRequested,
                                           progress_callback=progress.update)
            log.info(
                'service: watch/rating sync ({0}) complete -- {1} movie(s), {2} show(s), '
                '{3} episode(s) visited; changed {4} movie(s), {5} show(s), {6} episode(s); '
                '{7} error(s){8}'.format(
                    trigger_label, result['movies'], result['shows'], result['episodes'],
                    result['movies_changed'], result['shows_changed'], result['episodes_changed'],
                    result['errors'], ' (cancelled)' if result['cancelled'] else ''))

            # Only what changed is worth a notification; a pass that found nothing to do stays silent.
            message = None if result['cancelled'] else watch_rating_sync.describe_result(
                result, ADDON.getLocalizedString)
            if message:
                xbmcgui.Dialog().notification(
                    ADDON.getLocalizedString(32134),
                    message,
                    icon=xbmcgui.NOTIFICATION_INFO if not result['errors'] else xbmcgui.NOTIFICATION_WARNING,
                    time=8000,
                )
        finally:
            progress.finish()

    def _collection_art_pass_held(self, trigger_label, is_cancelled=None):
        """One collection_art_sync.run() pass. The caller holds _collection_art_lock. Nothing is
        shown unless Chronicle had a collection to compare, and only a change is announced."""
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

            result = collection_art_sync.run(is_cancelled=is_cancelled or self.abortRequested,
                                             progress_callback=on_progress)
            log.info(
                'service: collection art sync ({0}) complete -- {1} compared, {2} changed, '
                '{3} error(s){4}'.format(
                    trigger_label, result['collections'], result['changed'], result['errors'],
                    ' (cancelled)' if result['cancelled'] else ''))

            message = None if result['cancelled'] else collection_art_sync.describe_result(
                result, ADDON.getLocalizedString)
            if message:
                xbmcgui.Dialog().notification(
                    ADDON.getLocalizedString(32143),
                    message,
                    icon=xbmcgui.NOTIFICATION_INFO if not result['errors'] else xbmcgui.NOTIFICATION_WARNING,
                    time=8000,
                )
        finally:
            if bg is not None:
                bg.close()

    def run_watch_rating_sync(self, trigger_label):
        """Runs one watch_rating_sync.run() pass, under _watch_rating_lock so this service's own
        startup pass and periodic timer can't overlap each other. trigger_label (e.g.
        "startup", "scheduled", "manual") is purely for the kodi.log line, so it's obvious
        which trigger kicked off a given pass when reading logs later.

        Deliberately NOT coordinated with default.py's manual "Sync Now" action via any
        cross-process lock -- watch_rating_sync.run() only ever calls VideoLibrary.Set*Details
        and Chronicle's own API, no local file I/O, so the worst case of this service's own pass
        and a manual one overlapping is some redundant, wasted work, never corrupted state.

        Returns False only when the pass was DEFERRED behind a scan/scrape (the caller should try again soon);
        True when it started or another pass is already running.
        """
        if self._should_defer_for_active_scan('watch/rating sync ({0})'.format(trigger_label)):
            return False
        if not self._watch_rating_lock.acquire(False):
            log.info('service: watch/rating sync ({0}) skipped -- another pass is already '
                     'running'.format(trigger_label))
            return True

        def _do():
            try:
                self._watch_rating_pass(trigger_label)
            except Exception as exc:
                log.error('service: watch/rating sync ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                self._watch_rating_lock.release()

        threading.Thread(target=_do, name='chronicle-watch-rating-sync', daemon=True).start()
        return True

    def run_collection_art_sync(self, trigger_label):
        """Runs one collection_art_sync.run() pass -- see that module's own doc. Same locking/
        deferral shape as run_watch_rating_sync above: deferred behind an active scan/scrape
        (see _should_defer_for_active_scan), guarded by its own lock so this service's startup
        pass and periodic timer (or a manual "Sync Now") can't overlap each other.
        """
        if self._should_defer_for_active_scan('collection art sync ({0})'.format(trigger_label)):
            return False
        if not self._collection_art_lock.acquire(False):
            log.info('service: collection art sync ({0}) skipped -- another pass is already '
                     'running'.format(trigger_label))
            return True

        def _do():
            try:
                self._collection_art_pass(trigger_label)
            except Exception as exc:
                log.error('service: collection art sync ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                self._collection_art_lock.release()

        threading.Thread(target=_do, name='chronicle-collection-art-sync', daemon=True).start()
        return True

    def onCleanStarted(self, library):
        if library == 'video':
            self.cleaning = True

    def onCleanFinished(self, library):
        if library == 'video':
            self.cleaning = False

    def onScanFinished(self, library):
        """Kodi's own native callback, fired the same way regardless of what started the scan --
        its automatic startup scan, the user's manual "Update Library", or another add-on's. One
        pass follows it, gated by the "after each scan" setting (default on): the watch/rating sync
        (which also corrects any movie text/art that disagrees with Chronicle, and finishes the work
        a quick scan deferred) and then the collection art sync."""
        if library != 'video':
            return
        self.run_post_scan_pass('scan finished')

    def run_post_scan_pass(self, trigger_label):
        """The work that follows a library scan, as part of it: waits until the scan and this
        add-on's own scraper tail are over, then runs the watch/rating sync and the collection art
        sync one after the other. Each touches Kodi only where it differs from Chronicle. A pass
        already running (the periodic one, say) is not run twice; a second onScanFinished within
        _POST_SCAN_MIN_SECONDS_BETWEEN_RUNS of a finished pass is skipped."""
        if not xbmcaddon.Addon().getSettingBool('full_sync_check_enabled'):
            return
        since_last = time.time() - self._post_scan_last_completed_at
        if since_last < _POST_SCAN_MIN_SECONDS_BETWEEN_RUNS:
            log.info('service: post-scan pass ({0}) skipped -- a pass completed {1:.0f}s ago, '
                     'inside the {2}s cooldown (one scan can fire onScanFinished several times)'.format(
                     trigger_label, since_last, _POST_SCAN_MIN_SECONDS_BETWEEN_RUNS))
            return
        if not self._post_scan_lock.acquire(False):
            log.info('service: post-scan pass ({0}) skipped -- another pass is already '
                     'running'.format(trigger_label))
            return

        def _busy():
            return self.abortRequested() or self._should_defer_for_active_scan(
                'post-scan pass ({0})'.format(trigger_label))

        def _wait_until_quiet():
            """True once nothing else is scanning or scraping; False if Kodi is shutting down or the
            wait ran out. Bounded, and checked INSIDE the thread rather than once before spawning it:
            onScanFinished fires the instant Kodi's own scan ends, which is exactly when this addon's
            own last per-item scraper call is still within its idle window, and nothing else
            re-triggers this. Needed again before the second pass, since the first one can itself
            start re-scrapes (cast/collection differences) that keep the scraper busy."""
            deadline = time.time() + _POST_SCAN_DEFER_WAIT_TIMEOUT_SECONDS
            while self._should_defer_for_active_scan('post-scan pass ({0})'.format(trigger_label)):
                if self.abortRequested() or time.time() >= deadline:
                    log.info('service: post-scan pass ({0}) gave up waiting for other '
                             'activity to clear'.format(trigger_label))
                    return False
                if self.waitForAbort(_POST_SCAN_DEFER_POLL_SECONDS):
                    return False
            return True

        def _do():
            try:
                if not _wait_until_quiet():
                    return
                # One task, taken in turn with the other add-ons' syncs and kept for both passes below so nothing slips between them.
                with self._hold_baton('the post-scan sync') as got:
                    if got:
                        _passes()
            except Exception as exc:
                log.error('service: post-scan pass ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                self._post_scan_lock.release()

        def _passes():
            # Having waited for its turn, the library may have gone busy again.
            if not _wait_until_quiet():
                return
            # Each pass stops if a fresh scan/scrape starts mid-way (checked per item), so it
            # never competes with new scan activity for the same share and CPU.
            if self._watch_rating_lock.acquire(False):
                try:
                    self._watch_rating_pass(trigger_label, is_cancelled=_busy)
                finally:
                    self._watch_rating_lock.release()
            else:
                log.info('service: post-scan watch/rating sync skipped -- another pass is running')

            if not _wait_until_quiet():
                return
            if self._collection_art_lock.acquire(False):
                try:
                    self._collection_art_pass(trigger_label, is_cancelled=_busy)
                finally:
                    self._collection_art_lock.release()
            else:
                log.info('service: post-scan collection art sync skipped -- another pass is running')
            self._post_scan_last_completed_at = time.time()

        threading.Thread(target=_do, name='chronicle-post-scan-pass', daemon=True).start()

    def run_startup_refresh(self):
        """Runs this add-on's own once-per-startup refresh check on a background thread -- see
        kodi_scan_signal.run_startup_refresh's own doc. Not a recurring poll."""
        threading.Thread(
            target=kodi_scan_signal.run_startup_refresh, args=(['movie'],),
            kwargs={'is_aborted': self.abortRequested},
            name='chronicle-startup-refresh', daemon=True,
        ).start()


def run():
    monitor = ChronicleMonitor()
    # Records every file this Kodi plays, so a bookmark made by a partial rewatch is never mistaken for a stale one.
    playback_recorder = recent_play.Recorder()  # noqa: F841 -- must stay referenced for its callbacks to fire
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

    # Registered once per service start -- see lib/device_registration.py. Best-effort and silent
    # when Chronicle is not connected yet.
    threading.Thread(target=device_registration.register, name='chronicle-device-register',
                      daemon=True).start()

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
    watch_rating_was_enabled = xbmcaddon.Addon().getSettingBool('sync_watch_ratings_enabled')

    # Collection art sync timing state -- own anchor, same reasoning as watch/rating sync's own
    # started_at/was_enabled pair above, kept fully independent since the two features have
    # nothing to do with each other.
    collection_art_started_at = time.time()
    collection_art_startup_done = False
    last_collection_art_sync = collection_art_started_at
    last_collection_art_check = 0.0  # forces the very first loop iteration to check
    collection_art_was_enabled = xbmcaddon.Addon().getSettingBool('sync_collections_enabled')

    # Own anchor, deliberately not shared with service_started_at above -- that one gets reset
    # on a watch-rating off-to-on transition, which has nothing to do with this feature.
    refresh_service_started_at = time.time()
    startup_refresh_done = False

    # Never refresh just because the add-on was installed or upgraded (every install restarts this
    # service): skip the startup refresh check for this one start.
    if install_marker.is_first_start_of_version(ADDON.getAddonInfo('version')):
        log.info('service: first start of this add-on version -- skipping the startup refresh '
                 'check; it resumes on the next normal Kodi start')
        startup_refresh_done = True

    last_scan_active_heartbeat = 0.0  # forces an immediate first heartbeat once scanning starts
    last_scan_progress = 0.0
    was_scanning = None  # None until the first loop pass: lets a service that comes up mid-scan backfill

    # Standard Kodi service idle loop: sleep in short increments so
    # abortRequested() (set on Kodi shutdown) is noticed promptly instead of
    # blocking in one long sleep.
    while not monitor.abortRequested():
        # Checked at most once every _WATCH_RATING_CHECK_INTERVAL_SECONDS, not every single
        # ~3s idle-loop tick -- reading three settings and doing this arithmetic is cheap, but
        # there's no reason to do it 20x more often than the coarsest granularity (30-minute
        # steps) the interval setting itself offers.
        now = time.time()
        if now - last_watch_rating_check >= _WATCH_RATING_CHECK_INTERVAL_SECONDS:
            last_watch_rating_check = now
            watch_rating_enabled = xbmcaddon.Addon().getSettingBool('sync_watch_ratings_enabled')

            if watch_rating_enabled and not watch_rating_was_enabled:
                log.info('service: watch/rating sync re-enabled -- treating this as a fresh start')
                service_started_at = now
                startup_sync_done = False
            watch_rating_was_enabled = watch_rating_enabled

            if watch_rating_enabled:
                # Two independent checks, not if/else -- once the startup phase has passed,
                # periodic checking must run on every tick from then on, not just "instead of"
                # the one-time startup check on whichever single tick first noticed it.
                if not startup_sync_done and now - service_started_at >= _startup_delay_seconds():
                    startup_sync_done = True
                    if xbmcaddon.Addon().getSettingBool('sync_on_kodi_start'):
                        interval_seconds = max(30, xbmcaddon.Addon().getSettingInt('sync_interval_minutes')) * 60
                        last_watch_rating_sync = _anchor_after_attempt(
                            now, interval_seconds, monitor.run_watch_rating_sync('startup'))
                if startup_sync_done:
                    interval_seconds = max(30, xbmcaddon.Addon().getSettingInt('sync_interval_minutes')) * 60
                    if now - last_watch_rating_sync >= interval_seconds:
                        last_watch_rating_sync = _anchor_after_attempt(
                            now, interval_seconds, monitor.run_watch_rating_sync('scheduled'))
            else:
                # Keeps the anchor from drifting into the past while the master switch is off,
                # so re-enabling it doesn't immediately fire a sync that "should have" run
                # during however long it was off.
                last_watch_rating_sync = now

        # Same shape as the watch/rating sync block above, fully independent state -- see
        # collection_art_started_at's own doc for why.
        if now - last_collection_art_check >= _WATCH_RATING_CHECK_INTERVAL_SECONDS:
            last_collection_art_check = now
            collection_art_enabled = xbmcaddon.Addon().getSettingBool('sync_collections_enabled')

            if collection_art_enabled and not collection_art_was_enabled:
                log.info('service: collection art sync re-enabled -- treating this as a fresh start')
                collection_art_started_at = now
                collection_art_startup_done = False
            collection_art_was_enabled = collection_art_enabled

            if collection_art_enabled:
                if not collection_art_startup_done and \
                        now - collection_art_started_at >= _startup_delay_seconds():
                    collection_art_startup_done = True
                    if xbmcaddon.Addon().getSettingBool('sync_collections_on_kodi_start'):
                        interval_seconds = max(30, xbmcaddon.Addon().getSettingInt('sync_collections_interval_minutes')) * 60
                        last_collection_art_sync = _anchor_after_attempt(
                            now, interval_seconds, monitor.run_collection_art_sync('startup'))
                if collection_art_startup_done:
                    interval_seconds = max(30, xbmcaddon.Addon().getSettingInt('sync_collections_interval_minutes')) * 60
                    if now - last_collection_art_sync >= interval_seconds:
                        last_collection_art_sync = _anchor_after_attempt(
                            now, interval_seconds, monitor.run_collection_art_sync('scheduled'))
            else:
                last_collection_art_sync = now

        # Once per Kodi start: after Kodi's own startup library scan, re-scrape the items Chronicle
        # says changed since this device last scraped them. Never starts a scan itself.
        if not startup_refresh_done and \
                now - refresh_service_started_at >= _startup_delay_seconds():
            startup_refresh_done = True
            monitor.run_startup_refresh()

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

        # "Where is the scan": one log line a minute while Kodi scans, and one when it ends. Runs on
        # its own daemon thread (a cold source listing can take a while) and reads only what Kodi
        # appended to its log since the last line -- see lib/scan_progress.py.
        if kodi_scanning and not was_scanning:
            scan_progress.begin(backfill=was_scanning is None)
            last_scan_progress = 0.0
        if kodi_scanning and time.time() - last_scan_progress >= _SCAN_PROGRESS_INTERVAL_SECONDS:
            last_scan_progress = time.time()
            threading.Thread(target=scan_progress.tick, name='chronicle-scan-progress',
                              daemon=True).start()
        if was_scanning and not kodi_scanning:
            threading.Thread(target=scan_progress.tick, kwargs={'final': True},
                              name='chronicle-scan-progress-final', daemon=True).start()
        was_scanning = kodi_scanning

        # Covers BOTH content types, even for a user with only this addon installed: the tail
        # this addon's own scraper actions leave behind after Kodi's own directory-walk phase
        # ends (is_active, computed above from the shared activity_tracker signal file) counts
        # as active scanning too -- see report_scan_active()'s own doc.
        if (kodi_scanning or is_active) and \
                time.time() - last_scan_active_heartbeat >= _SCAN_ACTIVE_HEARTBEAT_SECONDS:
            last_scan_active_heartbeat = time.time()
            threading.Thread(
                target=ChronicleClient().report_scan_active,
                name='chronicle-scan-active-heartbeat', daemon=True,
            ).start()

        count = activity.get('count', 0) if activity else 0
        label = (activity.get('last_label') or '') if activity else ''
        suffix = ' -- {0}'.format(label) if label else ''
        message = status_display.status_text(
            kodi_scanning, monitor.cleaning, is_active,
            ADDON.getLocalizedString(32107).format(count, suffix),
            ADDON.getLocalizedString(32151), ADDON.getLocalizedString(32152))

        if message is not None:
            if bg is None:
                bg = xbmcgui.DialogProgressBG()
                bg.create(ADDON.getLocalizedString(32000), message)
                log.info('service: library busy -- showing corner status')
            elif message != last_shown_count:
                bg.update(0, message=message)
            last_shown_count = message
        elif bg is not None:
            bg.close()
            bg = None
            last_shown_count = None
            log.info('service: nothing running -- hiding corner status')
            if not is_active:
                activity_tracker.reset()

        if monitor.waitForAbort(_POLL_INTERVAL_SECONDS):
            break

    if bg is not None:
        bg.close()
    log.info('service: Chronicle Scraper background service stopped')


if __name__ == '__main__':
    run()
