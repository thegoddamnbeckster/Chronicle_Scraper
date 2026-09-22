# -*- coding: utf-8 -*-
"""script.chronicle.scraper.tv — Background service entry point.

Deliberately minimal: unlike the sibling Chronicle Scraper (Movies) addon's own service.py
(corner activity status, device registration, NFO-rebuild-on-scan, watch/rating sync -- see
that addon's own default.py doc for why none of those are duplicated here), this addon's
service exists for exactly one job: periodically polling Chronicle's "new content available"
signal (see lib/kodi_scan_signal.py) and triggering this device's own local VideoLibrary.Scan
when due.

Why this has to be a service of its own, not shared with the Movies addon: Kodi runs
xbmc.service as a per-addon persistent process -- there is no way for one addon's background
service to run code on behalf of a second, separate addon package. The NFO-rebuild coordination
the Movies addon's service provides for BOTH content types works around that by writing to
shared on-disk signal files under special://temp/chronicle_scraper/, which either addon's own
on-demand scrape (find/getdetails) can read -- but a periodic POLL has nothing to piggyback on
that way, since nothing about it is triggered by a scrape. The only thing that makes a Kodi
addon run continuously is its own declared xbmc.service extension. Without this file, the
scan-signal feature would only ever run for someone who also happened to have the Movies addon
installed -- silently breaking this addon's own stated promise (see addon.xml) of working
standalone for someone tracking TV shows only.
"""

import threading
import time

import xbmc
import xbmcaddon

from lib import activity_tracker
from lib import full_sync_check
from lib import kodi_scan_signal
from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log = Logger('service')

# Same idle-timeout/heartbeat shape as the sibling Movies addon's own service.py -- see that
# module's own doc for report_scan_active()'s full reasoning. Duplicated (not shared) because
# this addon must keep working standalone with no Movies addon installed at all -- see this
# file's own top-of-module doc for why the two services can't share code across addon packages.
_ACTIVITY_IDLE_TIMEOUT_SECONDS = 30
_SCAN_ACTIVE_HEARTBEAT_SECONDS = 60

# onScanFinished fires the instant a scan ends, which is exactly when this addon's own
# just-finished per-episode scraper activity is still inside _ACTIVITY_IDLE_TIMEOUT_SECONDS --
# see run_full_sync_check's own doc. Bounded so a genuinely stuck/long-running scrape can't wait
# forever; generous since a real scan/scrape tail can legitimately run long on a big library.
_FULL_SYNC_CHECK_DEFER_WAIT_TIMEOUT_SECONDS = 20 * 60
_FULL_SYNC_CHECK_DEFER_POLL_SECONDS = 10

# VideoLibrary.Scan finishing cascades into every OTHER addon's own onScanFinished handler too
# (see kodi_scan_signal.py's own doc on this) -- one native/manual/self-triggered scan can
# legitimately fire onScanFinished several times in quick succession as different addons' own
# triggered scans each complete in turn. This keeps a completed full sync-check pass from
# re-running on every one of those, not just the first. Same value as the Movies addon's own.
_FULL_SYNC_CHECK_MIN_SECONDS_BETWEEN_RUNS = 5 * 60

# Chronicle's "new content available" flag (lib/kodi_scan_signal.py) is always checked once
# after Kodi starts, regardless of the scan_signal_enabled setting -- most imports land via
# Chronicle's own nightly schedule, so the next Kodi startup already catches them with no
# recurring poll needed. Periodic re-checking while Kodi keeps running is opt-in (default off,
# see settings.xml) at a user-configurable interval (scan_signal_interval_minutes, default 120)
# -- a triggered scan cascades into every OTHER addon's own onScanFinished handler too (confirmed
# live: Chronicle_Scrobbler's VideoLibrary.Clean, SIMKL's own full sync pass), so this defaults
# to conservative rather than "as fast as possible." Same settings/behavior as the sibling
# Movies addon's own copy of this same logic.
_SCAN_SIGNAL_STARTUP_DELAY_SECONDS = 60
_POLL_INTERVAL_SECONDS = 3

# Short delay before the startup-scan followup check (lib/kodi_scan_signal.py's
# run_startup_scan_followup) -- just enough that Kodi's own JSON-RPC server is definitely up;
# the check itself is a single cheap local settings read, and the followup scan it may launch
# already does its own bounded wait for the network/share to actually be reachable.
_STARTUP_SCAN_FOLLOWUP_DELAY_SECONDS = 10


