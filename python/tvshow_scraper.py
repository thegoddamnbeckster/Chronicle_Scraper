# -*- coding: utf-8 -*-
"""Kodi metadata-scraper entry point (xbmc.metadata.scraper.tvshows).

Ground-truthed against Team Kodi's own bundled metadata.tvshows.themoviedb.org.python
addon (libs/actions.py + libs/data_utils.py) -- the real four-action TV scraper
contract, which is a superset of the movies contract:

  action=find            -> find_show(): one candidate per addDirectoryItem, same
                             "Chronicle already picked one answer" pattern as movies.
  action=getdetails       -> get_details(): show-level info, including every season
                             Chronicle already has. Also sets an episode guide string
                             (via InfoTagVideo.setEpisodeGuide) that Kodi hands back
                             verbatim as the "url" param to getepisodelist.
  action=getepisodelist   -> get_episode_list(): one addDirectoryItem per episode
                             Chronicle already has under this show.
  action=getepisodedetails-> get_episode_details(): full details for one episode.

Not implemented: NfoUrl and getartwork actions, same known-gap precedent as the
movies scraper's own NfoUrl omission -- see README.

Deliberately thin, same as python/scraper.py: this file only translates between
Kodi's plugin-handle protocol and Chronicle's HTTP API. All resolve-or-create
and cross-provider aggregation logic lives in Chronicle's own ScraperController.
"""

import json
import os
import sys
import urllib.parse
from urllib.parse import parse_qsl

import xbmcgui
import xbmcplugin

# See python/scraper.py's identical comment: Kodi only puts this script's own
# directory (python/) on sys.path for xbmc.metadata.scraper.* entries, not the
# addon root where lib/ actually lives.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.kodi_video_info import apply_common_video_info, apply_ratings, apply_artwork

log = Logger('tvshow_scraper')


def get_params(argv):
    """Same handle+querystring convention as python/scraper.py."""
    result = {'handle': int(argv[0])}
    if len(argv) < 2 or not argv[1]:
        return result
    result.update(parse_qsl(argv[1].lstrip('?')))
    return result


def build_lookup_string(media_item_id) -> str:
    return json.dumps({'chronicleId': media_item_id})


def parse_lookup_string(value):
    try:
        return json.loads(value).get('chronicleId')
    except (ValueError, AttributeError, TypeError):
        log.warning("Can't parse lookup string, is it from another addon? {0!r}".format(value))
        return None


