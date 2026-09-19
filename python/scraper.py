# -*- coding: utf-8 -*-
"""Kodi metadata-scraper entry point (xbmc.metadata.scraper.movies).

Real Kodi Python-scraper contract, confirmed against Team Kodi's own
metadata.themoviedb.org.python addon (python/scraper.py) as ground truth
rather than guessed:

  Kodi invokes this script with sys.argv = [script_path, handle, querystring],
  where querystring looks like "?action=find&title=X&year=Y".

  action=find        -> search_for_movie(): xbmcplugin.addDirectoryItem() per
                         candidate, url= an opaque lookup string Kodi echoes
                         back verbatim on the subsequent getdetails call.
  action=getdetails  -> get_details(): xbmcplugin.setResolvedUrl() with a
                         fully populated ListItem.

Uses the modern InfoTagVideo object API (ListItem.getVideoInfoTag(), Kodi 20+)
rather than the older setInfo('video', {...}) dict style -- ground-truthed
against Team Kodi's own metadata.tvshows.themoviedb.org.python addon, which
already uses this API and exposes strictly more of what Kodi can receive
(addSeason, setSet/setSetOverview, per-type addAvailableArtwork with multiple
candidates, setRatings with multiple sources, setTrailer, setUniqueIDs).

Deliberately thin: all the actual "which source, which title, resolve or
create" logic lives in Chronicle itself (ScraperController). This file only
translates between Kodi's plugin-handle protocol and Chronicle's HTTP API --
it never talks to TMDB or anything else directly.

Fields Kodi supports but no field Chronicle currently populates for movies
(writers, sort title, top 250) are simply left unset rather than faked --
see the addon README for the full list of known gaps.
"""

import json
import os
import sys
import time
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

# For the xbmc.metadata.scraper.* extension point, Kodi puts only this
# script's own directory (python/) on sys.path -- not the addon root the
# way it does for the xbmc.python.script entry (default.py). lib/ lives at
# the addon root, so it has to be added explicitly or "from lib.x import y"
# fails with ModuleNotFoundError.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.logger import Logger
from lib import activity_tracker
from lib.chronicle_client import ChronicleClient
from lib.kodi_video_info import (
    apply_common_video_info, apply_ratings, apply_artwork, youtube_trailer_uri,
)
from lib.collection_sync import sync_collection_art
from lib.movie_art_sync import sync_movie_art, find_movie_location
from lib import progress_sync

log = Logger('scraper')
ADDON = xbmcaddon.Addon()


def get_params(argv):
    """argv[0] is the plugin handle (int); argv[1] is a "?key=value&..." querystring.

    Matches the real TMDB addon's own get_params() exactly -- this is Kodi's
    standard plugin:// handle+querystring convention, not scraper-specific.
    """
    result = {'handle': int(argv[0])}
    if len(argv) < 2 or not argv[1]:
        return result
    result.update(parse_qsl(argv[1].lstrip('?')))
    return result


def build_lookup_string(media_item_id) -> str:
    """Opaque string Kodi stores after `find` and hands back verbatim to `getdetails`."""
    return json.dumps({'chronicleId': media_item_id})


def parse_lookup_string(value):
    try:
        return json.loads(value).get('chronicleId')
    except (ValueError, AttributeError, TypeError):
        log.warning("Can't parse lookup string, is it from another addon? {0!r}".format(value))
        return None


