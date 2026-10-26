# -*- coding: utf-8 -*-
"""Per-item refresh check for the Chronicle Scraper add-ons: once per Kodi startup, after Kodi's own
startup library scan has finished, ask Chronicle which already-scraped items changed since this
device last scraped them and have Kodi re-scrape just those.

This module used to also start library scans (when Chronicle signalled new content, and a second
one at startup). It no longer does, by design: Kodi pulls from Chronicle when IT scans, and the
add-on never triggers one -- a triggered scan also fires every other add-on's own
onScanFinished work (Chronicle_Scrobbler cleans the library, SIMKL runs a full sync), which is
what made the screens flicker. New content therefore appears at Kodi's own scans: its "Update
library on startup" scan, a manual one, or any other add-on's.

Identical in content to the sibling Chronicle Scraper (Movies/TV) add-on's copy -- synced at
build time from this file (see build.ps1), not maintained twice. xbmcaddon.Addon().getAddonInfo('id')
is what makes the same file behave correctly for whichever add-on runs it.
"""

import json
import time

import xbmc

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('kodi_scan_signal')

# Bounded wait for Library.IsScanning to flip True at all after startup -- Kodi's own startup scan
# starts a moment after its services do. If it never flips, there is no scan to wait out.
_SCAN_FINISH_STARTED_TIMEOUT_SECONDS = 30
_SCAN_FINISH_STARTED_POLL_SECONDS = 1
# Bounded wait for Library.IsScanning to go back to False once it is confirmed running. Generous --
# real full-library scans ranged from under a minute to well over an hour.
_SCAN_FINISH_MAX_WAIT_SECONDS = 3 * 60 * 60
_SCAN_FINISH_POLL_SECONDS = 15


def _read_native_startup_scan_setting():
    """True/False/None (unreadable) -- see this module's own doc for why this reads Kodi's own
    "Update library on startup" setting directly instead of the addon owning a copy of it."""
    try:
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'Settings.GetSettingValue',
                   'params': {'setting': 'videolibrary.updateonstartup'}}
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        return response.get('result', {}).get('value')
    except Exception as exc:
        log.warning('kodi_scan_signal: could not read videolibrary.updateonstartup: {0}'.format(exc))
        return None


def _wait_for_scan_to_finish(is_aborted):
    """Blocks (bounded) until Kodi's own Library.IsScanning flag goes back to False, so a caller
    can run logic that must happen strictly AFTER a scan actually completes -- not merely "some
    time after it was requested". Covers Kodi's own native startup scan
    or one a sibling add-on or the user started.

    First waits (briefly, bounded) for the flag to actually flip True at all -- there's a short
    window at startup where Kodi hasn't set it yet. If it never does, there's nothing
    running to wait out, so this returns immediately rather than sitting through the full
    "finish" timeout for no reason."""
    deadline = time.time() + _SCAN_FINISH_STARTED_TIMEOUT_SECONDS
    started = False
    while time.time() < deadline:
        if is_aborted():
            return
        if xbmc.getCondVisibility('Library.IsScanning'):
            started = True
            break
        time.sleep(_SCAN_FINISH_STARTED_POLL_SECONDS)
    if not started:
        return

    deadline = time.time() + _SCAN_FINISH_MAX_WAIT_SECONDS
    while time.time() < deadline:
        if is_aborted():
            return
        if not xbmc.getCondVisibility('Library.IsScanning'):
            return
        time.sleep(_SCAN_FINISH_POLL_SECONDS)
    log.warning('kodi_scan_signal: startup refresh -- scan still running after {0}s, '
                'proceeding to the refresh-check anyway'.format(_SCAN_FINISH_MAX_WAIT_SECONDS))