def find_show(title, year, handle):
    log.info('find: title={0!r} year={1!r}'.format(title, year))
    result = ChronicleClient().search_show(title, year)
    if not result:
        return

    label = result.get('title') or title
    if result.get('year'):
        label = '{0} ({1})'.format(label, result['year'])

    listitem = xbmcgui.ListItem(label, offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setTitle(result.get('title') or title)
    vtag.setTvShowTitle(result.get('title') or title)
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


def get_details(show_id, handle):
    if show_id is None:
        return False

    details = ChronicleClient().get_show_details(show_id)
    if not details:
        return False

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setMediaType('tvshow')
    vtag.setTvShowTitle(details.get('title') or '')

    apply_common_video_info(vtag, details)

    if details.get('tagline'):
        vtag.setTagLine(details['tagline'])
    if details.get('status'):
        vtag.setTvShowStatus(details['status'])

    for season in details.get('seasons') or []:
        number = season.get('number')
        if number is None:
            continue
        vtag.addSeason(number, season.get('name') or '')
        if season.get('posterUrl'):
            vtag.addAvailableArtwork(season['posterUrl'], 'poster', season=number)

    apply_ratings(vtag, details.get('ratings'))
    apply_artwork(listitem, details.get('artwork'))

    if details.get('cast'):
        listitem.setCast([
            {'name': name, 'role': '', 'order': i}
            for i, name in enumerate(details['cast'])
        ])

    # Kodi echoes this back verbatim as the "url" param to getepisodelist.
    vtag.setEpisodeGuide(build_lookup_string(show_id))

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    return True


def get_episode_list(show_ids, handle):
    show_id = parse_lookup_string(show_ids)
    if show_id is None:
        log.error('no chronicleId found in episode guide string: {0!r}'.format(show_ids))
        return

    episodes = ChronicleClient().get_episode_list(show_id)
    if not episodes:
        return

    for episode in episodes:
        listitem = xbmcgui.ListItem(episode.get('title') or 'Episode {0}'.format(episode.get('episode')),
                                     offscreen=True)
        vtag = listitem.getVideoInfoTag()
        vtag.setTitle(episode.get('title') or '')
        vtag.setSeason(episode.get('season') or 0)
        vtag.setEpisode(episode.get('episode') or 0)

        xbmcplugin.addDirectoryItem(
            handle=handle,
            url=build_lookup_string(episode['id']),
            listitem=listitem,
            isFolder=True,
        )


def get_episode_details(encoded_ids, handle):
    episode_id = parse_lookup_string(urllib.parse.unquote(encoded_ids))
    if episode_id is None:
        return False

    details = ChronicleClient().get_episode_details(episode_id)
    if not details:
        return False

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setMediaType('episode')
    vtag.setTitle(details.get('title') or '')
    vtag.setSeason(details.get('season') or 0)
    vtag.setEpisode(details.get('episode') or 0)
    if details.get('overview'):
        vtag.setPlot(details['overview'])
        vtag.setPlotOutline(details['overview'])
    if details.get('year'):
        vtag.setYear(details['year'])
    if details.get('directors'):
        vtag.setDirectors(details['directors'])

    ids = details.get('externalIds') or {}
    unique_ids = {k: v for k, v in (
        ('imdb', ids.get('imdb')), ('tmdb', ids.get('tmdb')),
        ('tvdb', ids.get('tvdb')), ('trakt', ids.get('trakt')),
    ) if v}
    if unique_ids:
        vtag.setUniqueIDs(unique_ids, 'imdb' if 'imdb' in unique_ids else next(iter(unique_ids)))

    apply_ratings(vtag, details.get('ratings'))

    if details.get('cast'):
        listitem.setCast([
            {'name': name, 'role': '', 'order': i}
            for i, name in enumerate(details['cast'])
        ])
    if details.get('thumbUrl'):
        listitem.setArt({'thumb': details['thumbUrl']})
        vtag.addAvailableArtwork(details['thumbUrl'], 'thumb')

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    return True


def get_artwork(show_id, handle):
    """Kodi's "getartwork" action for shows -- ground-truthed against Team
    Kodi's own metadata.tvshows.themoviedb.org.python (libs/actions.py's own
    get_artwork(show_id), called via params.get('id')). Fired by the art
    picker's own "Refresh" button, distinct from a full "Refresh information"
    (get_details() above)."""
    if show_id is None:
        return False

    details = ChronicleClient().get_show_details(show_id)
    if not details:
        return False

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    apply_artwork(listitem, details.get('artwork'))

    vtag = listitem.getVideoInfoTag()
    for season in details.get('seasons') or []:
        if season.get('posterUrl') and season.get('number') is not None:
            vtag.addAvailableArtwork(season['posterUrl'], 'poster', season=season['number'])

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    return True


def _resolve_lookup_id(params):
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
        find_show(params['title'], params.get('year'), params['handle'])
    elif action == 'getdetails' and 'url' in params:
        enddir = not get_details(parse_lookup_string(params['url']), params['handle'])
    elif action == 'getepisodelist' and 'url' in params:
        get_episode_list(params['url'], params['handle'])
    elif action == 'getepisodedetails' and 'url' in params:
        enddir = not get_episode_details(params['url'], params['handle'])
    elif action == 'getartwork':
        enddir = not get_artwork(_resolve_lookup_id(params), params['handle'])
    else:
        log.warning('unhandled or missing action: {0}'.format(action))

    if enddir:
        xbmcplugin.endOfDirectory(params['handle'])


if __name__ == '__main__':
    run()
