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
"""

import json
import posixpath
import time

import xbmc

from lib.logger import Logger
from lib.movie_art_sync import get_video_sources, listdir_with_timeout, normalize, year_tolerant_match

log = Logger('tvshow_location')

_LOOKUP_RETRIES = 2
_LOOKUP_RETRY_DELAY_SECONDS = 1.0


def find_show_location(title, year):
    """Returns (folder, tvshowid) -- folder is the show's own root folder
    (trailing slash), tvshowid is Kodi's own internal id for it (needed for
    get_episode() below). (None, None) if the show can't be found by any
    means yet -- e.g. Kodi hasn't committed a brand-new show at the exact
    moment getdetails() runs, the same commit-timing race
    movie_art_sync.py's own docstring documents for movies."""
    tvshowid, folder = _lookup_via_video_library(title, year)
    if folder:
        return folder, tvshowid

    folder = _search_sources_for_show(title, year)
    if folder:
        # Freshly found via source browsing -- not yet necessarily in
        # VideoLibrary (a brand-new show), so there's no tvshowid yet.
        # Callers needing episode files/streamdetails simply get nothing
        # this pass; the very next scan (once Kodi has indexed it) picks up
        # the fast, id-based path above instead.
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
