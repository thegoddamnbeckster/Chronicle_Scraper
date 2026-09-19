# -*- coding: utf-8 -*-
"""Polls Chronicle for "new movie/TV content was imported" and, if due, triggers a scan of only
this device's own source folder(s) that THIS addon is actually the configured scraper for -- so
it actually discovers the new episode/movie file.

Why this has to exist at all: VideoLibrary.Refresh* (what NfoPushService and this addon's own
per-edit sync rely on for every other change) only works on an item Kodi's own VideoLibrary
already has an entry for -- it cannot make Kodi discover a file it doesn't know about yet. Only
VideoLibrary.Scan can do that.

Deliberately pull, not push: Chronicle's server never calls this device's JSON-RPC-over-HTTP
endpoint for this (unlike NfoPushService, which does, and needs "Allow remote control via HTTP"
turned on for it). This module polls Chronicle's own signal flag instead and, if it's newer than
this device's last acknowledgement, runs VideoLibrary.Scan via xbmc.executeJSONRPC -- a purely
local, in-process call this addon always has access to regardless of any Kodi network setting,
and regardless of whether this device has ever self-registered anywhere (this feature's own
server-side tracking is keyed by API token, not by a registered-device row -- see Chronicle's
own KodiScanAck model doc).

## Scoped to exactly the folder(s) this addon is configured for -- never a blind whole-library
   scan (2026-09-11 correction)

The first version of this called VideoLibrary.Scan with no `directory` at all, which scans
EVERY configured video source regardless of content type -- confirmed live to be wrong: on a
device where only the TV addon is set up, a TV-triggered scan would still touch movie sources
Chronicle has no relationship with on that device at all (a different scraper entirely, or no
Chronicle involvement whatsoever). Worse, VideoLibrary.Scan finishing fires Kodi's global
onScanFinished(video) event, which EVERY other addon's own Monitor reacts to independently --
confirmed live that Chronicle_Scrobbler runs a throttled VideoLibrary.Clean (a full existence
check over the whole library) and SIMKL Scrobbler runs its own full sync pass on every single
firing of that same event. A blind, frequent scan trigger doesn't just cost this addon
something; it cascades into every other addon's own expensive work too.

There is no JSON-RPC method that exposes which local source folder is mapped to which content
type/scraper -- that mapping only exists in Kodi's own video database (the `path` table's
strContent/strScraper columns, populated by Video Sources > "This directory contains..." >
choose a scraper). So this module reads that table directly, read-only, live, every time --
never cached, never inferred from Chronicle's own server-side folder paths (which have no
reliable mapping to how any given device names that same share as one of its own sources
anyway). The user can repoint a folder to a different scraper at any time; the next signal
after that change picks the current assignment up automatically, with nothing to update on
Chronicle's side at all.

See Chronicle's own IKodiDeviceService.SignalNewContentAsync/IsScanNeededAsync docs for the
server side of all this.

Identical in content to the sibling Chronicle Scraper (Movies/TV) addon's own copy of this
module -- synced at build time from this file (see build.ps1's SharedFiles), not maintained as
two independently-edited copies. xbmcaddon.Addon().getAddonInfo('id') is what makes the same
file behave correctly for whichever addon is actually running it.

## Startup-scan followup (run_startup_scan_followup) -- 2026-09-18

Separate feature, same file because it shares find_own_source_directories() and the
"cross-addon shared special://temp/ marker" pattern above. Confirmed live: Kodi's own "Update
library on startup" setting fires a real VideoLibrary.Scan, but early enough in the device's
own boot sequence that network shares aren't mounted yet -- it sees an empty/unreachable
source and finishes in under a minute, instead of the full many-thousand-file pass a later
manual "Scan Library" does once the network is actually up.

Per-user direction (2026-09-18): fix this WITHOUT touching that checkbox -- it must stay the
one control, nothing new to configure or disable. There is no addon hook to intercept Kodi's
own native scan trigger itself (Python addons only see onScanStarted/onScanFinished, both
AFTER the fact, and there's no documented way to cancel an in-progress core scan job either),
so this doesn't try to race or replace that trigger. Instead it independently reads the exact
same setting the checkbox itself writes -- videolibrary.updateonstartup, a core (not
addon-specific) setting, readable over the same JSON-RPC Settings API this module already uses
elsewhere for VideoLibrary.Scan -- and, only if it's on, does its own correctly-timed
equivalent: waits (bounded) for this addon's own configured source folder to actually be
reachable, then fires one full, unscoped VideoLibrary.Scan itself, matching manual-scan
semantics exactly (not scoped to this addon's own folder the way check_and_scan()'s triggered
scans deliberately are -- "the same scan I get when I call for a manual scan" means everything,
same as the native checkbox's own intent).

Runs at most once per Kodi session: special://temp/chronicle_scraper/startup_followup_claimed.txt
(same shared-file pattern as lib/activity_tracker.py, same "no real locking" tradeoff -- if both
the Movies and TV addons are installed and happen to race this exact check within the same
poll tick, both firing the followup scan once is a harmless, merely-redundant outcome, not
worth real cross-process locking for) lets whichever addon's service claims first take
responsibility for actually firing the scan; the other sees the claim and skips firing.

Both addons still each run their OWN reachability wait for their OWN configured folder before
ever consulting the claim marker (confirmed live/by review 2026-09-18: claiming first and
skipping the loser's own wait would mean the eventual global scan can fire while the losing
addon's own share is still unmounted -- exactly the bug this feature exists to fix, just moved
to whichever addon didn't win the race). This does still leave one accepted, documented gap:
if the two addons' shares come up at very different times, the WINNER'S folder being reachable
is what triggers the fire, not confirmation that every installed addon's folder is reachable --
there's no cross-addon barrier here, only "wait for my own folder, then race to claim." In
practice both addons' sources are normally the same NAS/share, so this residual window is
narrow; closing it fully would need real cross-process coordination for a corner case, which
isn't worth it here.

Also feeds the SAME throttle check_and_scan() already respects (_mark_scan_triggered(), on a
successful fire) and defers to an already-active scan (xbmc.getCondVisibility('Library.IsScanning'))
before firing -- without either of those, a device with scan_signal_enabled on could fire this
followup's own unscoped scan and then, moments later, check_and_scan()'s own startup check could
fire ANOTHER scan on top of it, doubling the onScanFinished cascade this module's own
check_and_scan() doc argues at length against.
"""

