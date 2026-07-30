# -*- coding: utf-8 -*-
"""Keeps Kodi's local "Movie set information folder" in sync with Chronicle's
own collection artwork.

Why this exists: Kodi checks that local folder for a per-set poster/fanart
BEFORE it ever looks at anything a scraper contributes via addAvailableArtwork,
and uses a local file if one is there -- even a broken reference to a file
that no longer exists, which just renders as a blank card instead of falling
back to the scraper's art. Confirmed by reading Kodi's own MyVideos*.db
directly: multiple sets pointed at a "Movie Collections" folder that had been
moved/renamed on the NAS, leaving them with no usable art at all despite
Chronicle having a perfectly good poster.

The base folder itself (videolibrary.moviesetsfolder) is different per
install and per user's own NAS layout, so it's always read live from Kodi's
own Settings API (see kodi_settings.py) -- never hardcoded here.

Deliberately conservative: this only fills in a MISSING poster/fanart for a
set. It never overwrites a file that's already there, so it won't clobber
artwork a user has manually curated (several of this addon's own test sets
already have hand-picked banner/clearart/clearlogo/disc art alongside the
poster -- none of that is touched).
"""

import json
import re
import time
import urllib.request
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from lib.logger import Logger
from lib.kodi_settings import get_setting_value

log = Logger('collection_sync')

_ART_FILES = (
    ('poster.jpg', 'posterUrl'),
    ('fanart.jpg', 'backdropUrl'),
)

# Every local-art filename Kodi recognises for a movie set, and which "art"
# type each maps to -- used only by the stale-art repair below, not by the
# existing fill-missing logic above (which only ever handles poster/fanart).
_SET_ART_FILENAMES = {
    'poster.jpg': 'poster', 'fanart.jpg': 'fanart', 'banner.jpg': 'banner',
    'clearlogo.png': 'clearlogo', 'logo.png': 'logo', 'clearart.png': 'clearart',
    'discart.png': 'discart', 'disc.png': 'discart', 'landscape.jpg': 'landscape',
    'thumb.jpg': 'thumb',
}

# Every movie in a set gets scraped separately, and a single scan can touch
# many sets -- if the base folder itself is unreachable, that's one problem,
# not one-per-movie/set. Suppress repeat notifications within this window so
# a whole scan produces at most one alert. Each scraper invocation is a fresh
# process (no shared memory), so "once per scan" is approximated with a marker
# file timestamp rather than an in-memory flag; a scan quiet period longer
# than this is treated as a new scan and allowed to alert again.
_ALERT_SUPPRESS_SECONDS = 600


def sync_collection_art(collection):
    """collection is the same dict the /movies/details response returns under
    "collection" -- {id, name, overview, posterUrl, backdropUrl}. No-ops
    entirely if Kodi's movie-sets folder setting isn't configured; an empty
    setting is a deliberate choice, not something to warn about."""
    name = collection.get('name')
    if not name:
        return

    base = get_setting_value('videolibrary.moviesetsfolder')
    if not base:
        return

    folder = base.rstrip('/') + '/' + name + '/'

    _repair_stale_set_art(name, folder)

    if not xbmcvfs.exists(folder):
        base_readable = xbmcvfs.exists(base)
        mkdirs_ok = xbmcvfs.mkdirs(folder)
        log.info('Folder missing for "{0}": base_exists={1} folder={2} mkdirs_ok={3}'.format(
            name, base_readable, folder, mkdirs_ok))
        if not mkdirs_ok:
            _notify_unreachable(base)
            return
        log.info('Created missing movie set folder: {0}'.format(folder))

    for filename, field in _ART_FILES:
        url = collection.get(field)
        if not url:
            log.info('sync_collection_art: set "{0}" -- Chronicle has no {1}, nothing to fill'.format(
                name, field))
            continue
        dest = folder + filename
        if xbmcvfs.exists(dest):
            # This is the single most likely explanation for a set poster that never
            # updates: this module is deliberately fill-only (see module docstring),
            # so a stale/broken local file here silently wins forever with zero log
            # trace until now. If the set looks wrong, check this file manually --
            # it's what's actually being shown, not whatever Chronicle currently has.
            log.info('sync_collection_art: set "{0}" -- {1} already exists locally at {2}, '
                     'leaving it alone (this module never overwrites) -- Chronicle currently '
                     'has {3} for this field if that local file turns out to be stale'.format(
                     name, filename, dest, url))
            continue

        log.info('sync_collection_art: set "{0}" -- {1} missing locally, downloading {2} to {3}'.format(
            name, filename, url, dest))
        result = _write_remote_file(dest, url)
        if result == 'ok':
            log.info('Filled missing {0} for set "{1}" from Chronicle'.format(filename, name))
        elif result == 'write_failed':
            # Only an actual write failure implicates the folder itself -- a
            # download failure (dead/expired URL, upstream outage, etc.) says
            # nothing about whether the folder is writable, so it must not be
            # reported as the same problem.
            _notify_unreachable(base)
        # 'download_failed' already logged its own reason in _write_remote_file;
        # nothing further to do here, and definitely not a folder-writability signal.