def search_for_movie(title, year, handle):
    log.info('find: title={0!r} year={1!r}'.format(title, year))
    activity_tracker.mark_active(title)

    # Confirmation by filename: title-only matching misses whenever the folder
    # name Kodi derives its title from doesn't match Chronicle's own stored
    # title for an item it already has (e.g. a fan edit filed under a
    # franchise-prefixed folder, "Alien - Derelict" vs the real item's own
    # title "Derelict") -- Chronicle used to just create a second, wrongly-
    # typed, posterless duplicate every time that happened. find_movie_location
    # is the same VideoLibrary/source-browsing lookup get_details() already
    # uses to place local art -- reused here, before the title search, purely
    # to discover the real physical file's basename if it's findable this
    # early, so Chronicle can check "does this exact file already belong to
    # some other item" before ever considering creating a new one.
    try:
        _, _, full_filename, _, _ = find_movie_location(title, year)
    except Exception as exc:
        log.warning('find: filename pre-check failed for {0!r} ({1}) -- continuing by title only'.format(
                    title, exc))
        full_filename = None

    result = ChronicleClient().search_movie(title, year, filename=full_filename)
    if not result:
        log.warning('find: Chronicle returned no candidate for title={0!r} year={1!r} -- '
                    'this title will not appear as a match in Kodi at all'.format(title, year))
        return

    log.info('find: title={0!r} year={1!r} -> item {2} posterUrl={3}'.format(
        title, year, result.get('id'), result.get('posterUrl') or '(none)'))

    label = result.get('title') or title
    if result.get('year'):
        label = '{0} ({1})'.format(label, result['year'])

    listitem = xbmcgui.ListItem(label, offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setTitle(result.get('title') or title)
    if result.get('year'):
        vtag.setYear(result['year'])
    if result.get('posterUrl'):
        vtag.addAvailableArtwork(result['posterUrl'], 'poster')

    xbmcplugin.addDirectoryItem(
        handle=handle,
        url=build_lookup_string(result['id']),
        listitem=listitem,
        isFolder=True,
    )


def _log_details_summary(action, media_item_id, details):
    """Logs exactly what Chronicle handed back for this item -- title, whether
    artwork/collection are present at all, and per-arttype candidate counts.
    Added because this whole round-trip (Chronicle API -> scraper -> local file
    write) previously had zero visibility: a movie showing a blank poster in
    Kodi could mean Chronicle returned no poster candidates, or it could mean
    the scraper/sync side dropped a perfectly good URL -- with no logging,
    those two cases were indistinguishable from the log alone."""
    if not details:
        log.warning('{0}: Chronicle returned no details at all for item {1} (unreachable, 401, '
                    '404, or malformed response -- see client log lines above/below)'.format(
                    action, media_item_id))
        return

    artwork = details.get('artwork') or {}
    art_summary = ', '.join('{0}={1}'.format(k, len(v)) for k, v in artwork.items()) or '(none)'
    collection = details.get('collection')
    if collection:
        coll_summary = '"{0}" poster={1} backdrop={2}'.format(
            collection.get('name'),
            'yes' if collection.get('posterUrl') else 'NONE',
            'yes' if collection.get('backdropUrl') else 'NONE')
    else:
        coll_summary = '(none)'

    log.info('{0}: item {1} "{2}" -- artwork[{3}] collection={4}'.format(
        action, media_item_id, details.get('title'), art_summary, coll_summary))

    if not artwork.get('poster'):
        log.warning('{0}: item {1} "{2}" has NO poster candidates from Chronicle -- '
                    'Kodi will show a blank/title-only thumbnail'.format(
                    action, media_item_id, details.get('title')))


def get_details(media_item_id, handle):
    if media_item_id is None:
        log.warning('getdetails: called with no resolvable media_item_id -- lookup string could not be parsed')
        return False

    # Per-step timing -- added (2026-09-18) after a real full-library scan turned out to be
    # dominated almost entirely by client-side work (image downloads, source-folder lookups)
    # that never shows up in Chronicle's own server-side request log at all. Kept permanently
    # (not a temporary debug aid), so any future slowdown is directly diagnosable from kodi.log
    # instead of requiring another round of log archaeology.
    t_start = time.time()

    details = ChronicleClient().get_movie_details(media_item_id)
    t_chronicle = time.time()
    _log_details_summary('getdetails', media_item_id, details)
    if not details:
        return False
    activity_tracker.mark_active(details.get('title') or str(media_item_id))

    # Resolved once, here, and reused by everything below, so sync_movie_art
    # doesn't have to independently browse Kodi's video sources for the same
    # folder. knownFileName (when Chronicle has it) short-circuits straight to the
    # real file instead of re-deriving the folder from title/year -- see
    # find_movie_location()'s own docstring. When it had to fall back to
    # title/year matching anyway, report the discovered filename back so the
    # NEXT scrape gets to use the fast path too.
    folder, video_basename, full_filename, discovered_via_fallback, kodi_movie_id = find_movie_location(
        details.get('title'), details.get('year'), known_filename=details.get('knownFileName'))
    t_location = time.time()
    location = (folder, video_basename)
    if discovered_via_fallback and full_filename:
        ChronicleClient().report_resolved_file(media_item_id, full_filename)
    if kodi_movie_id is not None:
        # Lets Chronicle push a future NFO update straight to this device (see
        # lib/chronicle_client.py's report_kodi_id() and Chronicle's own NfoPushService).
        # Fired on every ordinary scan, so the mapping stays fresh.
        ChronicleClient().report_kodi_id(media_item_id, 'movie', kodi_movie_id)

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setMediaType('movie')

    apply_common_video_info(vtag, details)

    if details.get('tagline'):
        vtag.setTagLine(details['tagline'])
    if details.get('runtimeMinutes'):
        vtag.setDuration(details['runtimeMinutes'] * 60)
    crew = [m for m in (details.get('crew') or []) if isinstance(m, dict)]
    directors = [m.get('name') for m in crew if (m.get('job') or '').lower() == 'director']
    writers = [m.get('name') for m in crew
               if (m.get('job') or '').lower() in ('writer', 'screenplay', 'story', 'teleplay')]
    if directors:
        vtag.setDirectors(directors)
    if writers:
        vtag.setWriters(writers)

    collection = details.get('collection')
    if collection:
        vtag.setSet(collection.get('name') or '')
        if collection.get('overview'):
            vtag.setSetOverview(collection['overview'])
        if collection.get('posterUrl'):
            vtag.addAvailableArtwork(collection['posterUrl'], 'set.poster')
        if collection.get('backdropUrl'):
            vtag.addAvailableArtwork(collection['backdropUrl'], 'set.fanart')
        sync_collection_art(collection)
    t_collection_art = time.time()

    apply_ratings(vtag, details.get('ratings'))
    apply_artwork(listitem, details.get('artwork'))

    # Rating + resume reconciliation, inline with this same scrape -- per-user
    # request (2026-08-30): "I don't want a separate sync task in Kodi for
    # ratings. this needs to happen with the scraper automatically as part of
    # the scrape process." Rating is push-only (see progress_sync module doc:
    # Kodi has no per-rating timestamp to compare against). Resume is genuinely
    # bidirectional, using Kodi's own lastplayed the same way Chronicle_Scrobbler's
    # now-retired periodic sync did.
    if details.get('userRating'):
        vtag.setUserRating(details['userRating'])

    kodi_state = progress_sync.lookup_movie_state(details.get('title'), details.get('year'))
    direction, value = progress_sync.resolve_progress_direction(
        details.get('resumePositionPercent'), details.get('resumeUpdatedAt'), kodi_state)
    if direction == 'push':
        progress_sync.apply_resume_push(vtag, value, details.get('runtimeMinutes'))
    elif direction == 'pull':
        ChronicleClient().push_resume(
            media_item_id, value, progress_sync.kodi_lastplayed_to_iso(kodi_state.get('lastplayed')))

    # Fully-watched reconciliation -- separate from resume above on purpose. Chronicle clears
    # resumePositionPercent/resumeUpdatedAt to null the moment an item is marked watched
    # (nothing left to "resume"), so a completed item gives resolve_progress_direction nothing
    # to compare and it correctly no-ops. isWatched/lastWatchedAt are never cleared, so this
    # is the only path that can ever sync a finished watch onto a Kodi instance that's never
    # played the item. Confirmed live (2026-09-05): a movie completed on one Shield stayed
    # permanently unwatched on another with no error, since nothing was actually wrong --
    # nothing was trying to reconcile watched status at all.
    watched_direction, watched_value = progress_sync.resolve_watched_direction(
        details.get('isWatched'), details.get('lastWatchedAt'), kodi_state)
    if watched_direction == 'push':
        progress_sync.apply_watched_push(vtag, watched_value)
    elif watched_direction == 'pull':
        ChronicleClient().push_watched(
            media_item_id, progress_sync.kodi_lastplayed_to_iso(watched_value))
    t_progress_sync = time.time()

    sync_movie_art(details.get('title'), details.get('year'), details.get('artwork'), location=location)
    t_movie_art = time.time()

    if details.get('cast'):
        vtag.setCast([
            xbmc.Actor(name=actor.get('name') or '', role=actor.get('role') or '', order=i)
            for i, actor in enumerate(details['cast'])
        ])

    trailer_uri = youtube_trailer_uri(details.get('trailerUrl'))
    if trailer_uri:
        vtag.setTrailer(trailer_uri)

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    log.info(
        'getdetails: "{0}" timing -- chronicle api: {1:.2f}s, find location: {2:.2f}s, '
        'collection art: {3:.2f}s, progress/rating sync: {4:.2f}s, movie art: {5:.2f}s, '
        'total: {6:.2f}s'.format(
            details.get('title'),
            t_chronicle - t_start,
            t_location - t_chronicle,
            t_collection_art - t_location,
            t_progress_sync - t_collection_art,
            t_movie_art - t_progress_sync,
            time.time() - t_start))
    return True


def get_artwork(media_item_id, handle):
    """Kodi's "getartwork" action -- fired by the art picker's own "Refresh"
    button (distinct from a full library "Refresh information", which goes
    through get_details() above). Not implemented by Team Kodi's own bundled
    movies scraper (only their TV one), but Kodi calls it in practice, so
    leaving it unhandled meant re-running just an artwork refresh silently
    did nothing."""
    if media_item_id is None:
        log.warning('getartwork: called with no resolvable media_item_id')
        return False

    details = ChronicleClient().get_movie_details(media_item_id)
    _log_details_summary('getartwork', media_item_id, details)
    if not details:
        return False
    activity_tracker.mark_active(details.get('title') or str(media_item_id))

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    apply_artwork(listitem, details.get('artwork'))
    sync_movie_art(details.get('title'), details.get('year'), details.get('artwork'))

    collection = details.get('collection')
    if collection:
        vtag = listitem.getVideoInfoTag()
        if collection.get('posterUrl'):
            vtag.addAvailableArtwork(collection['posterUrl'], 'set.poster')
        if collection.get('backdropUrl'):
            vtag.addAvailableArtwork(collection['backdropUrl'], 'set.fanart')
        sync_collection_art(collection)

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    return True


def _resolve_lookup_id(params):
    """getartwork's id can arrive as a bare int (Kodi's own convention, per the
    real TV scraper's params.get('id')) or as our own lookup-string JSON under
    'url' -- accept either rather than guessing wrong and silently no-opping."""
    raw = params.get('id') or params.get('url')
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return parse_lookup_string(raw)


def _parse_year(raw):
    """Kodi's find querystring carries year as a plain string (get_params()
    never converts any value); year_tolerant_match() does int arithmetic on
    it, so a raw string reaching that far throws 'unsupported operand
    type(s) for -: int and str' and aborts the whole filename pre-check for
    every title, not just ones with a genuinely missing/malformed year."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def run():
    params = get_params(sys.argv[1:])
    enddir = True

    action = params.get('action')
    if action == 'find' and 'title' in params:
        search_for_movie(params['title'], _parse_year(params.get('year')), params['handle'])
    elif action == 'getdetails' and 'url' in params:
        enddir = not get_details(parse_lookup_string(params['url']), params['handle'])
    elif action == 'getartwork':
        enddir = not get_artwork(_resolve_lookup_id(params), params['handle'])
    else:
        log.warning('unhandled or missing action: {0}'.format(action))

    if enddir:
        xbmcplugin.endOfDirectory(params['handle'])


if __name__ == '__main__':
    run()