import glob
import json
import os
import sqlite3
import threading
import time

import xbmc
import xbmcaddon
import xbmcvfs

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('kodi_scan_signal')

ADDON = xbmcaddon.Addon()

# Minimum real time between actually TRIGGERED scans -- distinct from how often we merely poll
# Chronicle for the flag (that stays cheap and frequent, a single GET). This exists because of
# the onScanFinished cascade described above: a triggered scan here isn't a small, contained
# cost, it's every other addon's own scan-finished handler firing too. Longer than
# Chronicle_Scrobbler's own VideoLibrary.Clean throttle (120s) specifically so THIS addon isn't
# what makes that threshold trip on every single cycle.
_MIN_SECONDS_BETWEEN_TRIGGERED_SCANS = 15 * 60


def _marker_path():
    return xbmcvfs.translatePath(ADDON.getAddonInfo('profile') + 'last_scan_signal_trigger.txt')


def _scan_recently_triggered():
    marker = _marker_path()
    if not xbmcvfs.exists(marker):
        return False
    try:
        f = xbmcvfs.File(marker, 'r')
        try:
            last_run = float(f.read() or '0')
        finally:
            f.close()
    except Exception as exc:
        log.warning('kodi_scan_signal: could not read scan-throttle marker: {0}'.format(exc))
        return False
    return (time.time() - last_run) < _MIN_SECONDS_BETWEEN_TRIGGERED_SCANS


def _mark_scan_triggered():
    try:
        f = xbmcvfs.File(_marker_path(), 'w')
        try:
            f.write(bytearray(str(time.time()), 'utf-8'))
        finally:
            f.close()
    except Exception as exc:
        log.warning('kodi_scan_signal: could not write scan-throttle marker: {0}'.format(exc))


