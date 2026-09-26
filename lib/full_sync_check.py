# -*- coding: utf-8 -*-
"""Full-library sync-check: after every Kodi video library scan finishes -- whatever triggered
it (native startup auto-scan, the user's own manual "Update Library", or this addon's own
new-content-detection feature) -- walks every movie Kodi's own VideoLibrary already has and
verifies it still matches Chronicle's current data, pushing a correction only for whatever
field(s) actually disagree.

Per-user direction (2026-09-22): "Kodi has all of the file paths. You should be able to use
that to definitively figure out what item is what in Chronicle." Resolves each Kodi movie to
its Chronicle item purely by the exact video file's own basename
(ChronicleClient.get_movie_details_by_file), never by title/year matching and never by
Chronicle's own kodi_library_ids bookkeeping table -- that table only covers items a NORMAL
scrape has already successfully reported, and can't be trusted as a complete inventory the way
Kodi's own file list is.

Root-caused live (2026-09-22): a "Ghostbusters (2016)" file was scraped with the WRONG (1984)
movie's data at some point in the past and never corrected since -- Chronicle's own data was
already right, but nothing had ever pushed the fix into Kodi's own local VideoLibrary (the
existing kodi-refresh-signal mechanism only fires for items already known to have changed
SINCE their last successful sync; it can't catch data that was simply wrong from the start).
This is the durable, general fix for that whole class of problem, not a one-off.

Per-user direction: only ever updates a field that actually differs from what Chronicle has --
"you're only doing this for items that are not the same as what Chronicle has" -- never a blind
overwrite of everything on every pass. Only ever calls VideoLibrary.SetMovieDetails/writes a
movie's own local art files, never writes an NFO file and never involves Chronicle's own server
calling this device's JSON-RPC -- same pull-only architecture as every other feature in this addon.

Poster/fanart/thumb go through movie_art_sync.sync_movie_art(), not VideoLibrary.SetMovieDetails'
own `art` parameter. Caught live (2026-09-22), right after this feature's first release: Kodi
uses a movie's own local "-poster.jpg" file unconditionally, before ever looking at anything a
scraper (or SetMovieDetails' art param) offers -- see movie_art_sync.py's own module doc, the
same reason the normal scrape path never trusted SetMovieDetails for art either. The Ghostbusters
(2016) fix landed correctly for every OTHER field but the poster kept showing the wrong (1984)
image regardless, because nothing had actually rewritten the local file. sync_movie_art() already
solves this properly (writes the local file, skip-caches on unchanged URL+size, invalidates
Kodi's texture cache on overwrite) and is called unconditionally per item, same as the normal
scrape flow -- its own skip-cache is what keeps an unchanged poster cheap, not a diff check here.
"""

import json
import posixpath
import re

import xbmc

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger
from lib import movie_art_sync

log = Logger('full_sync_check')

_MOVIE_PROPERTIES = [
    'file', 'title', 'year', 'plot', 'tagline', 'mpaa', 'genre', 'director',
    'imdbnumber', 'uniqueid', 'premiered', 'art', 'cast', 'studio', 'country', 'set',
]

_YEAR_IN_NAME = re.compile(r'[\(\[]((?:19|20)\d{2})[\)\]]')


def year_from_path(path):
    """The (YYYY)/[YYYY] year in a movie file's own name, else in its folder's name, else None.

    Kodi's own year for a movie is only as good as the scrape that set it -- confirmed live
    (2026-09-26): "Total Recall (2012).mkv" was scraped as the 1990 film, so Kodi's year (1990)
    matched Chronicle's wrong item and even a corrected Chronicle (which now knows the file as the
    2012 film) would have been rejected by the year guard as a contradiction and never applied. The
    file name is the independent witness to which film a file is."""
    parts = (path or '').replace('\\', '/').split('/')
    for candidate in (parts[-1], parts[-2] if len(parts) > 1 else ''):
        m = _YEAR_IN_NAME.search(candidate)
        if m:
            return int(m.group(1))
    return None


def years_in_path(path):
    """Every (YYYY)/[YYYY] year tag in a movie's file name and its folder name. A fan edit can carry two
    (live 2026-09-26: folder "... Defrosted Edition (2017)" holding "... (2014).mkv"), and a match for
    either is not a contradiction."""
    parts = (path or '').replace('\\', '/').split('/')
    found = []
    for candidate in (parts[-1], parts[-2] if len(parts) > 1 else ''):
        found.extend(int(y) for y in _YEAR_IN_NAME.findall(candidate))
    return found


