# -*- coding: utf-8 -*-
"""Keeps a movie's own local artwork files (in its own folder on disk) in sync
with Chronicle's current pick.

Why this exists: confirmed by exhaustively reading Kodi's entire Settings API
(all 318 settings, including expert level) that there is NO toggle anywhere
for "prefer online artwork" at the video-library level -- only the music
library has one (musiclibrary.preferonlinealbumart). A movie's own local
files (e.g. "<Movie Folder>-poster.jpg" sitting next to the video file) are
used unconditionally, the same way Kodi discovered local NFO files before
scrapers existed. This is core, un-overridable Kodi behaviour, not a bug in
this addon and not something addAvailableArtwork()/setArt() can beat.

Confirmed twice, directly: a movie's poster reverted to its old local file
with NO corresponding getdetails() call in kodi.log anywhere nearby -- i.e.
Kodi re-applies local files on its own schedule, independent of whether the
scraper ran again. setArt() alone cannot survive that; the only fix that
actually holds up is making the local file agree with Chronicle's pick, since
then it no longer matters how many times Kodi re-checks it.

Unlike collection_sync.py (which only FILLS IN a missing set poster/fanart
and never touches a file that's already there, because that folder structure
is usually hand-curated), this module deliberately OVERWRITES whatever local
poster/fanart file already exists for the movie -- the whole point is fixing
stale local files left over from a previous scraper, not preserving them.

Finding the movie's folder, cross-platform:
The scraper's own find/getdetails contract never gives us the movie's file
path on any channel -- confirmed directly with a diagnostic build that logged
the real querystring params, cwd, and environment Kodi hands the script: no
path/file field ever appears, cwd is just wherever Kodi's Python interpreter
happens to run from, and the only Kodi-set env vars are install-level. This
is Kodi's own C++ core invoking the scraper the same way on every platform,
so the absence is not a Windows-specific gap -- it's true everywhere Kodi runs.

The first thing tried is Kodi's own VideoLibrary via JSON-RPC (cheap, instant,
correct for anything already in the library). But for a movie being scraped
for the very first time, VideoLibrary won't have it yet -- confirmed directly
and repeatedly (kodi.log) that Kodi hasn't committed a brand-new item at the
exact moment getdetails() runs, badly enough that even a bounded retry
(3 attempts, 1.5s apart) still missed several real movies during an active
scan. getartwork was hoped to catch this on a later pass; confirmed directly
that it does NOT fire automatically after getdetails, so it isn't a safety net.

The fallback -- and the one that actually holds up regardless of the commit
race -- browses Kodi's own configured video sources directly via
Files.GetSources + xbmcvfs.listdir(), matching the folder name against the
title/year. This never depends on library state at all, only on the file
already existing on disk, which it does by the time getdetails() runs. Both
JSON-RPC methods used (Files.GetSources, VideoLibrary.GetMovies) and both VFS
calls (xbmcvfs.listdir, xbmcvfs.File) are Kodi's own cross-platform
abstractions -- identical behaviour on Windows, Linux, macOS, Android, or any
other platform Kodi runs on, including a Kodi-side multipath:// virtual
source that bundles several real folders into one browsable source (Kodi's
own VFS handles this transparently; nothing multipath-specific is done here).
"""

import json
import posixpath
import re
import threading
import time
from urllib.parse import unquote

import xbmc
import xbmcvfs

from lib.logger import Logger

log = Logger('movie_art_sync')

# Bound on how long a single xbmcvfs.listdir() call is allowed to run -- see
# listdir_with_timeout() for why this exists. Generous enough for a slow but
# working SMB share; short enough that one unresponsive share out of several
# doesn't stall an entire scrape.
_LISTDIR_TIMEOUT_SECONDS = 8

_ART_FILES = (
    ('poster', 'jpg'),
    ('fanart', 'jpg'),
)

# Retry budget for the VideoLibrary fast path -- see module docstring. Kept
# short and early-exiting on success, since a movie already in the library
# (the common case on any scan after the very first) resolves on attempt one
# with zero added delay; this only costs time for something genuinely new.
_LOOKUP_RETRIES = 2
_LOOKUP_RETRY_DELAY_SECONDS = 1.0