def run_startup_refresh(kinds, is_aborted=None):
    """Best-effort, safe to call once from service.py shortly after startup. Does nothing (not an
    error) when Kodi's native "Update library on startup" setting is off or unreadable -- there is
    then no startup scan whose end to wait for, and the refresh rides on that one checkbox alone,
    with nothing new to configure. Otherwise waits for Kodi's own startup scan to finish and then
    runs check_and_refresh(kinds) -- never in parallel with it, and never starting a scan itself.
    is_aborted (e.g. an xbmc.Monitor's abortRequested) lets a caller cut the waits short on Kodi
    shutdown. kinds is passed straight through (['movie'] for the Movies add-on, ['episode',
    'tvshow'] for the TV add-on)."""
    is_aborted = is_aborted or (lambda: False)
    try:
        if _read_native_startup_scan_setting() is not True:
            return
        _wait_for_scan_to_finish(is_aborted)
        if is_aborted():
            return
        check_and_refresh(kinds)
    except Exception as exc:
        log.warning('kodi_scan_signal: startup refresh failed: {0}'.format(exc))


## Per-item refresh-push signal (check_and_refresh) -- 2026-09-18, re-triggered 2026-09-19
#
# Per-user direction: Kodi already has already-scraped metadata go stale with no way to refresh
# it short of a manual per-item "Refresh information" click. Chronicle can't call a device's
# JSON-RPC directly XX
# polls Chronicle's own kodi-refresh-signal for items it already knows about (by kind) whose own
# metadata has changed since this device last scraped them, and fires one local
# VideoLibrary.Refresh* per item. See Chronicle's own IKodiDeviceService.GetItemsNeedingRefreshAsync
# doc for the full design, including why there's no separate acknowledgement call here.
#
# NOT a recurring/periodic poll (2026-09-19 correction to this feature's first shipped version,
# which was): this only ever runs once per Kodi startup, gated on the native "Update
# library on startup" setting, and only after that scan finishes, via
# _wait_for_scan_to_finish(). check_and_refresh() itself has no gating logic of its own; it's a
# pure poll-and-refresh primitive, and run_startup_refresh owns all of the "when/whether to
# call it" decisions. There is deliberately no settings.xml toggle for this feature at all -- it
# rides entirely on the native startup-scan checkbox, nothing new to configure.
#
# Each addon calls check_and_refresh with only the kind(s) it actually owns (Movies: "movie";
# TV: "episode", "tvshow") from its own run_startup_refresh call in its own service.py.

_REFRESH_METHOD_BY_KIND = {
    'movie':   ('VideoLibrary.RefreshMovie', 'movieid'),
    'episode': ('VideoLibrary.RefreshEpisode', 'episodeid'),
    'tvshow':  ('VideoLibrary.RefreshTVShow', 'tvshowid'),
}


def check_and_refresh(kinds):
    """Polls Chronicle for items of the given kinds (e.g. ['movie'] or ['episode', 'tvshow'])
    that are due for a local refresh, and fires one VideoLibrary.Refresh* per item. Best-effort
    throughout: one item failing to refresh (a stale/removed kodi_id, a transient JSON-RPC
    error) never stops the rest, and any failure here is logged, not raised -- same tolerance
    as run_startup_refresh's own top-level guard, since this is the unattended last step of a
    startup check, not something a user is watching. Has no gating logic of its own -- see
    "Per-item refresh-push signal" above for why its only caller, run_startup_refresh,
    owns all of the "when/whether to call this" decisions."""
    try:
        client = ChronicleClient()
        items = client.get_refresh_signal(kinds)
        if not items:
            return
        log.info('kodi_scan_signal: refresh-signal -- {0} item(s) due for a local '
                  'refresh'.format(len(items)))
        for item in items:
            kind = item.get('kind')
            kodi_id = item.get('kodiId')
            method_param = _REFRESH_METHOD_BY_KIND.get(kind)
            if method_param is None or not kodi_id:
                log.warning('kodi_scan_signal: refresh-signal -- skipping unrecognized item '
                            '{0!r}'.format(item))
                continue
            method, param_name = method_param
            request = {'jsonrpc': '2.0', 'id': 1, 'method': method,
                       'params': {param_name: kodi_id}}
            try:
                response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
            except Exception as exc:
                log.warning('kodi_scan_signal: refresh-signal -- {0}({1}={2}) call failed: '
                            '{3}'.format(method, param_name, kodi_id, exc))
                continue
            if 'error' in response:
                log.warning('kodi_scan_signal: refresh-signal -- {0}({1}={2}) rejected: '
                            '{3}'.format(method, param_name, kodi_id, response['error']))
    except Exception as exc:
        log.warning('kodi_scan_signal: refresh-signal check failed: {0}'.format(exc))
