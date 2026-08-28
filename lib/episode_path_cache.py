# -*- coding: utf-8 -*-
"""Caches each episode's own real file path, keyed by (tvshowid, season,
episode), for the brief window between nfo_rebuild.py issuing
VideoLibrary.RefreshEpisode() for it and tvshow_scraper.py's
get_episode_details() callback running for that same episode.

Why this needs to exist: confirmed live via kodi.log (2026-08-28) that
lib/tvshow_location.py's get_episode() -- which asks Kodi's own
VideoLibrary.GetEpisodes(tvshowid) for this exact episode's file path --
comes back empty for essentially every episode processed during a rebuild
pass, even though nfo_rebuild.py's own earlier, full-library
VideoLibrary.GetEpisodes call (taken before any refresh was issued) found a
real file for every one of them. The file was never actually missing on
disk; what's missing is timing. VideoLibrary.RefreshEpisode() re-scrapes an
already-library episode the same way Kodi handles any forced item refresh:
the episode's own library row is torn down and only recommitted once
get_episode_details() finishes (setResolvedUrl() + endOfDirectory()) -- so
asking VideoLibrary about this exact episode FROM WITHIN that same callback,
before it has returned, is asking about a row that, by construction, cannot
exist yet. No retry or delay fixes this: it isn't a matter of waiting long
enough, the row will not exist until this very call chain finishes. Compare
lib/movie_art_sync.py's find_movie_location(), which sidesteps the
equivalent movie-side problem with a filesystem source-browsing fallback
that never touches VideoLibrary state at all -- episodes have no equivalent
of their own (no per-episode fuzzy matching is normally needed once the
show's own folder is known; see tvshow_location.py's module docstring), so
instead this stashes the already-known-good path nfo_rebuild.py had BEFORE
it issued the refresh, and hands it back the one time it's asked for.

Same special://temp/ rationale as legacy_nfo.py/rebuild_state.py: the write
side (nfo_rebuild.py's issue loop) always runs from the movie addon's
process, but the read side (get_episode_details()) can run from either
addon's process depending on item type, and Chronicle Scraper is split into
two separate addon packages (script.chronicle.scraper.movie,
script.chronicle.scraper.tv) with two separate, unrelated addon_data
folders -- special://temp/ is the one location both can read and write
without depending on the other's addon_data folder existing.

One-shot by design, same as legacy_nfo.py: load_and_clear() deletes the
entry the moment it's consumed, so a stale path from an earlier rebuild can
never be replayed against a since-moved or since-renamed file.
"""

import re

import xbmcvfs

from lib.logger import Logger

log = Logger('episode_path_cache')

_CACHE_DIR = 'special://temp/chronicle_scraper/episode_path_cache/'

_SAFE_KEY_RE = re.compile(r'[^A-Za-z0-9._-]+')


def _cache_path(tvshowid, season, episode):
    key = '{0}_{1}_{2}'.format(tvshowid, season, episode)
    return _CACHE_DIR + _SAFE_KEY_RE.sub('_', key) + '.path'


def save(tvshowid, season, episode, file_path):
    """Called by nfo_rebuild.py right before it issues
    VideoLibrary.RefreshEpisode() for this episode -- file_path is whatever
    nfo_rebuild.py's OWN earlier, pre-refresh VideoLibrary.GetEpisodes call
    already found for it: a real, already-committed value, since Kodi is not
    yet in the middle of tearing this episode's row down at the moment this
    runs. Best-effort and silent on failure -- a failure here just means
    get_episode_details() falls back to whatever its own (racy) live
    VideoLibrary lookup returns for this one episode, same as before this
    cache existed."""
    if tvshowid is None or season is None or episode is None or not file_path:
        return
    try:
        if not xbmcvfs.exists(_CACHE_DIR):
            xbmcvfs.mkdirs(_CACHE_DIR)
        f = xbmcvfs.File(_cache_path(tvshowid, season, episode), 'w')
        try:
            f.write(bytearray(file_path.encode('utf-8')))
        finally:
            f.close()
    except Exception as exc:
        log.warning('Could not cache file path for tvshowid={0} S{1}E{2}: {3}'.format(
                    tvshowid, season, episode, exc))


def load_and_clear(tvshowid, season, episode):
    """Returns the cached file path for this (tvshowid, season, episode) and
    deletes the entry (one-shot -- see module docstring), or None if nothing
    is cached for it (e.g. this isn't a rebuild pass, or nfo_rebuild.py had
    no file for this episode either)."""
    if tvshowid is None or season is None or episode is None:
        return None
    path = _cache_path(tvshowid, season, episode)
    if not xbmcvfs.exists(path):
        return None

    file_path = None
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = bytes(f.readBytes()).decode('utf-8')
            file_path = raw or None
        finally:
            f.close()
    except Exception as exc:
        log.warning('Could not read cached file path for tvshowid={0} S{1}E{2}: {3}'.format(
                    tvshowid, season, episode, exc))

    try:
        xbmcvfs.delete(path)
    except Exception as exc:
        log.warning("Couldn't delete consumed path-cache entry {0}: {1}".format(path, exc))

    return file_path