# Video file extensions recognised when confirming a matched folder really
# holds a video (not, say, a same-named TV show folder or an empty stub).
_VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.m2ts', '.wmv', '.iso')


def sync_movie_art(title, year, artwork, location=None):
    """artwork is the same dict ScraperController's /movies/details returns --
    {arttype: [{url, source}, ...]}. Overwrites the movie's own local
    poster/fanart files (if its folder can be found) with Chronicle's first
    (authoritative) candidate for each type.

    location, if given, is a pre-resolved (folder, video_basename) tuple from
    find_movie_location() -- pass this when the caller already looked the
    movie up for another reason (e.g. also writing an NFO) so this doesn't
    repeat the same source-browsing/listdir work a second time."""
    if not artwork:
        log.warning('sync_movie_art: "{0}" ({1}) -- Chronicle sent no artwork dict at all, '
                    'nothing to sync'.format(title, year))
        return

    folder, _video_basename = location if location else find_movie_location(title, year)
    if not folder:
        return

    folder_name = posixpath.basename(folder.rstrip('/'))
    if not folder_name:
        log.warning('sync_movie_art: "{0}" ({1}) -- matched folder {2!r} has no usable name, '
                    'skipping'.format(title, year, folder))
        return

    for art_type, ext in _ART_FILES:
        candidates = artwork.get(art_type)
        if not candidates:
            log.info('sync_movie_art: "{0}" ({1}) -- Chronicle has no {2} candidate, '
                     'leaving local file (if any) untouched'.format(title, year, art_type))
            continue
        url = candidates[0]['url']
        dest = '{0}{1}-{2}.{3}'.format(folder, folder_name, art_type, ext)
        log.info('sync_movie_art: "{0}" ({1}) -- writing {2} from {3} to {4}'.format(
            title, year, art_type, url, dest))
        if _write_remote_file(dest, url):
            log.info('Synced local {0} for "{1}" from Chronicle'.format(art_type, title))


def find_movie_location(title, year):
    """Returns (folder, video_basename) or (None, None). folder is the movie's
    own folder path (trailing slash); video_basename is the real video file's
    own name with its extension stripped, when known -- this is what Kodi
    actually expects a local NFO to be named to take highest precedence
    (a bare 'movie.nfo' is also valid but loses to a real <video-name>.nfo
    that another tool, e.g. tinyMediaManager, may have already left behind
    under the true video filename), so callers writing an NFO need this, not
    just the folder name movie_art_sync itself is content with for images.

    Tries the VideoLibrary fast path first, then falls back to browsing
    Kodi's own configured video sources directly -- see module docstring for
    why both exist and why the fallback is the one that's actually reliable."""
    file_path = _lookup_via_video_library(title, year)
    if file_path:
        folder = posixpath.dirname(file_path) + '/'
        return folder, _strip_video_ext(posixpath.basename(file_path))

    result = _search_sources_for_movie(title, year)
    if result:
        folder, video_name = result
        return folder, (_strip_video_ext(video_name) if video_name else None)

    log.info('No folder found for {0!r} ({1}) via VideoLibrary or source browsing -- '
             'will not sync local art/NFO this pass'.format(title, year))
    return None, None


def _strip_video_ext(filename):
    if not filename:
        return None
    lower = filename.lower()
    for ext in _VIDEO_EXTENSIONS:
        if lower.endswith(ext):
            return filename[:-len(ext)]
    return filename


def _lookup_via_video_library(title, year):
    for attempt in range(1, _LOOKUP_RETRIES + 1):
        file_path = _lookup_movie_file(title, year)
        if file_path:
            return file_path
        if attempt < _LOOKUP_RETRIES:
            time.sleep(_LOOKUP_RETRY_DELAY_SECONDS)
    return None


