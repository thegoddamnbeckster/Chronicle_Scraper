# -*- coding: utf-8 -*-
"""script.chronicle.scraper.tv — Background service entry point.

Deliberately minimal: unlike the sibling Chronicle Scraper (Movies) addon's own service.py
(corner activity status, device registration, watch/rating sync -- see
that addon's own default.py doc for why none of those are duplicated here), this addon's
service exists for two jobs: after each Kodi library scan, checking every episode against
Chronicle (see lib/full_sync_check.py), and once per Kodi start asking Chronicle which already-scraped
items changed so Kodi can re-scrape just those (see lib/kodi_scan_signal.py). It never starts a library
scan: Kodi pulls from Chronicle when IT scans, and a scan this add-on started would also fire every other
add-on's own scan-finished work.

Why this has to be a service of its own, not shared with the Movies addon: Kodi runs
xbmc.service as a per-addon persistent process -- there is no way for one addon's background
service to run code on behalf of a second, separate addon package. The only thing that makes a Kodi
addon run continuously is its own declared xbmc.service extension. Without this file, the
refresh check would only ever run for someone who also happened to have the Movies addon
installed -- silently breaking this addon's own stated promise (see addon.xml) of working
standalone for someone tracking TV shows only.
"""

import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

from lib import activity_tracker
from lib import device_registration
from lib import full_sync_check
from lib import install_marker
from lib import kodi_scan_signal
from lib import pass_progress
from lib import sync_baton
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

_POLL_INTERVAL_SECONDS = 3

# Default for the "seconds to wait after Kodi starts" setting (startup_delay_seconds): how long after
# service start before the once-per-startup refresh check, so Kodi's own start-up -- add-on init,
# network, mounting the drives -- gets room first. Read fresh each time, so changing it needs no restart.
_DEFAULT_STARTUP_DELAY_SECONDS = 10


def _startup_delay_seconds():
    try:
        return max(0, ADDON.getSettingInt('startup_delay_seconds'))
    except Exception:
        return _DEFAULT_STARTUP_DELAY_SECONDS


class ChronicleTVMonitor(xbmc.Monitor):
    def __init__(self):
        super(ChronicleTVMonitor, self).__init__()
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
        """Kodi's own native callback, fired the same way regardless of what started the
        scan -- its own automatic startup scan, the user's manual "Update Library", or another
        add-on's. Per-user direction (2026-09-22): the full sync-check should run after ALL of those
        alike, gated by its own settings toggle (default on) rather than trying to tell them
        apart -- see lib/full_sync_check.py's own doc for what this actually does and why."""
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
                     'inside the {2}s cooldown (one scan can fire '
                     'onScanFinished several times)'.format(
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

                # Takes its turn with the movie add-on's syncs (lib/sync_baton.py): one background task at a time on this Kodi.
                with sync_baton.hold(sync_baton.owner_id(ADDON.getAddonInfo('id')), 'the episode check', log,
                                     is_aborted=self.abortRequested) as got:
                    if not got:
                        return
                    log.info('service: starting full sync-check ({0})'.format(trigger_label))
                    # This pass writes corrections into the library, which makes Kodi's screens refresh;
                    # the corner progress says so from the first moment (lib/pass_progress.py).
                    progress = pass_progress.PassProgress(
                        ADDON.getLocalizedString(32186), ADDON.getLocalizedString(32187), log)
                    progress.start()
                    try:
                        # Also stops the sweep if a fresh scan/scrape starts mid-pass -- see the Movies
                        # addon's own identical comment for why.
                        result = full_sync_check.run(
                            is_cancelled=lambda: self.abortRequested() or
                                self._should_defer_for_active_scan('full sync-check ({0})'.format(trigger_label)),
                            progress_callback=progress.update)
                    finally:
                        progress.finish()
                    log.info('service: full sync-check ({0}) complete -- {1}'.format(
                             trigger_label, result))
                    self._notify_full_sync_check_result(result)
                    self._full_sync_check_last_completed_at = time.time()
            except Exception as exc:
                log.error('service: full sync-check ({0}) failed: {1}'.format(trigger_label, exc))
            finally:
                self._full_sync_check_lock.release()

        threading.Thread(target=_do, name='chronicle-tv-full-sync-check', daemon=True).start()

    def _notify_full_sync_check_result(self, result):
        """Says how the post-scan check ended, so a pass that ran (and refreshed the screens) never ends
        silently. Best-effort: a notification problem must not look like a failed check."""
        try:
            icon = xbmcgui.NOTIFICATION_INFO
            if result.get('aborted'):
                icon = xbmcgui.NOTIFICATION_WARNING
                message = ADDON.getLocalizedString(32190).format(result['aborted'])
            elif result.get('cancelled'):
                message = ADDON.getLocalizedString(32189)
            else:
                if result.get('errors'):
                    icon = xbmcgui.NOTIFICATION_WARNING
                message = ADDON.getLocalizedString(32188).format(
                    result.get('checked', 0), result.get('updated', 0),
                    ADDON.getLocalizedString(32191).format(result['errors']) if result.get('errors') else '')
            xbmcgui.Dialog().notification(ADDON.getLocalizedString(32000), message, icon=icon, time=8000)
        except Exception as exc:
            log.warning('service: could not show the full sync-check result: {0}'.format(exc))

    def run_startup_refresh(self):
        """Runs this addon's own once-per-startup refresh check on a background thread -- see
        kodi_scan_signal.run_startup_refresh's own doc. Not a recurring poll. Movies are the
        Movies addon's own responsibility, via its own identical call with its own kind."""
        threading.Thread(
            target=kodi_scan_signal.run_startup_refresh, args=(['episode', 'tvshow'],),
            kwargs={'is_aborted': self.abortRequested},
            name='chronicle-tv-startup-refresh', daemon=True,
        ).start()


def run():
    monitor = ChronicleTVMonitor()
    log.info('service: Chronicle Scraper (TV) background service started')

    # Registered once per service start -- see lib/device_registration.py. Best-effort and silent
    # when Chronicle is not connected yet.
    threading.Thread(target=device_registration.register, name='chronicle-tv-device-register',
                     daemon=True).start()

    service_started_at = time.time()
    startup_refresh_done = False
    last_scan_active_heartbeat = 0.0  # forces an immediate first heartbeat once scanning starts

    # Never refresh just because the add-on was installed or upgraded (every install restarts this service): skip the
    # startup refresh check for this one start. It resumes on the next normal Kodi start.
    if install_marker.is_first_start_of_version(ADDON.getAddonInfo('version')):
        log.info('service: first start of this add-on version -- skipping the startup refresh '
                 'check; it resumes on the next normal Kodi start')
        startup_refresh_done = True

    # Standard Kodi service idle loop: sleep in short increments so abortRequested() (set on
    # Kodi shutdown) is noticed promptly instead of blocking in one long sleep.
    while not monitor.abortRequested():
        now = time.time()

        # Once per Kodi start: after Kodi's own startup library scan, re-scrape the items Chronicle
        # says changed since this device last scraped them. Never starts a scan itself.
        if not startup_refresh_done and now - service_started_at >= _startup_delay_seconds():
            startup_refresh_done = True
            monitor.run_startup_refresh()

        # Covers this addon's own scraper-activity tail (per-episode find/getepisodedetails
        # calls, each its own short-lived process -- see lib/activity_tracker.py) as well as
        # Kodi's own directory-walk phase -- see ChronicleClient.report_scan_active's own doc.
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