class ChronicleTVMonitor(xbmc.Monitor):
    def __init__(self):
        super(ChronicleTVMonitor, self).__init__()
        # Guards against two check_and_scan() calls overlapping if one is still waiting on a
        # slow/hanging network call when the next poll interval comes due -- cheap insurance,
        # not a response to any observed failure. Same precedent as the Movies addon's own
        # _scan_signal_lock.
        self._scan_signal_lock = threading.Lock()
        # Guards against onScanFinished firing again (a second scan completing) while a
        # previous full sync-check pass is still walking the library.
        self._full_sync_check_lock = threading.Lock()
        # See _FULL_SYNC_CHECK_MIN_SECONDS_BETWEEN_RUNS's own doc -- 0.0 so the very first
        # onScanFinished this session always runs regardless of when the service itself started.
        self._full_sync_check_last_completed_at = 0.0

    def _should_defer_for_active_scan(self, task_label):
        """True if either Kodi's own library scan (Library.IsScanning) or this addon's own
        scraper activity tail (activity_tracker) is recent enough to still be "in progress" --
        same reasoning and same shared activity_tracker signal as the Movies addon's own
        identically-named method (see that file's own doc): full_sync_check's own work
        shouldn't contend with an active scan/scrape for the same local/server capacity."""
        kodi_scanning = xbmc.getCondVisibility('Library.IsScanning')
        scraper_active = activity_tracker.is_recently_active(_ACTIVITY_IDLE_TIMEOUT_SECONDS)
        if kodi_scanning or scraper_active:
            log.info('service: {0} deferred -- {1} still in progress'.format(
                     task_label, 'a Kodi library scan' if kodi_scanning else 'scraper activity'))
            return True
        return False

    def onScanFinished(self, library):
        """Kodi's own native callback, fired the same way regardless of what triggered the
        scan -- its own automatic startup scan, the user's manual "Update Library", or this
        addon's own new-content-detection feature calling VideoLibrary.Scan. Per-user direction
        (2026-09-22): the full sync-check should run after ALL of those alike, gated by its own
        settings toggle (default on) rather than trying to tell them apart -- see
        lib/full_sync_check.py's own doc for what this actually does and why."""
        if library != 'video':
            return
        self.run_full_sync_check('scan finished')

    def run_full_sync_check(self, trigger_label):
        """Runs one full_sync_check.run() pass -- checks every episode Kodi already has
        against Chronicle's current data (matched by file, not title/season/episode) and
        corrects only whatever disagrees. Guarded by _full_sync_check_lock so an onScanFinished
        firing again mid-pass can't overlap a previous one."""
        if not ADDON.getSettingBool('full_sync_check_enabled'):
            return
        since_last = time.time() - self._full_sync_check_last_completed_at
        if since_last < _FULL_SYNC_CHECK_MIN_SECONDS_BETWEEN_RUNS:
            log.info('service: full sync-check ({0}) skipped -- a pass completed {1:.0f}s ago, '
                     'inside the {2}s cooldown (VideoLibrary.Scan finishing cascades into '
                     'multiple onScanFinished firings)'.format(
                     trigger_label, since_last, _FULL_SYNC_CHECK_MIN_SECONDS_BETWEEN_RUNS))
            return
        if not self._full_sync_check_lock.acquire(False):
            log.info('service: full sync-check ({0}) skipped -- another pass is already '
                     'running'.format(trigger_label))
            return

        def _do():
            try:
                # Checked INSIDE the thread, with a bounded wait -- see the Movies addon's own
                # identical comment for why checking once before spawning would make this defer
                # on every single run and never get a second chance.
                deadline = time.time() + _FULL_SYNC_CHECK_DEFER_WAIT_TIMEOUT_SECONDS
                while self._should_defer_for_active_scan('full sync-check ({0})'.format(trigger_label)):
                    if self.abortRequested() or time.time() >= deadline:
                        log.info('service: full sync-check ({0}) gave up waiting for other '
                                 'activity to clear'.format(trigger_label))
                        return
                    if self.waitForAbort(_FULL_SYNC_CHECK_DEFER_POLL_SECONDS):
                        return

                log.info('service: starting full sync-check ({0})'.format(trigger_label))
                # Also stops the sweep if a fresh scan/scrape starts mid-pass -- see the Movies
                # addon's own identical comment for why.
                result = full_sync_check.run(is_cancelled=lambda: self.abortRequested() or
                    self._should_defer_for_active_scan('full sync-check ({0})'.format(trigger_label)))
                log.info('service: full sync-check ({0}) complete -- {1}'.format(
                         trigger_label, result))
                self._full_sync_check_last_completed_at = time.time()
            except Exception as exc:
                log.error('service: full sync-check ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                self._full_sync_check_lock.release()

        threading.Thread(target=_do, name='chronicle-tv-full-sync-check', daemon=True).start()

    def run_scan_signal_check(self):
        if not self._scan_signal_lock.acquire(False):
            log.info('service: scan-signal check skipped -- another check is already running')
            return
        try:
            threading.Thread(
                target=self._run_scan_signal_check_locked,
                name='chronicle-tv-scan-signal-check', daemon=True,
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
        """Also runs this addon's own check_and_refresh(['episode', 'tvshow']) as the followup's
        own last step, once the scan it fires (or defers to) actually finishes -- see
        kodi_scan_signal.run_startup_scan_followup's own doc. Not a recurring poll: this fires
        at most once per Kodi session, same as the scan followup itself. Movies are the Movies
        addon's own responsibility, via its own identical copy of this same call with its own
        kind."""
        threading.Thread(
            target=self._run_startup_scan_followup, args=(service_started_at,),
            name='chronicle-tv-startup-scan-followup', daemon=True,
        ).start()

    def _run_startup_scan_followup(self, service_started_at):
        try:
            kodi_scan_signal.run_startup_scan_followup(
                service_started_at, ['episode', 'tvshow'], is_aborted=self.abortRequested)
        except Exception as exc:
            log.error('service: startup-scan followup failed: {0}'.format(exc))


def run():
    monitor = ChronicleTVMonitor()
    log.info('service: Chronicle Scraper (TV) background service started')

    service_started_at = time.time()
    startup_done = False
    startup_followup_done = False
    last_scan_signal_check = 0.0  # only consulted once scan_signal_enabled is on
    last_scan_active_heartbeat = 0.0  # forces an immediate first heartbeat once scanning starts

    # Standard Kodi service idle loop: sleep in short increments so abortRequested() (set on
    # Kodi shutdown) is noticed promptly instead of blocking in one long sleep.
    while not monitor.abortRequested():
        now = time.time()

        # Startup check: always runs exactly once, regardless of scan_signal_enabled -- see
        # this file's own top-of-module doc for why a recurring poll isn't the default.
        if not startup_done and now - service_started_at >= _SCAN_SIGNAL_STARTUP_DELAY_SECONDS:
            startup_done = True
            monitor.run_scan_signal_check()

        # Independent of scan_signal_enabled above -- see lib/kodi_scan_signal.py's own doc
        # (Startup-scan followup). Always runs exactly once; the function itself is a no-op if
        # Kodi's native "Update library on startup" setting turns out to be off. Also runs this
        # addon's own per-item refresh-check as its own last step, once the scan actually
        # finishes -- see run_startup_scan_followup's own doc above; not a recurring poll.
        if not startup_followup_done and \
                now - service_started_at >= _STARTUP_SCAN_FOLLOWUP_DELAY_SECONDS:
            startup_followup_done = True
            monitor.run_startup_scan_followup(service_started_at)

        if ADDON.getSettingBool('scan_signal_enabled'):
            interval_seconds = max(30, ADDON.getSettingInt('scan_signal_interval_minutes')) * 60
            if now - last_scan_signal_check >= interval_seconds:
                last_scan_signal_check = now
                monitor.run_scan_signal_check()

        # Covers this addon's own scraper-activity tail (per-episode find/NfoUrl/
        # getepisodedetails calls, each its own short-lived process -- see
        # lib/activity_tracker.py) as well as Kodi's own directory-walk phase, since
        # NfoGenerationService's scheduled sweep used to contend with an active scan for the
        # same server/IO capacity for the whole duration of either -- see
        # ChronicleClient.report_scan_active's own doc.
        kodi_scanning = xbmc.getCondVisibility('Library.IsScanning')
        activity = activity_tracker.read_activity()
        scraper_active = activity is not None and \
            (time.time() - activity.get('timestamp', 0)) < _ACTIVITY_IDLE_TIMEOUT_SECONDS
        if (kodi_scanning or scraper_active) and \
                now - last_scan_active_heartbeat >= _SCAN_ACTIVE_HEARTBEAT_SECONDS:
            last_scan_active_heartbeat = now
            threading.Thread(
                target=ChronicleClient().report_scan_active,
                name='chronicle-tv-scan-active-heartbeat', daemon=True,
            ).start()

        if monitor.waitForAbort(_POLL_INTERVAL_SECONDS):
            break

    log.info('service: Chronicle Scraper (TV) background service stopped')


if __name__ == '__main__':
    run()