def _lookup_movie_file(title, year):
    """Single VideoLibrary attempt -- returns the file path, or None.

    Confirmed directly (2026-07-30) that this fast path is its own separate
    exposure to the exact same class of bug the slow path's weak fallback
    caused: Kodi's VideoLibrary.GetMovies title filter trusts whatever title
    is CURRENTLY STORED for a library entry, with zero connection to that
    entry's real file/folder. If that stored title is itself wrong (from an
    earlier bad match, a stale NFO Kodi re-read, or anything else), this
    returns a real file path -- so movie_art_sync writes correct-looking
    Chronicle data into the WRONG movie's folder just as confidently as the
    slow path's old startswith fallback did. The fix mirrors the slow path's:
    verify the returned file's own containing folder actually matches the
    searched title (+year, when known) before trusting it at all."""
    request = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'VideoLibrary.GetMovies',
        'params': {
            'filter': {'field': 'title', 'operator': 'is', 'value': title},
            'properties': ['file', 'year'],
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't query VideoLibrary for {0!r}: {1}".format(title, exc))
        return None

    if 'error' in response:
        log.warning('VideoLibrary.GetMovies rejected title={0!r}: {1}'.format(title, response['error']))
        return None

    movies = response.get('result', {}).get('movies') or []
    if not movies:
        return None

    candidate = None
    if year:
        for movie in movies:
            if movie.get('year') == year:
                candidate = movie
                break
    if candidate is None:
        candidate = movies[0]

    file_path = candidate.get('file')
    if not file_path:
        return None

    folder_name = posixpath.basename(posixpath.dirname(file_path).rstrip('/'))
    target = _normalize(title)
    target_with_year = target + str(year) if year else None
    folder_norm = _normalize(folder_name)
    matches = (target_with_year and folder_norm == target_with_year) or \
              (not target_with_year and folder_norm == target)
    if not matches:
        log.warning('VideoLibrary lookup for {0!r} ({1}) returned {2!r} -- folder name doesn\'t '
                    'match the searched title, Kodi\'s own stored title for this entry is '
                    'likely wrong; refusing to trust it, falling back to source browsing '
                    'instead'.format(title, year, file_path))
        return None

    return file_path


def _normalize(text):
    """Lowercases and strips everything but letters/digits, so folder-naming
    variations (colons, periods, apostrophes, ampersands, spacing) don't
    prevent a real match -- e.g. "Mar.IA" and "Mar IA" both normalize to
    "maria"."""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _get_video_sources():
    """Every configured video source path, via Kodi's own cross-platform
    Files.GetSources -- with any multipath:// virtual source (bundling several
    real shares into one browsable "Movies" entry, confirmed to be exactly how
    this user's sources are set up) expanded into its real constituent paths.
    xbmcvfs.listdir() browses a multipath:// bundle for LISTING transparently,
    but a folder name found that way can't be re-appended onto the raw
    multipath:// string afterward -- see _expand_multipath() for why. Expanding
    here means every path this returns is a genuine browsable location on its
    own, so nothing downstream has to know multipath sources exist at all.

    Sources aren't filtered to "movies content" specifically (that's per-path
    scraper config, not exposed this way) -- searching a TV/music source too
    just costs a little time, never breaks anything."""
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'Files.GetSources', 'params': {'media': 'video'}}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't get video sources: {0}".format(exc))
        return []

    sources = []
    for entry in response.get('result', {}).get('sources', []):
        path = entry.get('file')
        # addons://sources/video/ etc. aren't real browsable folders.
        if path and '://' in path and not path.startswith('addons://'):
            sources.extend(_expand_multipath(path))
    return sources


def _expand_multipath(source):
    """Splits a multipath:// bundle (e.g. "multipath://smb%3a%2f%2f.../.../")
    into its real underlying path strings. Confirmed directly (kodi.log) that
    doing folder-name concatenation directly on the raw multipath:// string
    instead of one of its real constituent paths produces a hard
    "XFILE::CDirectory::GetDirectory - Error getting <name>" from Kodi's own
    core with no Python-catchable exception -- xbmcvfs.listdir() on the bad
    path just silently returns nothing, which is why this previously looked
    like "no folder found" for every movie living under a multipath source
    that the VideoLibrary-lookup fast path missed. multipath:// isn't a real
    browsable location itself, just a container Kodi's own GetSources/listdir
    happen to browse transparently -- appending a suffix to the *container*
    rather than to one of the real paths inside it is what broke.
    Returns [source] unchanged for anything that isn't a multipath:// URL."""
    prefix = 'multipath://'
    if not source.startswith(prefix):
        return [source]
    body = source[len(prefix):]
    return [unquote(segment) for segment in body.split('/') if segment]


