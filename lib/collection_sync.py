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

Chronicle is authoritative here, the same way movie_art_sync.py already
treats it as authoritative for an individual movie's own poster/fanart:
whenever Chronicle has a candidate for a given art type, it overwrites
whatever local file is already there. This was previously fill-only (never
touch a file that's already there), specifically to protect hand-picked
art -- but that meant a wrong or stale local file (a mismatched-language
poster, an old pick from before a collection was corrected) could sit there
forever with no way for Chronicle's own, corrected answer to ever actually
reach Kodi: this dedicated folder is what Kodi reads on refresh, not
anything the scraper offers live via addAvailableArtwork. Confirmed
directly (2026-08-21) that this was hiding real fixes -- a collection
poster-language correction and a poster-fallback fix both landed in
Chronicle but never appeared in this folder, because every slot already had
*a* file sitting there, right or not.

pinnedSlots (a user's explicit pin in Chronicle) no longer changes the
write itself -- everything overwrites now -- but is still reported in the
log, since a pinned choice and an auto-resolved one are worth being able to
tell apart after the fact.

Overwriting a file isn't enough on its own: Kodi caches every image it has
loaded and only re-checks a local file's hash about once a day, so a replaced
poster.jpg would keep rendering the old picture until then. Each overwrite is
therefore followed by a Textures.RemoveTexture call for that path.
"""

import json
import re
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from lib import art_sync_cache
from lib.remote_file import write_remote_file
from lib.logger import Logger
from lib.kodi_settings import get_setting_value

log = Logger('collection_sync')

# (local filename, Chronicle field on the collection payload, Chronicle's
# canonical slot name, Kodi's art type). The slot name is what "pinnedSlots"
# reports, and the art type is what Kodi's own library calls the same image.
#
# Kodi accepts more set art than poster/fanart, and Chronicle now resolves all
# of it for collections, so every one of these is written -- previously only
# the first two were, which meant a collection's logo/banner/disc art existed
# in Chronicle and simply never reached Kodi.
_ART_FILES = (
    ('poster.jpg',    'posterUrl',    'poster_url',   'poster'),
    ('fanart.jpg',    'backdropUrl',  'backdrop_url', 'fanart'),
    ('banner.jpg',    'bannerUrl',    'banner_url',   'banner'),
    ('clearlogo.png', 'logoUrl',      'logo_url',     'clearlogo'),
    ('clearart.png',  'clearartUrl',  'clearart_url', 'clearart'),
    ('discart.png',   'discUrl',      'disc_url',     'discart'),
    ('thumb.jpg',     'thumbUrl',     'thumb_url',    'thumb'),
)

# Every local-art filename Kodi recognises for a movie set, and which "art"
# type each maps to -- used by the stale-art repair below, which has to
# recognise files this module never writes itself (logo.png, landscape.jpg)
# because a user may well have put them there by hand.
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
    setting is a deliberate choice, not something to warn about.

    Overwrites whatever local file is already there for every art type
    Chronicle currently has a candidate for -- see the module docstring for
    why this is no longer fill-only. Skips the download entirely when
    art_sync_cache already confirms the exact same URL is what's sitting
    there right now (lib/art_sync_cache.py)."""
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

    pinned = set(collection.get('pinnedSlots') or ())
    refreshed = {}

    for filename, field, slot, art_type in _ART_FILES:
        url = collection.get(field)
        if not url:
            log.info('sync_collection_art: set "{0}" -- Chronicle has no {1}, nothing to fill'.format(
                name, field))
            continue

        dest = folder + filename
        exists = xbmcvfs.exists(dest)
        is_pinned = slot in pinned

        if art_sync_cache.already_synced(dest, url):
            log.info('sync_collection_art: set "{0}" -- {1} already matches Chronicle\'s '
                     'current pick, skipping download'.format(name, filename))
            continue

        action = 'refreshing (pinned in Chronicle)' if (exists and is_pinned) \
            else 'refreshing existing' if exists else 'missing locally, downloading'
        log.info('sync_collection_art: set "{0}" -- {1} {2}: {3} -> {4}'.format(
            name, filename, action, url, dest))

        result = write_remote_file(dest, url)
        if result == 'ok':
            art_sync_cache.remember(dest, url)
            if exists:
                # Same path, new bytes: Kodi would keep serving the cached copy for up
                # to a day without this.
                _invalidate_texture(dest)
                refreshed[art_type] = dest
                log.info('Refreshed {0} for set "{1}" from Chronicle{2}'.format(
                    filename, name, ' (pinned)' if is_pinned else ''))
            else:
                log.info('Filled missing {0} for set "{1}" from Chronicle'.format(filename, name))
        elif result == 'write_failed':
            # Only an actual write failure implicates the folder itself -- a
            # download failure (dead/expired URL, upstream outage, etc.) says
            # nothing about whether the folder is writable, so it must not be
            # reported as the same problem.
            _notify_unreachable(base)
        # 'download_failed' already logged its own reason in write_remote_file;
        # nothing further to do here, and definitely not a folder-writability signal.

    if refreshed:
        # A set that already had art registered keeps pointing at the old cached
        # texture until it's re-registered, even though the file underneath changed.
        setid, _ = _find_set(name)
        if setid is not None:
            _set_movie_set_art(setid, refreshed)


def preserve_local_movieset_file(set_name, source_path, filename):
    """Copies a movie-folder-embedded "movieset-<type>" local art file (e.g.
    "movieset-poster.jpg", written by tinyMediaManager or another tool
    directly alongside a movie -- a separate, independent local-art
    convention from this module's own dedicated Movie Set Information
    folder) into that dedicated folder, before nfo_rebuild.py's bulk
    rebuild permanently deletes the original. Without this, that data was
    simply gone with no way back -- the same class of loss the movie NFO
    preservation (lib/legacy_nfo.py) fixes for .nfo files, applied to this
    other spot nfo_rebuild.py's own delete step also touches.

    Fill-only, same rule as sync_collection_art itself: only writes when
    the dedicated folder doesn't already have a file for this same slot,
    so a real (or user-pinned) image already there is never clobbered by
    an older file salvaged from one particular movie's own folder. Returns
    True if it copied something, False otherwise (nothing to salvage,
    the slot's already covered, or the dedicated folder isn't configured/
    writable)."""
    if not set_name or not filename.lower().startswith('movieset-'):
        return False

    base = get_setting_value('videolibrary.moviesetsfolder')
    if not base:
        return False

    dest_filename = filename[len('movieset-'):]
    folder = base.rstrip('/') + '/' + set_name + '/'
    dest = folder + dest_filename

    if xbmcvfs.exists(dest):
        return False

    if not xbmcvfs.exists(folder) and not xbmcvfs.mkdirs(folder):
        log.warning("Couldn't create set folder {0} to salvage {1}".format(folder, filename))
        return False

    try:
        f_in = xbmcvfs.File(source_path, 'r')
        try:
            data = f_in.readBytes()
        finally:
            f_in.close()
        f_out = xbmcvfs.File(dest, 'w')
        try:
            f_out.write(data)
        finally:
            f_out.close()
    except Exception as exc:
        log.warning("Couldn't salvage {0} to {1}: {2}".format(source_path, dest, exc))
        return False

    log.info('nfo_rebuild: salvaged local "{0}" for set "{1}" into the dedicated set folder '
             'before deleting the movie-folder copy'.format(filename, set_name))
    return True


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


def _invalidate_texture(path):
    """Drops Kodi's cached copy of an image so a replaced file at the same path
    is actually re-read.

    Kodi's texture cache is keyed by path, and for a local file it only
    re-hashes on a roughly daily interval -- so overwriting poster.jpg leaves
    the old picture on screen until that check happens to come round. Removing
    the cache entry forces a re-read on next display.

    Best-effort throughout: a miss here costs a stale thumbnail, never correct
    artwork, so nothing about it should interrupt the sync."""
    # Kodi may have cached the path with credentials in it, or url-encoded
    # inside an image:// wrapper; match on the bare filename to catch those,
    # then confirm the full path below before removing anything.
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'Textures.GetTextures',
        'params': {
            'filter': {'field': 'url', 'operator': 'contains',
                       'value': path.rsplit('/', 1)[-1]},
            'properties': ['url'],
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't query Textures.GetTextures for {0}: {1}".format(path, exc))
        return

    removed = 0
    for texture in response.get('result', {}).get('textures', []):
        url = texture.get('url') or ''
        decoded = urllib.parse.unquote(url)
        # Filename-contains is a broad filter on purpose (see above); confirm the
        # full path really is this file before removing anyone else's texture.
        if path not in decoded and path not in url:
            continue
        remove = {
            'jsonrpc': '2.0', 'id': 1, 'method': 'Textures.RemoveTexture',
            'params': {'textureid': texture.get('textureid')},
        }
        try:
            xbmc.executeJSONRPC(json.dumps(remove))
            removed += 1
        except Exception as exc:
            log.warning("Couldn't remove cached texture {0}: {1}".format(
                texture.get('textureid'), exc))

    log.info('Invalidated {0} cached texture(s) for {1}'.format(removed, path))


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
