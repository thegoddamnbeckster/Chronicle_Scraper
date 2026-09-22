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
overwrite of everything on every pass. Only ever calls VideoLibrary.SetMovieDetails, never
writes an NFO file and never involves Chronicle's own server calling this device's JSON-RPC --
same pull-only architecture as every other feature in this addon.
"""

import json
import posixpath

import xbmc

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('full_sync_check')

_MOVIE_PROPERTIES = [
    'file', 'title', 'year', 'plot', 'tagline', 'mpaa', 'genre', 'director',
    'imdbnumber', 'uniqueid', 'premiered', 'art',
]


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

    imdb = (details.get('externalIds') or {}).get('imdb')
    if imdb and kodi_item.get('imdbnumber') != imdb:
        updates['imdbnumber'] = imdb
        uniqueid = dict(kodi_item.get('uniqueid') or {})
        uniqueid['imdb'] = imdb
        tmdb = (details.get('externalIds') or {}).get('tmdb')
        if tmdb:
            uniqueid['tmdb'] = tmdb
        updates['uniqueid'] = uniqueid

    posters = (details.get('artwork') or {}).get('poster') or []
    chronicle_poster = posters[0]['url'] if posters else None
    kodi_poster = (kodi_item.get('art') or {}).get('poster')
    if chronicle_poster and kodi_poster != chronicle_poster:
        art = dict(kodi_item.get('art') or {})
        art['poster'] = chronicle_poster
        updates['art'] = art

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

        details = client.get_movie_details_by_file(file_name, year=movie.get('year'))
        if not details:
            continue  # nothing unambiguous in Chronicle for this exact file -- nothing to sync
        checked += 1

        updates = diff_movie(movie, details)
        if not updates:
            continue

        if _set_movie_details(movie['movieid'], updates):
            updated += 1
            log.info('full_sync_check: "{0}" ({1}) -- updated {2}'.format(
                movie.get('title'), movie.get('year'), ', '.join(sorted(updates.keys()))))
        else:
            errors += 1

    result = {'checked': checked, 'updated': updated, 'errors': errors, 'cancelled': is_cancelled(),
              'aborted': None}
    log.info('full_sync_check: complete -- {0}'.format(result))
    return result
