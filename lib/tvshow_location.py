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
    find_show_location) or Kodi doesn't have this episode yet (e.g. a
    brand-new file not yet scanned). One VideoLibrary.GetEpisodes call gives
    both the file path and Kodi's own streamdetails together -- unlike
    movies, there's no separate lookup needed for streamdetails, since an
    episode is identified precisely by season+episode rather than a fuzzy
    title/year guess. Matched in Python rather than via a JSON-RPC filter on
    season+episode -- simpler to get right than trusting a two-field filter
    combination, and a single show's episode list is never large enough for
    that to matter."""
    if tvshowid is None:
        return None, None
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
        return None, None
    if 'error' in response:
        log.warning('VideoLibrary.GetEpisodes rejected tvshowid={0}: {1}'.format(tvshowid, response['error']))
        return None, None

    for ep in response.get('result', {}).get('episodes') or []:
        if ep.get('season') != season or ep.get('episode') != episode:
            continue
        file_path = ep.get('file')
        raw = ep.get('streamdetails') or {}
        streamdetails = raw if (raw.get('video') or raw.get('audio') or raw.get('subtitle')) else None
        return file_path, streamdetails

    return None, None
