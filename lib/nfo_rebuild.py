# -*- coding: utf-8 -*-
"""Bulk "nuke and rebuild" for local movie NFO/set-art files -- the actual
mechanism the "Recreate local NFO file from Chronicle's data" setting needs
to be useful for a library that already has local NFOs, not just new
additions.

Why this is a separate, explicit, user-triggered action rather than fully
automatic: nfo_writer.py's write only ever runs when Kodi actually invokes
this scraper -- and Kodi never does that for a movie that already has ANY
local NFO on disk, from this addon or anywhere else (tinyMediaManager,
another scraper, a hand-written one). Confirmed directly via kodi.log and a
live VideoLibrary.RefreshMovie test (2026-07-30): a movie with an existing
NFO never reaches find/getdetails at all, no matter how many times the
library is rescanned. The passive per-scrape NFO write can therefore only
ever help movies added AFTER it's enabled -- it cannot retroactively fix a
library where local NFOs already exist, since Chronicle_Scraper simply never
gets called for those. This action breaks that deadlock on demand: delete
the local NFO (and movieset-* art, a separate but equally local-file-wins
convention Kodi supports) Kodi is currently preferring, then force a refresh
so Kodi has no choice but to ask Chronicle for a real answer -- the
passive setting then keeps things current on every subsequent scrape.

Deliberately destructive and irreversible -- default.py must show a clear
warning and get explicit confirmation before calling run(). Every deleted
file is Kodi's own local-metadata copy, not the video file itself; Chronicle
is always the one true source of the data being deleted, so nothing gets
lost that Chronicle can't reproduce -- but a file only some OTHER tool
(tinyMediaManager, hand edits) knows about would be gone with no way back.
"""

import json

import xbmc
import xbmcvfs

from lib.logger import Logger
from lib.movie_art_sync import listdir_with_timeout

log = Logger('nfo_rebuild')

# Paced deliberately -- this fires one VideoLibrary.RefreshMovie per movie,
# each of which spawns this addon's own scraper process again (HTTP call to
# Chronicle, image downloads, an NFO write). Back-to-back with no pacing at
# all risks doing to Chronicle/the SMB shares what an unthrottled loop would;
# matches the ~1s/movie pace already proven fine for ~1200 movies elsewhere
# this session.
_SETTLE_DELAY_SECONDS = 1.0


def run(progress_callback=None, is_cancelled=None):
    """Deletes every local .nfo and movieset-* file for every movie already
    in Kodi's library, then refreshes each one so Chronicle_Scraper
    repopulates them fresh.

    progress_callback(index, total, label), if given, is called before each
    movie is processed. is_cancelled(), if given, is checked between movies
    and stops the run early (already-processed movies stay fixed either way).

    Returns (movies_processed, nfo_deleted, movieset_deleted, refresh_errors).
    """
    movies = _get_all_movies()
    total = len(movies)
    nfo_deleted = 0
    movieset_deleted = 0
    refresh_errors = 0
    processed = 0

    log.info('nfo_rebuild: starting -- {0} movies in library'.format(total))

    for movie in movies:
        if is_cancelled is not None and is_cancelled():
            log.warning('nfo_rebuild: cancelled by user at {0}/{1}'.format(processed, total))
            break

        if progress_callback is not None:
            progress_callback(processed, total, movie.get('label') or '')

        file_path = movie.get('file') or ''
        folder = file_path.rsplit('/', 1)[0] + '/' if '/' in file_path else None
        if folder:
            deleted_nfo, deleted_movieset = _delete_local_metadata(folder)
            nfo_deleted += deleted_nfo
            movieset_deleted += deleted_movieset

        if not _refresh_movie(movie['movieid']):
            refresh_errors += 1

        processed += 1
        xbmc.sleep(int(_SETTLE_DELAY_SECONDS * 1000))

    log.info('nfo_rebuild: done -- {0}/{1} movies processed, {2} nfo deleted, '
             '{3} movieset files deleted, {4} refresh errors'.format(
             processed, total, nfo_deleted, movieset_deleted, refresh_errors))
    return processed, nfo_deleted, movieset_deleted, refresh_errors


def _get_all_movies():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovies',
        'params': {'properties': ['file', 'title']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get movie list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetMovies rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('movies') or []


def _delete_local_metadata(folder):
    """Deletes any .nfo and movieset-* file directly in folder (timeout-
    guarded the same way movie_art_sync.py's own source browsing is, since
    this walks every movie's real folder and an unresponsive share here would
    otherwise hang the whole rebuild the same way it once hung art syncing).
    Returns (nfo_deleted, movieset_deleted)."""
    _dirs, files = listdir_with_timeout(folder)
    if files is None:
        return 0, 0

    nfo_count = 0
    movieset_count = 0
    for name in files:
        is_nfo = name.lower().endswith('.nfo')
        is_movieset = name.lower().startswith('movieset-')
        if not (is_nfo or is_movieset):
            continue
        path = folder + name
        try:
            if xbmcvfs.delete(path):
                if is_nfo:
                    nfo_count += 1
                else:
                    movieset_count += 1
            else:
                log.warning("xbmcvfs.delete() returned falsy for {0}".format(path))
        except Exception as exc:
            log.warning("Couldn't delete {0}: {1}".format(path, exc))

    return nfo_count, movieset_count


def _refresh_movie(movieid):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.RefreshMovie',
        'params': {'movieid': movieid},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't refresh movieid {0}: {1}".format(movieid, exc))
        return False
    if 'error' in response:
        log.warning('RefreshMovie rejected movieid {0}: {1}'.format(movieid, response['error']))
        return False
    return True