def _repair_stale_set_art(name, folder):
    """Confirmed directly (2026-07-30): Kodi registers a movie set's art once,
    whenever it's first discovered, and never automatically re-checks it --
    if the "Movie set information folder" ever moves (e.g. during a NAS
    reorganisation), every set's registered art keeps pointing at the now-dead
    old location forever, rendering a blank card, even after this module has
    since successfully filled in a real file at the NEW location. Found ~110
    sets in exactly this state in one pass (registered against a share that
    no longer has a "Movie Collections" folder at all).

    For every art type Kodi has registered as a local file, verifies that
    file still exists; if not, either repoints it at the same-named file in
    the CURRENT set folder (if one exists there) or clears the stale
    reference entirely so Kodi stops trying to load a dead path -- per
    explicit instruction, a reference with nothing valid to replace it is
    deleted outright rather than left dangling."""
    setid, current_art = _find_set(name)
    if setid is None:
        return

    updates = {}
    for art_type, value in current_art.items():
        local_path = _decode_local_image_path(value)
        if local_path is None:
            continue  # a remote (http/https) URL, or not a scheme we handle -- nothing to verify
        if xbmcvfs.exists(local_path):
            continue  # still good, leave it alone

        replacement = None
        for filename, mapped_type in _SET_ART_FILENAMES.items():
            if mapped_type != art_type:
                continue
            candidate = folder + filename
            if xbmcvfs.exists(candidate):
                replacement = candidate
                break

        updates[art_type] = replacement or ''  # '' clears a dead reference

    if not updates:
        return

    for art_type, new_value in updates.items():
        if new_value:
            log.info('sync_collection_art: set "{0}" -- registered {1} no longer exists, '
                     'repointing to {2}'.format(name, art_type, new_value))
        else:
            log.info('sync_collection_art: set "{0}" -- registered {1} no longer exists and '
                     'no replacement found in the current folder, clearing the stale '
                     'reference'.format(name, art_type))

    _set_movie_set_art(setid, updates)


def _find_set(name):
    """Returns (setid, art_dict) for the Kodi movie set matching name, or
    (None, None) if not found."""
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovieSets',
        'params': {'properties': ['art']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't query VideoLibrary.GetMovieSets: {0}".format(exc))
        return None, None

    for s in response.get('result', {}).get('sets', []):
        if s.get('label') == name:
            return s.get('setid'), s.get('art') or {}
    return None, None


def _decode_local_image_path(image_url):
    """Kodi wraps a set's registered art as image://<url-encoded-path>/,
    whether the underlying reference is local or remote. Returns the plain
    smb:// path (credentials stripped, matching how the rest of this addon
    references shares) for a local file, or None for a remote http(s) URL or
    any other scheme this doesn't handle -- nothing to verify in that case."""
    if not image_url.startswith('image://'):
        return None
    inner = image_url[len('image://'):].rstrip('/')
    decoded = urllib.parse.unquote(inner)
    if not decoded.startswith('smb://'):
        return None
    return re.sub(r'^smb://[^@/]+@', 'smb://', decoded)


def _set_movie_set_art(setid, art):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetMovieSetDetails',
        'params': {'setid': setid, 'art': art},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't update set art for setid {0}: {1}".format(setid, exc))
        return
    if response.get('result') != 'OK':
        log.warning('SetMovieSetDetails for setid {0} returned unexpected response: {1}'.format(
            setid, response))


def _write_remote_file(dest_path, url):
    """Returns 'ok', 'download_failed', or 'write_failed' -- the caller needs to
    tell these apart, since only a write failure says anything about whether the
    destination folder itself is writable."""
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = resp.read()
    except Exception as exc:
        log.warning("Couldn't download {0}: {1}".format(url, exc))
        return 'download_failed'

    try:
        f = xbmcvfs.File(dest_path, 'w')
        try:
            written = f.write(bytearray(data))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write {0}: {1}: {2}".format(dest_path, type(exc).__name__, exc))
        return 'write_failed'

    # xbmcvfs.File.write() doesn't raise on most VFS failures (permission denied,
    # unreachable share) -- it just silently returns a falsy value, so that has
    # to be treated as a real failure, not just an exception.
    ok = bool(written) if written is not None else True
    if not ok:
        log.warning('xbmcvfs.File.write() returned falsy for {0} (no exception raised)'.format(dest_path))
        return 'write_failed'
    return 'ok'


def _notify_unreachable(base_folder):
    """Warns about the configured base folder only -- which set/movie triggered
    the check doesn't matter, since the problem is the folder itself, not any
    one title. Throttled to at most one popup per _ALERT_SUPPRESS_SECONDS so a
    whole scan (which can touch this same broken folder for every set it sees)
    doesn't produce a notification per movie."""
    log.warning('Movie set folder not writable: {0}'.format(base_folder))

    if not _should_alert():
        return

    xbmcgui.Dialog().notification(
        'Chronicle Scraper',
        'Movie collections folder not reachable: {0}'.format(base_folder),
        xbmcgui.NOTIFICATION_WARNING,
        7000,
    )


def _marker_path():
    profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not xbmcvfs.exists(profile):
        xbmcvfs.mkdirs(profile)
    return profile.rstrip('/\\') + '/collection_folder_alert.marker'


def _should_alert():
    path = _marker_path()
    now = time.time()

    if xbmcvfs.exists(path):
        try:
            f = xbmcvfs.File(path)
            try:
                contents = f.read()
            finally:
                f.close()
            last = float((contents or '0').strip())
        except Exception:
            last = 0
        if now - last < _ALERT_SUPPRESS_SECONDS:
            return False

    try:
        f = xbmcvfs.File(path, 'w')
        try:
            f.write(bytearray(str(now).encode('utf-8')))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write alert marker (will alert anyway): {0}".format(exc))

    return True
