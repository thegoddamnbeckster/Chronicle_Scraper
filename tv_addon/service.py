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


class ChronicleTVMonitor(xbmc.Monitor):
    def __init__(self):
        super(ChronicleTVMonitor, self).__init__()
        # Guards against two check_and_scan() calls overlapping if one is still waiting on a
        # slow/hanging network call when the next poll interval comes due -- cheap insurance,
        # not a response to any observed failure. Same precedent as the Movies addon's own
        # _scan_signal_lock.
        self._scan_signal_lock = threading.Lock()

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


def run():
    monitor = ChronicleTVMonitor()
    log.info('service: Chronicle Scraper (TV) background service started')

    service_started_at = time.time()
    startup_done = False
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
