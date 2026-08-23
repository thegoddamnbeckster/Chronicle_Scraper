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
import posixpath
import sys
import urllib.parse
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

# See python/scraper.py's identical comment: Kodi only puts this script's own
# directory (python/) on sys.path for xbmc.metadata.scraper.* entries, not the
# addon root where lib/ actually lives.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.logger import Logger
from lib import activity_tracker
from lib import legacy_nfo
from lib import rebuild_state
from lib.chronicle_client import ChronicleClient
from lib.kodi_video_info import apply_common_video_info, apply_ratings, apply_artwork
from lib.movie_art_sync import strip_video_ext
from lib.tv_nfo_writer import sync_show_nfo, sync_episode_nfo
from lib.tvshow_location import find_show_location, get_episode

log = Logger('tvshow_scraper')
ADDON = xbmcaddon.Addon()


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
    activity_tracker.mark_active(title)
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


def _merge_legacy_gaps(details, legacy_data, keys):
    """Fills in any field `details` has nothing for using a harvested legacy
    NFO's data (see lib/legacy_nfo.py) -- Chronicle's own data always wins
    where it has any, this only plugs genuine gaps. keys is the flat list
    of scalar/list field names to consider (list-vs-scalar doesn't matter
    here: "not details.get(key)" is falsy for both an empty list and a
    missing/empty string alike)."""
    for key in keys:
        if not details.get(key) and legacy_data.get(key):
            details[key] = legacy_data[key]


