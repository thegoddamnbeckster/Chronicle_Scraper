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

    def onScanFinished(self, library):
        # Kodi fires this for both 'video' and 'music' library scans --
        # nfo_rebuild.py only knows how to rebuild movie/TV NFOs, so a music
        # scan is a silent no-op rather than an error.
        if library != 'video':
            return

        if not ADDON.getSettingBool('auto_rebuild_on_scan'):
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
        try:
            log.info('service: video library scan finished, auto-rebuild is on -- starting NFO rebuild')
            result = nfo_rebuild.run(is_cancelled=self.abortRequested)
            log.info(
                'service: NFO rebuild complete -- {0}/{1} items processed, {2} confirmed rewritten, '
                '{3} nfo deleted, {4} movieset file(s) deleted, {5} refresh error(s)'.format(
                    result['processed'], result['total'], result['nfo_confirmed'], result['nfo_deleted'],
                    result['movieset_deleted'], result['refresh_errors']))
        except Exception as exc:
            log.error('service: NFO rebuild failed: {0}'.format(exc))
        finally:
            self._rebuild_lock.release()


def run():
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

    # Standard Kodi service idle loop: sleep in short increments so
    # abortRequested() (set on Kodi shutdown) is noticed promptly instead of
    # blocking in one long sleep.
    while not monitor.abortRequested():
        if time.time() - last_device_register >= _DEVICE_REREGISTER_INTERVAL_SECONDS:
            threading.Thread(target=device_registration.register, name='chronicle-device-register',
                              daemon=True).start()
            last_device_register = time.time()

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
