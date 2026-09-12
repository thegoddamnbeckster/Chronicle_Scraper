# -*- coding: utf-8 -*-
"""Finds a TV show's own root folder on disk, and an episode's own file
within it -- the TV-side equivalent of movie_art_sync.py's
find_movie_location(), reusing its shared building blocks (video source
listing, title normalization, year-tolerant matching, timeout-guarded
listdir) rather than re-deriving them, since those were each hard-won
against real bugs on the movie side (see movie_art_sync.py's own module
docstring and git history) and the same failure modes apply here too.

Why a show's folder needs finding the same way a movie's does: Kodi's
find/getdetails contract hands this scraper no path information on any
channel, confirmed for movies via a diagnostic build (movie_art_sync.py's
own docstring) and true for the identical C++ core invocation on the TV
side too.

Episodes are simpler once the show itself is found, though: unlike a movie
(identified only by title+year, sometimes ambiguous), an episode is
identified by an exact (season, episode) pair that Kodi's own VideoLibrary
already indexes precisely once the show exists there -- so no equivalent
of the movie side's fuzzy folder-name matching or source-browsing fallback
is needed for episodes. VideoLibrary.GetEpisodes is the one and only path,
and the same call doubles as the source for Kodi's own streamdetails for
that file.

Also duplicated verbatim into the MOVIE addon's own lib/ (same reasoning as
chronicle_client.py's duplication) -- nfo_rebuild.py lives there and needs
these same two functions to resolve TV rebuild-queue items. If you fix a bug
here, fix it in both copies.
"""

import json
import posixpath
import re
import time

import xbmc
import xbmcvfs

from lib.logger import Logger
from lib.movie_art_sync import get_video_sources, listdir_with_timeout, normalize, year_tolerant_match

log = Logger('tvshow_location')

_LOOKUP_RETRIES = 2
_LOOKUP_RETRY_DELAY_SECONDS = 1.0

# Root-caused live (2026-09-12): find_show_location() runs unconditionally on EVERY
# get_episode_details() call, re-resolving the exact same (folder, tvshowid) pair for every
# single episode of a show -- 38 identical VideoLibrary.GetTVShows round-trips (each with its
# own retry-with-sleep on a miss) for one 38-episode show, all asking Kodi's own JSON-RPC server
# the same question from a scraper callback running on one of Kodi's own scan worker threads.
# During a first-time scan of 100+ shows, Kodi runs many of these callbacks concurrently, so
# this became self-inflicted contention on Kodi's own API from Kodi's own scan -- confirmed live
# via a real household library: shows sat with only their most-recently-processed handful of
# episodes ever committed (no exception anywhere, in either this addon's log or Kodi's own --
# a scraper callback that simply doesn't return in time is indistinguishable from one Kodi never
# scheduled, and Kodi silently moves on either way, unlike an error it would actually surface).
# Same special://temp/ cross-process approach as episode_path_cache.py/rebuild_state.py -- every
# scraper action is its own short-lived process, so there is no in-memory dict any of them could
# share. Unlike episode_path_cache.py this is NOT one-shot: the whole point is serving the same
# answer to every episode of a show without re-asking Kodi, so entries persist until they expire.
# Only a successful VideoLibrary hit (folder AND tvshowid) is cached -- the source-browsing
# fallback's folder-only result deliberately isn't, so a brand-new show still switches onto the
# fast tvshowid-bearing path the moment Kodi actually commits it, instead of being stuck serving
# a stale no-tvshowid answer for the rest of the TTL window.
_LOCATION_CACHE_DIR = 'special://temp/chronicle_scraper/tvshow_location_cache/'
_LOCATION_CACHE_TTL_SECONDS = 900
_SAFE_KEY_RE = re.compile(r'[^A-Za-z0-9._-]+')


def _location_cache_path(title, year):
    key = '{0}_{1}'.format(normalize(title), year or 0)
    return _LOCATION_CACHE_DIR + _SAFE_KEY_RE.sub('_', key) + '.json'


def _load_cached_location(title, year):
    """Returns (folder, tvshowid) from a live, unexpired cache entry, or None if there
    isn't one -- callers treat None as "go do the real lookup", same as a cache miss."""
    path = _location_cache_path(title, year)
    if not xbmcvfs.exists(path):
        return None
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = bytes(f.readBytes()).decode('utf-8')
        finally:
            f.close()
        data = json.loads(raw)
        if time.time() - data.get('cachedAt', 0) > _LOCATION_CACHE_TTL_SECONDS:
            return None
        return data.get('folder'), data.get('tvshowid')
    except Exception as exc:
        log.warning("Couldn't read cached location for {0!r} ({1}): {2}".format(title, year, exc))
        return None


