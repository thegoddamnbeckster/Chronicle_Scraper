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
"""

import glob
import json
import os
import sqlite3
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