def _get_all_movies():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovies',
        'params': {'properties': _MOVIE_PROPERTIES},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("full_sync_check: couldn't get movie list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('full_sync_check: VideoLibrary.GetMovies rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('movies') or []


def cast_differs(kodi_item, details):
    """True when Chronicle has a cast for this movie and its set of names differs from Kodi's.

    Deliberately NOT part of diff_movie's SetMovieDetails params: Kodi's JSON-RPC
    VideoLibrary.SetMovieDetails has no cast parameter at all ("Too many parameters"), so putting
    one in the update made Kodi reject the WHOLE call -- confirmed live (2026-09-26): every field of
    Total Recall (2012)'s correction was discarded because its cast differed, and since v3.17.11 the
    same happened to any movie with a differing cast. Cast is only settable by a re-scrape (see
    refresh_movie)."""
    cast = [c for c in (details.get('cast') or []) if c.get('name')]
    kodi_names = {(c.get('name') or '').lower() for c in (kodi_item.get('cast') or [])}
    return bool(cast) and kodi_names != {c['name'].lower() for c in cast}


def refresh_movie(movieid):
    """Asks Kodi to re-scrape one movie (VideoLibrary.RefreshMovie) -- the only way to change its
    cast. The scrape goes through this addon, which answers with Chronicle's current data. Only ever
    called for a movie whose identity-level text was just corrected AND whose cast differs, never for
    a cast-only difference (names/order differ harmlessly all the time)."""
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.RefreshMovie',
        'params': {'movieid': movieid, 'ignorenfo': True},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning('full_sync_check: RefreshMovie({0}) failed: {1}'.format(movieid, exc))
        return False
    if 'error' in response:
        log.warning('full_sync_check: RefreshMovie({0}) rejected: {1}'.format(movieid, response['error']))
        return False
    return True


def set_differs(kodi_item, details):
    """True when Chronicle puts this movie in a collection and Kodi's set for it is different (or
    none). Only that direction: a movie Chronicle has no collection for is never pulled out of a set
    Kodi has, since the set may have come from elsewhere.

    Kodi has no way to assign a set over JSON-RPC's SetMovieDetails without also owning the set row,
    so -- like cast -- membership is restored by a re-scrape, whose answer (scraper.py's setSet) puts
    the movie back in Chronicle's collection. Live (2026-09-26): The Matrix and Captain America: The
    Winter Soldier were standalone in Kodi because Kodi was bound to a duplicate Chronicle item with no
    collection; once bound to the real one their set had to be restored."""
    chronicle_set = ((details.get('collection') or {}).get('name') or '').strip()
    return bool(chronicle_set) and (kodi_item.get('set') or '').strip() != chronicle_set


IDENTITY_KEYS = ('title', 'year', 'plot')


def needs_cast_refresh(kodi_item, details, updates):
    """A re-scrape is warranted only when this movie was just found to be a different work (its
    title, year or plot had to be corrected) AND its cast differs."""
    return any(k in updates for k in IDENTITY_KEYS) and cast_differs(kodi_item, details)


def plausible_year(year):
    """A real release year -- Kodi reports an unknown one as 65535 (-1 stored unsigned)."""
    import time
    return isinstance(year, int) and 1878 <= year <= time.gmtime().tm_year + 10


def diff_movie(kodi_item, details):
    """Returns a dict of VideoLibrary.SetMovieDetails params for whatever fields actually
    differ between what Kodi currently has (kodi_item, a VideoLibrary.GetMovies entry using
    this module's own _MOVIE_PROPERTIES) and what Chronicle currently has (details, a
    get_movie_details()-shaped dict) -- or {} if everything already matches. Only ever adds a
    key when Chronicle actually has a value for it AND that value disagrees with Kodi's own --
    a field Chronicle has nothing for is left alone rather than blanked out."""
    updates = {}

    if details.get('title') and kodi_item.get('title') != details['title']:
        updates['title'] = details['title']
    if details.get('year') and kodi_item.get('year') != details['year']:
        updates['year'] = details['year']
    elif not plausible_year(kodi_item.get('year')) and not details.get('year'):
        # Kodi's own year is impossible (65535) and Chronicle has none to replace it with: the year in
        # the file's own name is the next best witness, better than showing 65535.
        file_year = year_from_path(kodi_item.get('file'))
        if file_year:
            updates['year'] = file_year
    if details.get('overview') and kodi_item.get('plot') != details['overview']:
        updates['plot'] = details['overview']
    if details.get('tagline') and kodi_item.get('tagline') != details['tagline']:
        updates['tagline'] = details['tagline']
    if details.get('mpaa') and kodi_item.get('mpaa') != details['mpaa']:
        updates['mpaa'] = details['mpaa']

    premiered = details.get('premiered')
    if premiered and kodi_item.get('premiered') != premiered[:10]:
        updates['premiered'] = premiered[:10]

    # List fields compared as sets, not ordered lists -- Kodi's own VideoLibrary.GetMovies
    # array-typed properties are not guaranteed to read back in the same order they were
    # written in, and an order-only "difference" isn't a real one; treating it as one would
    # push a same-content "correction" on every single pass forever, violating this function's
    # whole contract of only touching fields that actually differ.
    genres = details.get('genres') or []
    if genres and set(kodi_item.get('genre') or []) != set(genres):
        updates['genre'] = genres

    directors = [c['name'] for c in (details.get('crew') or [])
                 if (c.get('job') or '').lower() == 'director']
    if directors and set(kodi_item.get('director') or []) != set(directors):
        updates['director'] = directors

    studio = details.get('studio')
    if studio and set(kodi_item.get('studio') or []) != {studio}:
        updates['studio'] = [studio]
    country = details.get('country')
    if country and set(kodi_item.get('country') or []) != {country}:
        updates['country'] = [country]

    imdb = (details.get('externalIds') or {}).get('imdb')
    if imdb and kodi_item.get('imdbnumber') != imdb:
        updates['imdbnumber'] = imdb
        uniqueid = dict(kodi_item.get('uniqueid') or {})
        uniqueid['imdb'] = imdb
        tmdb = (details.get('externalIds') or {}).get('tmdb')
        if tmdb:
            uniqueid['tmdb'] = tmdb
        updates['uniqueid'] = uniqueid

    # Deliberately NOT handling 'art' here -- see this module's own top-of-file doc. Kodi
    # ignores VideoLibrary.SetMovieDetails' own art parameter in favor of a movie's local
    # poster/fanart file whenever one exists, so a diff-and-set approach here would silently
    # never actually take effect. sync_movie_art() (called unconditionally in run(), with its
    # own skip-cache) is what actually reaches the screen.

    return updates


def _set_movie_details(movieid, updates):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetMovieDetails',
        'params': dict(updates, movieid=movieid),
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning('full_sync_check: SetMovieDetails({0}) failed: {1}'.format(movieid, exc))
        return False
    if 'error' in response:
        log.warning('full_sync_check: SetMovieDetails({0}) rejected: {1}'.format(movieid, response['error']))
        return False
    return True


def run(is_cancelled=None, progress_callback=None):
    """Best-effort throughout: one movie failing to resolve or update never stops the rest.
    Returns a summary dict; never raises.

    Checks connectivity up front and aborts the whole pass if Chronicle isn't reachable --
    ChronicleClient._get() returns None identically for "no match for this file" (expected,
    most items) and "Chronicle is down/unreachable/API key rejected" (a real failure), so
    without this check an outage partway through would silently look exactly like a completely
    clean library (checked=0 either way) instead of the error it actually is."""
    is_cancelled = is_cancelled or (lambda: False)
    client = ChronicleClient()

    reachable, message = client.test_connection()
    if not reachable:
        log.error('full_sync_check: aborting -- Chronicle not reachable: {0}'.format(message))
        return {'checked': 0, 'updated': 0, 'errors': 0, 'cancelled': False,
                'aborted': 'Chronicle not reachable: {0}'.format(message)}

    movies = _get_all_movies()
    log.info('full_sync_check: checking {0} movie(s) against Chronicle'.format(len(movies)))

    checked = 0
    updated = 0
    errors = 0
    for index, movie in enumerate(movies):
        if is_cancelled():
            break
        if progress_callback:
            progress_callback(index, len(movies), movie.get('title') or '')

        file_path = movie.get('file')
        if not file_path:
            continue
        file_name = posixpath.basename(file_path)

        details = client.get_movie_details_by_file(
            file_name, year=year_from_path(file_path) or movie.get('year'))
        if not details:
            continue  # nothing unambiguous in Chronicle for this exact file -- nothing to sync
        checked += 1

        # Unconditional, same as the normal scrape flow (python/scraper.py) -- sync_movie_art's
        # own skip-cache (unchanged URL + local file size) is what keeps an already-correct
        # poster cheap here, not a diff check. location is derived straight from the file Kodi
        # itself just told us about -- more direct and reliable than sync_movie_art's own
        # fallback lookup (a VideoLibrary re-query, then source-folder browsing), which exists
        # for when the caller doesn't already know the file's real current location.
        folder = posixpath.dirname(file_path) + '/'
        video_basename = movie_art_sync.strip_video_ext(posixpath.basename(file_path))
        try:
            movie_art_sync.sync_movie_art(
                details.get('title'), details.get('year'), details.get('artwork'),
                location=(folder, video_basename))
        except Exception as exc:
            log.warning('full_sync_check: art sync failed for "{0}": {1}'.format(
                        movie.get('title'), exc))

        updates = diff_movie(movie, details)
        if not updates:
            continue

        refresh = needs_cast_refresh(movie, details, updates) or set_differs(movie, details)
        if _set_movie_details(movie['movieid'], updates):
            updated += 1
            log.info('full_sync_check: "{0}" ({1}) -- updated {2}'.format(
                movie.get('title'), movie.get('year'), ', '.join(sorted(updates.keys()))))
            if refresh:
                refresh_movie(movie['movieid'])
        else:
            errors += 1

    result = {'checked': checked, 'updated': updated, 'errors': errors, 'cancelled': is_cancelled(),
              'aborted': None}
    log.info('full_sync_check: complete -- {0}'.format(result))
    return result