def _save_cached_location(title, year, folder, tvshowid):
    """Best-effort and silent on failure -- a failure here just means every episode of this
    show goes back to resolving its own location the slow way, same as before this cache
    existed, never a correctness problem."""
    try:
        if not xbmcvfs.exists(_LOCATION_CACHE_DIR):
            xbmcvfs.mkdirs(_LOCATION_CACHE_DIR)
        data = json.dumps({'folder': folder, 'tvshowid': tvshowid, 'cachedAt': time.time()})
        f = xbmcvfs.File(_location_cache_path(title, year), 'w')
        try:
            f.write(bytearray(data.encode('utf-8')))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't cache location for {0!r} ({1}): {2}".format(title, year, exc))


def find_show_location(title, year):
    """Returns (folder, tvshowid) -- folder is the show's own root folder
    (trailing slash), tvshowid is Kodi's own internal id for it (needed for
    get_episode() below). (None, None) if the show can't be found by any
    means yet -- e.g. Kodi hasn't committed a brand-new show at the exact
    moment getdetails() runs, the same commit-timing race
    movie_art_sync.py's own docstring documents for movies.

    Checks the location cache first -- see its own doc, right above
    _location_cache_path, for why this exists: every episode of a show asks
    this exact same question."""
    cached = _load_cached_location(title, year)
    if cached is not None:
        return cached

    tvshowid, folder = _lookup_via_video_library(title, year)
    if folder:
        _save_cached_location(title, year, folder, tvshowid)
        return folder, tvshowid

    folder = _search_sources_for_show(title, year)
    if folder:
        # Freshly found via source browsing -- not yet necessarily in
        # VideoLibrary (a brand-new show), so there's no tvshowid yet.
        # Callers needing episode files/streamdetails simply get nothing
        # this pass; the very next scan (once Kodi has indexed it) picks up
        # the fast, id-based path above instead. Deliberately NOT cached --
        # see the cache's own doc for why a no-tvshowid result must never
        # be served stale once Kodi actually commits the show.
        return folder, None

    log.info('No folder found for {0!r} ({1}) via VideoLibrary or source browsing -- '
             'will not sync local art/NFO this pass'.format(title, year))
    return None, None


def _lookup_via_video_library(title, year):
    for attempt in range(1, _LOOKUP_RETRIES + 1):
        tvshowid, folder = _lookup_show(title, year)
        if folder:
            return tvshowid, folder
        if attempt < _LOOKUP_RETRIES:
            time.sleep(_LOOKUP_RETRY_DELAY_SECONDS)
    return None, None