def _find_video_db_path():
    """Locates Kodi's own current video database file (special://database/MyVideosNNN.db).
    Picks the highest schema version present -- there's normally exactly one; a leftover from a
    Kodi downgrade is the only realistic case with more than one, and the newest schema number
    is always the one actively in use. Returns None (not an error) if nothing matches."""
    db_dir = xbmcvfs.translatePath('special://database/')
    candidates = sorted(glob.glob(os.path.join(db_dir, 'MyVideos*.db')))
    return candidates[-1] if candidates else None


def find_own_source_directories():
    """Reads Kodi's own video database (read-only, so this never contends with Kodi's own open
    write connection for a lock) for the top-level source paths whose scraper is set to THIS
    addon specifically (ADDON.getAddonInfo('id') -- e.g. script.chronicle.scraper.tv). This is
    the only correct way to answer "which of this device's folders am I actually responsible
    for": the user sets this per-folder in Kodi's own Video Sources dialog, it can change at any
    time, and it has to be resolved locally and live -- see this module's own doc for why it can
    never be cached or inferred from Chronicle's own server-side data.

    Public (no leading underscore) for internal clarity, though this specific function isn't
    imported cross-module: tv_addon/lib/library_repair.py's repair_stale_shows() needed this
    exact same scoped-lookup logic (see that function's own doc for why an unscoped scan is
    actively harmful, not just wasteful) but deliberately reuses its own local
    own_source_directories()/trigger_scan() helpers instead of importing this module -- see
    library_repair.py's own docstring for why (this file isn't checked into tv_addon/lib/ in
    source control, only build-time-copied, so nothing under tv_addon/tests/ could import it).

    idParentPath IS NULL restricts this to actual configured sources, not every subfolder Kodi's
    scanner has separately recorded underneath a recursively-scanned one (scanning the parent
    directory already covers those).

    Returns [] -- not a fallback to "scan everything," an genuine empty result -- if this addon
    isn't assigned to any source on this device, if the database can't be located, or if
    anything about reading it fails. "Obey whatever setting is on each folder" means a folder
    with no matching setting gets left alone, never scanned as a guess."""
    db_path = _find_video_db_path()
    if not db_path:
        log.warning('kodi_scan_signal: could not locate Kodi\'s own video database -- '
                     'treating as "no source folder is configured for this addon"')
        return []

    own_id = ADDON.getAddonInfo('id')
    try:
        conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
        try:
            rows = conn.execute(
                'SELECT DISTINCT strPath FROM path WHERE strScraper = ? AND idParentPath IS NULL',
                (own_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning('kodi_scan_signal: could not read Kodi\'s own video database ({0}) -- '
                     'treating as "no source folder is configured for this addon"'.format(exc))
        return []

    return [row[0] for row in rows]


def check_and_scan():
    """Best-effort, safe to call repeatedly from service.py's own idle loop. Does nothing (not
    an error) when Chronicle isn't configured yet, when there's nothing new, when a scan was
    already triggered too recently, or when this addon has no source folder assigned to it on
    this device at all."""
    client = ChronicleClient()
    if not client.is_scan_needed():
        return

    if _scan_recently_triggered():
        log.info('kodi_scan_signal: new content signalled, but a scan already ran within the '
                  'last {0} minutes -- skipping this cycle (see the onScanFinished cascade '
                  'this throttle exists for). Will retry next poll.'.format(
                  _MIN_SECONDS_BETWEEN_TRIGGERED_SCANS // 60))
        return

    directories = find_own_source_directories()
    if not directories:
        log.info('kodi_scan_signal: new content signalled, but no source folder on this '
                  'device is currently set to use this addon as its scraper -- nothing to '
                  'scan here. Acknowledging so this device stops being asked about it.')
        client.acknowledge_scan_needed()
        return

    log.info('kodi_scan_signal: new content signalled by Chronicle -- scanning {0} source '
              'folder(s) this addon is configured for on this device: {1}'.format(
              len(directories), directories))

    any_accepted = False
    for directory in directories:
        request = {
            'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.Scan',
            'params': {'directory': directory, 'showdialogs': False},
        }
        try:
            response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        except Exception as exc:
            log.warning('kodi_scan_signal: VideoLibrary.Scan({0}) call failed: {1}'.format(
                        directory, exc))
            continue
        if 'error' in response:
            log.warning('kodi_scan_signal: VideoLibrary.Scan({0}) rejected: {1}'.format(
                        directory, response['error']))
            continue
        any_accepted = True

    _mark_scan_triggered()

    # Only acknowledged if at least one scan was actually accepted -- if every directory
    # attempt failed (e.g. a share that's briefly offline), leave the signal due so the next
    # poll retries rather than silently giving up on it. The throttle above still applies
    # either way, so a persistently-failing device can't hammer VideoLibrary.Scan every cycle.
    if any_accepted:
        client.acknowledge_scan_needed()


_STARTUP_FOLLOWUP_MARKER_PATH = 'special://temp/chronicle_scraper/startup_followup_claimed.txt'
_STARTUP_FOLLOWUP_REACHABILITY_TIMEOUT_SECONDS = 180
_STARTUP_FOLLOWUP_REACHABILITY_POLL_SECONDS = 5
_STARTUP_FOLLOWUP_REACHABILITY_CHECK_TIMEOUT_SECONDS = 10


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


def _read_startup_followup_marker():
    if not xbmcvfs.exists(_STARTUP_FOLLOWUP_MARKER_PATH):
        return None
    try:
        f = xbmcvfs.File(_STARTUP_FOLLOWUP_MARKER_PATH, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
        return float(raw.decode('utf-8') or '0')
    except Exception as exc:
        # Deliberately logged (unlike a merely-missing file, handled above) -- a marker that
        # exists but can't be read is indistinguishable from "never claimed" to the caller,
        # silently breaking the once-per-boot guarantee (every service re-claims and re-fires)
        # with nothing in the log to explain why. See _claim_startup_followup's own except for
        # the write-side counterpart of this same logging.
        log.warning('kodi_scan_signal: could not read startup-followup claim marker: {0}'.format(exc))
        return None


def _claim_startup_followup():
    folder = _STARTUP_FOLLOWUP_MARKER_PATH.rsplit('/', 1)[0] + '/'
    try:
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        f = xbmcvfs.File(_STARTUP_FOLLOWUP_MARKER_PATH, 'w')
        try:
            f.write(bytearray(str(time.time()), 'utf-8'))
        finally:
            f.close()
    except Exception as exc:
        log.warning('kodi_scan_signal: could not write startup-followup claim marker: {0}'.format(exc))


def _try_claim_startup_followup(service_started_at):
    """True if THIS call should be the one to run the followup scan for this boot -- see this
    module's own doc for why a rare double-claim (both addons happen to race this) is an
    accepted, harmless outcome rather than something worth real locking for."""
    claimed_at = _read_startup_followup_marker()
    if claimed_at is not None and claimed_at >= service_started_at:
        return False
    _claim_startup_followup()
    return True


def _is_directory_reachable(directory):
    """xbmcvfs.exists() on an unresponsive share doesn't raise or time out on its own -- the
    exact same hazard lib/movie_art_sync.py's listdir_with_timeout() was built to work around
    for xbmcvfs.listdir() (confirmed there directly via kodi.log + a live JSONRPC.Ping proving
    Kodi's own core was otherwise fully responsive). Duplicated here rather than imported --
    movie_art_sync.py is movie-addon-only and this module is shared with the TV addon.

    Runs the real call in a background daemon thread and treats a timeout as "not reachable
    right now" (not an error) -- there's no way to cancel a blocked native VFS call from
    Python, so the thread is simply abandoned rather than waited on further."""
    result = {}

    def _run():
        try:
            result['value'] = xbmcvfs.exists(directory)
        except Exception as exc:
            result['error'] = exc

    thread = threading.Thread(target=_run, name='ChronicleReachabilityCheck', daemon=True)
    thread.start()
    thread.join(_STARTUP_FOLLOWUP_REACHABILITY_CHECK_TIMEOUT_SECONDS)
    if thread.is_alive():
        return False
    return bool(result.get('value'))


def _wait_for_reachable(directories, is_aborted):
    deadline = time.time() + _STARTUP_FOLLOWUP_REACHABILITY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if is_aborted():
            log.info('kodi_scan_signal: startup-scan followup -- Kodi is shutting down, '
                      'abandoning the reachability wait')
            return False
        if all(_is_directory_reachable(d) for d in directories):
            return True
        time.sleep(_STARTUP_FOLLOWUP_REACHABILITY_POLL_SECONDS)
    log.warning('kodi_scan_signal: startup-scan followup -- source folder(s) still not reachable '
                'after {0}s, proceeding anyway'.format(_STARTUP_FOLLOWUP_REACHABILITY_TIMEOUT_SECONDS))
    return False


def run_startup_scan_followup(service_started_at, is_aborted=None):
    """Best-effort, safe to call once from service.py shortly after startup -- see this
    module's own doc (Startup-scan followup) for the full design. Does nothing (not an error)
    when Kodi's native "Update library on startup" setting is off or unreadable, when a
    sibling Chronicle Scraper addon has already claimed and fired this boot's followup, or when
    a library scan (native or a sibling's) is already in progress by the time this is ready to
    fire. is_aborted (e.g. an xbmc.Monitor's abortRequested) lets a caller cut the reachability
    wait short on Kodi shutdown; defaults to "never aborted" for a caller that doesn't have one
    (e.g. a direct test call)."""
    is_aborted = is_aborted or (lambda: False)
    try:
        if _read_native_startup_scan_setting() is not True:
            return

        # Always waits for THIS addon's own folder first, regardless of who ends up claiming --
        # see this module's own doc for why claiming before waiting would let the loser's own
        # folder skip the reachability check entirely even though the eventual scan covers it.
        directories = find_own_source_directories()
        if directories:
            _wait_for_reachable(directories, is_aborted)
        else:
            log.info('kodi_scan_signal: startup-scan followup -- no source folder on this '
                      'device is set to use this addon as its scraper; proceeding without a '
                      'reachability check of its own (another installed Chronicle Scraper '
                      'addon may still need one of its own folders to come up first)')

        if is_aborted():
            return

        if not _try_claim_startup_followup(service_started_at):
            log.info('kodi_scan_signal: startup-scan followup already claimed by the sibling '
                      'Chronicle Scraper addon this boot -- skipping')
            return

        if xbmc.getCondVisibility('Library.IsScanning'):
            log.info('kodi_scan_signal: startup-scan followup -- a library scan (Kodi\'s own '
                      'native one, or one a sibling addon just fired) is already running; '
                      'skipping rather than firing a second, fully overlapping one')
            return

        log.info('kodi_scan_signal: startup-scan followup -- Kodi\'s own "Update library on '
                  'startup" is on, firing a full library scan now (matching manual-scan '
                  'semantics) to make up for its own too-early, shallow native pass')
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.Scan',
                   'params': {'showdialogs': False}}
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        if 'error' in response:
            log.warning('kodi_scan_signal: startup-scan followup -- VideoLibrary.Scan '
                        'rejected: {0}'.format(response['error']))
            return

        # Feeds check_and_scan()'s own throttle so a device with scan_signal_enabled on
        # doesn't fire a SECOND unscoped scan moments later on top of this one -- see this
        # module's own doc for the cascade cost that would double.
        _mark_scan_triggered()
    except Exception as exc:
        log.warning('kodi_scan_signal: startup-scan followup failed: {0}'.format(exc))


## Per-item refresh-push signal (check_and_refresh) -- 2026-09-18
#
# Per-user direction: Kodi already has already-scraped metadata go stale with no way to refresh
# it short of a manual per-item "Refresh information" click. Chronicle can't call a device's
# JSON-RPC directly (same reasoning as this module's own scan-signal above), so this is the same
# pull architecture applied to per-item refreshes instead of a whole-library scan: this device
# polls Chronicle's own kodi-refresh-signal for items it already knows about (by kind) whose own
# metadata has changed since this device last scraped them, and fires one local
# VideoLibrary.Refresh* per item. See Chronicle's own IKodiDeviceService.GetItemsNeedingRefreshAsync
# doc for the full design, including why there's no separate acknowledgement call here.
#
# Each addon polls with only the kind(s) it actually owns (Movies: "movie"; TV: "episode",
# "tvshow") -- per-user direction, controlled by each addon's OWN settings, the same way
# scan_signal_enabled/scan_signal_interval_minutes already are, not a single shared toggle.

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
    as check_and_scan()'s own top-level guard, since this runs unattended on a timer."""
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