def listdir_with_timeout(path, timeout_seconds=_LISTDIR_TIMEOUT_SECONDS):
    """xbmcvfs.listdir() on a share that's gone unresponsive doesn't raise or
    time out on its own -- confirmed directly (kodi.log + a live JSONRPC.Ping
    proving Kodi's own core was otherwise fully responsive) that a single
    unresponsive SMB path can block the calling thread indefinitely, with zero
    further log output, for as long as that share stays down. Expanding
    multipath sources into several real paths (see _expand_multipath) made
    this worse, not better -- one bad share among several now means several
    separate opportunities to hang instead of one.

    Runs the real listdir() call in a background daemon thread and gives up
    waiting after timeout_seconds. If it times out, that background thread is
    simply abandoned (still blocked, harmless, cleaned up whenever Kodi's
    Python interpreter for this invocation eventually exits) -- there is no
    way to forcibly cancel a blocked native VFS call from Python, so moving on
    without it is the only option that doesn't just relocate the hang.

    Returns (dirs, files) on success, or (None, None) on error or timeout.
    """
    result = {}

    def _run():
        try:
            result['value'] = xbmcvfs.listdir(path)
        except Exception as exc:
            result['error'] = exc

    thread = threading.Thread(target=_run, name='ChronicleListdir', daemon=True)
    thread.start()
    thread.join(timeout_seconds)

    if thread.is_alive():
        log.warning('Timed out after {0}s listing {1} -- share may be unresponsive, '
                    'moving on without it'.format(timeout_seconds, path))
        return None, None
    if 'error' in result:
        log.warning("Couldn't list {0}: {1}".format(path, result['error']))
        return None, None
    return result['value']


def _search_sources_for_movie(title, year):
    """Returns (folder, video_filename) for the first matching folder that
    actually holds a video file, or None.

    Requires an EXACT normalized match (title+year if year is known,
    otherwise title alone) -- confirmed directly (2026-07-30, live kodi.log)
    that the previous "starts with" weak fallback caused real, silent data
    corruption: "Alien" (1979) matched "Alien Romulus (2024)" -- a
    *completely different* movie -- because that folder's normalized name
    happened to start with "alien", and this function returns on the FIRST
    source that yields ANY match without checking whether a later source has
    the real one. Since movie_art_sync overwrites unconditionally, that wrote
    Alien's poster/fanart/NFO directly over Alien Romulus's own, correct
    files. A prefix match between two different franchise entries (X / X-Men,
    It / It Follows, Die Hard / Die Hard - With a Vengeance, and many more --
    confirmed via a full-log scan, not a one-off) is common enough that this
    fallback was actively dangerous, not just occasionally wrong. Skipping
    the sync entirely (logged clearly by the caller) is always safer than a
    silent wrong-folder write with no indication anything went wrong.

    Every source is checked before giving up, rather than stopping at the
    first source with no match -- the previous per-source-only exact check
    already had this same "wrong source checked first" exposure even before
    the weak fallback is considered.
    """
    target = _normalize(title)
    if not target:
        return None
    target_with_year = target + str(year) if year else None

    for source in _get_video_sources():
        dirs, _files = listdir_with_timeout(source)
        if dirs is None:
            continue

        best = None
        for name in dirs:
            normalized = _normalize(name)
            if target_with_year and normalized == target_with_year:
                best = name
                break  # exact title+year match -- can't do better than this
            if best is None and not target_with_year and normalized == target:
                best = name  # no year to disambiguate with -- exact title match only

        if not best:
            continue

        folder = source.rstrip('/') + '/' + best + '/'
        video_name = _find_video_filename(folder)
        if video_name:
            return folder, video_name

    return None


def _find_video_filename(folder):
    """Returns the first video file's name found directly in folder, or None
    -- also confirms a matched folder actually holds a video (not just a
    same-named folder for something else, e.g. a TV show sharing a movie's
    title)."""
    _dirs, files = listdir_with_timeout(folder)
    if files is None:
        return None
    for f in files:
        if f.lower().endswith(_VIDEO_EXTENSIONS):
            return f
    return None


def _write_remote_file(dest_path, url):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = resp.read()
    except Exception as exc:
        log.warning("Couldn't download {0}: {1}".format(url, exc))
        return False

    try:
        f = xbmcvfs.File(dest_path, 'w')
        try:
            f.write(bytearray(data))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write {0}: {1}".format(dest_path, exc))
        return False

    return True
