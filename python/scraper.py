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
from urllib.parse import parse_qsl

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
from lib.chronicle_client import ChronicleClient
from lib.kodi_video_info import (
    apply_common_video_info, apply_ratings, apply_artwork, youtube_trailer_uri,
)
from lib.collection_sync import sync_collection_art
from lib.movie_art_sync import sync_movie_art, find_movie_location
from lib.nfo_writer import sync_movie_nfo

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
    result = ChronicleClient().search_movie(title, year)
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

    details = ChronicleClient().get_movie_details(media_item_id)
    _log_details_summary('getdetails', media_item_id, details)
    if not details:
        return False

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setMediaType('movie')

    apply_common_video_info(vtag, details)

    if details.get('tagline'):
        vtag.setTagLine(details['tagline'])
    if details.get('runtimeMinutes'):
        vtag.setDuration(details['runtimeMinutes'] * 60)
    if details.get('directors'):
        vtag.setDirectors(details['directors'])

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

    apply_ratings(vtag, details.get('ratings'))
    apply_artwork(listitem, details.get('artwork'))

    # Resolved once and reused by both syncs below -- both would otherwise
    # independently browse Kodi's video sources to find the same folder.
    location = find_movie_location(details.get('title'), details.get('year'))
    sync_movie_art(details.get('title'), details.get('year'), details.get('artwork'), location=location)

    if ADDON.getSettingBool('write_nfo'):
        sync_movie_nfo(details.get('title'), details.get('year'), details, location=location)

    if details.get('cast'):
        listitem.setCast([
            {'name': name, 'role': '', 'order': i}
            for i, name in enumerate(details['cast'])
        ])

    trailer_uri = youtube_trailer_uri(details.get('trailerUrl'))
    if trailer_uri:
        vtag.setTrailer(trailer_uri)

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
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


def run():
    params = get_params(sys.argv[1:])
    enddir = True

    action = params.get('action')
    if action == 'find' and 'title' in params:
        search_for_movie(params['title'], params.get('year'), params['handle'])
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