def _lookup_show(title, year):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetTVShows',
        'params': {
            'filter': {'field': 'title', 'operator': 'is', 'value': title},
            'properties': ['file', 'year'],
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't query VideoLibrary for {0!r}: {1}".format(title, exc))
        return None, None
    if 'error' in response:
        log.warning('VideoLibrary.GetTVShows rejected title={0!r}: {1}'.format(title, response['error']))
        return None, None

    shows = response.get('result', {}).get('tvshows') or []
    if not shows:
        return None, None

    candidate = None
    if year:
        for show in shows:
            if show.get('year') == year:
                candidate = show
                break
    if candidate is None:
        candidate = shows[0]

    folder = candidate.get('file')
    if not folder:
        return None, None

    # Same folder-name verification movie_art_sync.py's own fast path added
    # after a real cross-contamination bug (v2.6.0) -- Kodi's title index can
    # point at the wrong entry if that entry's own stored title is itself
    # wrong (a stale local NFO, an earlier bad match). Verify the folder
    # actually matches before trusting it.
    folder_name = posixpath.basename(folder.rstrip('/'))
    if not year_tolerant_match(normalize(folder_name), normalize(title), year):
        log.warning('VideoLibrary lookup for {0!r} ({1}) returned {2!r} -- folder name doesn\'t '
                    'match the searched title, refusing to trust it, falling back to source '
                    'browsing instead'.format(title, year, folder))
        return None, None

    return candidate.get('tvshowid'), (folder if folder.endswith('/') else folder + '/')


def _search_sources_for_show(title, year):
    """Same three-tier exact-match strategy as
    movie_art_sync._search_sources_for_movie (title+year, then title with no
    year in the folder, then title+year within +/-1) -- see that function's
    own extensive docstring for why each tier exists and why fuzzy/
    startswith matching is deliberately never used."""
    target = normalize(title)
    if not target:
        return None
    target_with_year = target + str(year) if year else None

    listings = []
    for source in get_video_sources():
        dirs, _files = listdir_with_timeout(source)
        if dirs is not None:
            listings.append((source, dirs))

    if target_with_year:
        for source, dirs in listings:
            for name in dirs:
                if normalize(name) == target_with_year:
                    return source.rstrip('/') + '/' + name + '/'

    for source, dirs in listings:
        for name in dirs:
            if normalize(name) == target:
                return source.rstrip('/') + '/' + name + '/'

    if year is not None:
        for source, dirs in listings:
            for name in dirs:
                if year_tolerant_match(normalize(name), target, year):
                    return source.rstrip('/') + '/' + name + '/'

    return None


def get_episode(tvshowid, season, episode):
    """Returns (file_path, streamdetails) for this exact (season, episode)
    under tvshowid, or (None, None) if tvshowid is unknown (see
    find_show_location), the VideoLibrary.GetEpisodes call itself fails, or
    this (season, episode) isn't in Kodi's list yet -- e.g. a brand-new file
    not yet committed (getepisodedetails() for a just-added episode runs
    DURING the very scan that's adding it, so this is common, not rare; see
    module docstring). One VideoLibrary.GetEpisodes call gives both the file
    path and Kodi's own streamdetails together -- unlike movies, there's no
    separate lookup needed for streamdetails, since an episode is identified
    precisely by season+episode rather than a fuzzy title/year guess.
    Matched in Python rather than via a JSON-RPC filter on season+episode --
    simpler to get right than trusting a two-field filter combination, and a
    single show's episode list is never large enough for that to matter.

    A SECOND, distinct way this comes back empty, confirmed live via
    kodi.log (2026-08-28): during an nfo_rebuild.py rebuild pass, this
    episode's OWN VideoLibrary.RefreshEpisode() call is what's currently
    running the very getepisodedetails() callback that calls this function
    -- Kodi tears the episode's library row down for the duration of that
    refresh and only recommits it once the callback returns (setResolvedUrl
    + endOfDirectory), so asking VideoLibrary about this exact episode from
    inside its own in-flight refresh legitimately finds nothing, no matter
    how "already committed" the item was a moment before the refresh was
    issued. This is NOT the brand-new-file race above (that's about an item
    Kodi hasn't indexed yet at all; this is about an item Kodi is
    momentarily un-indexing on purpose) and no retry fixes it -- the row
    will not exist until this same call chain finishes. See
    lib/episode_path_cache.py, which nfo_rebuild.py populates with each
    episode's already-known file path before issuing the refresh, and which
    tvshow_scraper.py's get_episode_details() falls back to whenever this
    function comes back empty during a rebuild.

    Deliberately single-attempt, no retry: this is called synchronously from
    Kodi's own live library scan (getepisodedetails()), and its result only
    feeds the local NFO write / legacy-NFO harvest, not the episode's actual
    resolution into Kodi's library (that already happened via
    setResolvedUrl() before this runs) -- so blocking the scan with a sleep
    here to chase the brand-new-file race buys nothing for how fast files
    actually show up in Kodi, only for how fast the local NFO catches up
    (and does nothing at all for the in-flight-refresh race above, which no
    amount of waiting resolves). A missed NFO outside a rebuild pass is
    caught on the episode's next ordinary scan, or on the next explicit
    rebuild. Also returns Kodi's own internal episodeid (None if not found) -- see
    lib/chronicle_client.py's report_kodi_id(), which callers use to let Chronicle push a
    future NFO update straight to this device via VideoLibrary.RefreshEpisode."""
    if tvshowid is None:
        return None, None, None

    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetEpisodes',
        'params': {
            'tvshowid': tvshowid,
            'properties': ['file', 'streamdetails', 'season', 'episode'],
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't query VideoLibrary.GetEpisodes for tvshowid={0}: {1}".format(tvshowid, exc))
        return None, None, None
    if 'error' in response:
        log.warning('VideoLibrary.GetEpisodes rejected tvshowid={0}: {1}'.format(tvshowid, response['error']))
        return None, None, None

    for ep in response.get('result', {}).get('episodes') or []:
        if ep.get('season') != season or ep.get('episode') != episode:
            continue
        file_path = ep.get('file')
        raw = ep.get('streamdetails') or {}
        streamdetails = raw if (raw.get('video') or raw.get('audio') or raw.get('subtitle')) else None
        return file_path, streamdetails, ep.get('episodeid')

    return None, None, None
