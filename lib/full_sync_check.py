# -*- coding: utf-8 -*-
"""What a Kodi movie should look like according to Chronicle, and the difference between the two.

This used to be its own pass after every library scan; that pass is now the watch/rating sync
(lib/watch_rating_sync.py), which uses everything here: diff_movie() names the fields whose value in
Kodi is not Chronicle's, so only those are written, never a blind overwrite; refresh_movie() and
set_differs()/needs_cast_refresh() decide when a re-scrape is the only way to fix cast or set
membership (VideoLibrary.SetMovieDetails has no parameter for either).

Per-user direction (2026-09-22): "Kodi has all of the file paths. You should be able to use that to
definitively figure out what item is what in Chronicle." year_from_path()/years_in_path() read the
(YYYY) in a file's own name, the independent witness to which film a file is when Kodi's own year came
from a wrong scrape (live 2026-09-26: "Total Recall (2012).mkv" scraped as the 1990 film).

Poster/fanart/thumb are not handled here: Kodi uses a movie's own local "-poster.jpg" unconditionally,
before anything a scraper or SetMovieDetails' art parameter offers, so art goes through
movie_art_sync.sync_movie_art() (which writes the local file and skip-caches an unchanged one).
"""

import json
import re

import xbmc

from lib.logger import Logger

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
    # Kodi derives a movie's year from its premiered date and ignores a year sent alone: a film Kodi read as
    # "65535-11-30" kept year 65535 however often the year was "applied" (every pass, reported 2026-10-31). When the year is
    # being corrected, the date has to agree with it, and a Jan 1 stands in when Chronicle has no full date.
    if 'year' in updates and 'premiered' not in updates             and (kodi_item.get('premiered') or '')[:4] != str(updates['year']):
        updates['premiered'] = '{0}-01-01'.format(updates['year'])

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