def get_details(show_id, handle):
    if show_id is None:
        return False

    details = ChronicleClient().get_show_details(show_id)
    if not details:
        return False
    activity_tracker.mark_active(details.get('title') or str(show_id))

    # find_show_location()/the legacy-NFO harvest below only ever feed the
    # NFO write further down (no local art sync consumes them at the show
    # level, unlike movies) -- both stay skipped entirely outside a rebuild
    # pass, same gate as the NFO write itself, so an ordinary scan doesn't
    # pay for find_show_location()'s own VideoLibrary lookup (and retry) for
    # a result nothing this pass would use. See lib/rebuild_state.py.
    location = (None, None)
    if rebuild_state.is_active():
        # Resolved once, here, before the ListItem below is built, so a
        # legacy-NFO merge (just below) benefits everything downstream --
        # Kodi's own display included, not just the NFO. Same ordering fix
        # as python/scraper.py's get_details() -- see that function's own
        # comment for why this has to happen before apply_common_video_info()
        # runs.
        folder, tvshowid = find_show_location(details.get('title'), details.get('year'))
        location = (folder, tvshowid)

        # If nfo_rebuild.py's rebuild action ran against this show, whatever
        # its previous tvshow.nfo contained (e.g. from tinyMediaManager) was
        # harvested and stashed before deletion -- see lib/legacy_nfo.py.
        # Pick it up now (one-shot), fill any gap Chronicle's own data has,
        # and feed it back into Chronicle itself so it isn't lost.
        stash_key = posixpath.basename(folder.rstrip('/')) if folder else None
        legacy_data = legacy_nfo.load_and_clear_stash(stash_key) if stash_key else None
        if legacy_data:
            _merge_legacy_gaps(details, legacy_data, (
                'title', 'overview', 'year', 'premiered', 'mpaa', 'country',
                'studio', 'status', 'runtimeMinutes', 'genres', 'tags', 'cast',
                'ratings',
            ))
            ChronicleClient().contribute_metadata(show_id, 'chronicle_scraper.legacy_nfo', legacy_data)

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    vtag = listitem.getVideoInfoTag()
    vtag.setMediaType('tvshow')
    vtag.setTvShowTitle(details.get('title') or '')

    apply_common_video_info(vtag, details)

    if details.get('tagline'):
        vtag.setTagLine(details['tagline'])
    if details.get('status'):
        vtag.setTvShowStatus(details['status'])
    if details.get('runtimeMinutes'):
        vtag.setDuration(details['runtimeMinutes'] * 60)

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
        vtag.setCast([
            xbmc.Actor(name=actor.get('name') or '', role=actor.get('role') or '', order=i)
            for i, actor in enumerate(details['cast'])
        ])

    # NFO writing only ever happens as part of an explicit rebuild pass -- see
    # lib/rebuild_state.py and python/scraper.py's own identical gate.
    if ADDON.getSettingBool('write_nfo') and rebuild_state.is_active():
        sync_show_nfo(details.get('title'), details.get('year'), details, location=location)

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
    episode_label = '{0} - {1}'.format(details.get('showTitle'), details.get('title')) \
        if details.get('showTitle') else (details.get('title') or str(episode_id))
    activity_tracker.mark_active(episode_label)

    # find_show_location()/get_episode()/the legacy-NFO harvest below only
    # ever feed the NFO write further down -- nothing else here consumes
    # them -- so all of it stays skipped entirely outside a rebuild pass,
    # same gate as the write itself. See lib/rebuild_state.py. This also
    # means an ordinary scan never pays for find_show_location()'s and
    # get_episode()'s own VideoLibrary lookups for a result nothing this
    # pass would use.
    folder = video_basename = streamdetails = None
    if rebuild_state.is_active():
        # Locate the episode's own file, the same way python/scraper.py
        # locates a movie's -- Kodi's find/getepisodedetails contract never
        # hands this script a file path any more than the movies one does.
        # showTitle/showYear (the PARENT show's, not this episode's) is what
        # Chronicle's /tv/episode-details response carries for exactly this
        # purpose -- see ScraperController.GetEpisodeDetails server-side.
        show_title = details.get('showTitle')
        if show_title:
            _show_folder, tvshowid = find_show_location(show_title, details.get('showYear'))
            if tvshowid is not None:
                # Kodi's VideoLibrary.GetEpisodes returns file path and
                # streamdetails together in one call -- there's no cheaper
                # way to get just the file path, so this always fetches
                # both, but streamdetails is only ever kept (and written
                # into the NFO) when write_streamdetails is on. See
                # python/scraper.py's own comment for why that's opt-in.
                file_path, episode_streamdetails = get_episode(tvshowid, details.get('season'), details.get('episode'))
                if file_path:
                    folder = posixpath.dirname(file_path) + '/'
                    video_basename = strip_video_ext(posixpath.basename(file_path))
                if ADDON.getSettingBool('write_streamdetails'):
                    streamdetails = episode_streamdetails

        # If nfo_rebuild.py's rebuild action ran against this episode,
        # whatever its previous NFO contained (e.g. from tinyMediaManager)
        # was harvested and stashed before deletion -- see lib/legacy_nfo.py.
        # Pick it up now (one-shot), fill any gap Chronicle's own data has,
        # and feed it back into Chronicle. Done before the ListItem below is
        # built, same ordering fix as python/scraper.py's get_details() (and
        # this function's own show-level sibling above) -- see those for why.
        legacy_data = legacy_nfo.load_and_clear_stash(video_basename) if video_basename else None
        if legacy_data:
            _merge_legacy_gaps(details, legacy_data, (
                'title', 'overview', 'aired', 'runtimeMinutes', 'cast', 'crew', 'ratings',
            ))
            ChronicleClient().contribute_metadata(episode_id, 'chronicle_scraper.legacy_nfo', legacy_data)

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
    crew = [m for m in (details.get('crew') or []) if isinstance(m, dict)]
    directors = [m.get('name') for m in crew if (m.get('job') or '').lower() == 'director']
    writers = [m.get('name') for m in crew
               if (m.get('job') or '').lower() in ('writer', 'screenplay', 'story', 'teleplay')]
    if directors:
        vtag.setDirectors(directors)
    if writers:
        vtag.setWriters(writers)

    ids = details.get('externalIds') or {}
    unique_ids = {k: v for k, v in (
        ('imdb', ids.get('imdb')), ('tmdb', ids.get('tmdb')),
        ('tvdb', ids.get('tvdb')), ('trakt', ids.get('trakt')),
    ) if v}
    if unique_ids:
        vtag.setUniqueIDs(unique_ids, 'imdb' if 'imdb' in unique_ids else next(iter(unique_ids)))

    apply_ratings(vtag, details.get('ratings'))

    if details.get('cast'):
        vtag.setCast([
            xbmc.Actor(name=actor.get('name') or '', role=actor.get('role') or '', order=i)
            for i, actor in enumerate(details['cast'])
        ])
    if details.get('thumbUrl'):
        listitem.setArt({'thumb': details['thumbUrl']})
        vtag.addAvailableArtwork(details['thumbUrl'], 'thumb')

    # NFO writing only ever happens as part of an explicit rebuild pass -- see
    # lib/rebuild_state.py and python/scraper.py's own identical gate.
    if ADDON.getSettingBool('write_nfo') and rebuild_state.is_active():
        sync_episode_nfo(details, folder, video_basename, streamdetails=streamdetails)

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
    activity_tracker.mark_active(details.get('title') or str(show_id))

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

    action = params.get('action')
    if action == 'find' and 'title' in params:
        find_show(params['title'], params.get('year'), params['handle'])
    elif action == 'getdetails' and 'url' in params:
        get_details(parse_lookup_string(params['url']), params['handle'])
    elif action == 'getepisodelist' and 'url' in params:
        get_episode_list(params['url'], params['handle'])
    elif action == 'getepisodedetails' and 'url' in params:
        get_episode_details(params['url'], params['handle'])
    elif action == 'getartwork':
        get_artwork(_resolve_lookup_id(params), params['handle'])
    else:
        log.warning('unhandled or missing action: {0}'.format(action))

    # Unconditional, unlike python/scraper.py's movies run() (which only calls
    # this when get_details() returned False -- ground-truthed there against
    # Team Kodi's own metadata.themoviedb.org.python, whose run() does the
    # identical "enddir = not get_details(...)" thing). The TV contract is
    # genuinely different: Team Kodi's own bundled TV scraper
    # (metadata.tvshows.themoviedb.org.python, libs/actions.py's router())
    # calls xbmcplugin.endOfDirectory() after EVERY action with no exception
    # -- including getdetails/getepisodedetails/getartwork, even though each
    # of those already called setResolvedUrl(). This file previously copied
    # the movies pattern here too (enddir = not get_details(...) etc.), which
    # left Kodi's plugin handle never explicitly finished on a *successful*
    # getdetails/getepisodedetails -- no exception, nothing in kodi.log, the
    # show/episode just never finished being committed to the library. Movies
    # never surfaced this because skipping it on success is the movies
    # contract's own correct behaviour, not a general rule that also applies
    # to TV.
    xbmcplugin.endOfDirectory(params['handle'])


if __name__ == '__main__':
    run()
