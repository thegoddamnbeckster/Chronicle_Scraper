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
from lib import episode_path_cache
from lib import legacy_nfo
from lib import rebuild_state
from lib.chronicle_client import ChronicleClient
from lib.kodi_video_info import apply_common_video_info, apply_ratings, apply_artwork
from lib.movie_art_sync import strip_video_ext
from lib.tv_nfo_writer import sync_show_nfo, sync_episode_nfo
from lib.tvshow_location import find_show_location, get_episode
from lib import progress_sync

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

    # Rating reconciliation, inline with this same scrape -- per-user request
    # (2026-08-30): "I don't want a separate sync task in Kodi for ratings."
    # Push-only: shows have no per-item resume concept in Kodi (that's
    # per-episode only, see get_episode_details() below), and Kodi exposes no
    # "when was this rating set" signal to compare against, so Chronicle's
    # rating always wins -- same one-directional design already validated in
    # Chronicle_Scrobbler before this moved into the scraper.
    if details.get('userRating'):
        vtag.setUserRating(details['userRating'])

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
    log.info('get_details: show_id={0} title={1!r} -- setResolvedUrl sent to Kodi'.format(
             show_id, details.get('title')))
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
    # Resolved unconditionally (not just during a rebuild pass) so rating +
    # resume reconciliation below always has tvshowid available -- per-user
    # request (2026-08-30): "this needs to happen with the scraper
    # automatically as part of the scrape process," not gated to an explicit
    # rebuild. showTitle/showYear (the PARENT show's, not this episode's) is
    # what Chronicle's /tv/episode-details response carries for exactly this
    # purpose -- see ScraperController.GetEpisodeDetails server-side.
    show_title = details.get('showTitle')
    tvshowid = None
    if show_title:
        _show_folder, tvshowid = find_show_location(show_title, details.get('showYear'))

    if rebuild_state.is_active():
        # Locate the episode's own file, the same way python/scraper.py
        # locates a movie's -- Kodi's find/getepisodedetails contract never
        # hands this script a file path any more than the movies one does.
        if tvshowid is not None:
            # Kodi's VideoLibrary.GetEpisodes returns file path and
            # streamdetails together in one call -- there's no cheaper
            # way to get just the file path, so this always fetches
            # both, but streamdetails is only ever kept (and written
            # into the NFO) when write_streamdetails is on. See
            # python/scraper.py's own comment for why that's opt-in.
            file_path, episode_streamdetails = get_episode(tvshowid, details.get('season'), details.get('episode'))
            # This is purely a rebuild-pass NFO/legacy-harvest lookup --
            # it does NOT gate whether the episode itself loads into
            # Kodi's library (setResolvedUrl() below runs regardless of
            # file_path). Logged so it's visible whether VideoLibrary
            # already has this episode's file at rebuild time, distinct
            # from whether the episode gets committed at all (that's the
            # endOfDirectory log lines in run()).
            log.info('get_episode_details: tvshowid={0} S{1}E{2} -- VideoLibrary lookup found file_path={3!r}'.format(
                     tvshowid, details.get('season'), details.get('episode'), file_path))
            if not file_path:
                # Confirmed live via kodi.log (2026-08-28): during a
                # rebuild pass, the VideoLibrary lookup just above comes
                # back empty for essentially every episode -- not
                # because the file is missing, but because this exact
                # episode's own RefreshEpisode() is what's currently
                # running this very callback, and its library row isn't
                # recommitted until this callback returns (see
                # episode_path_cache.py's module docstring). Fall back to
                # nfo_rebuild.py's own pre-refresh known-good path for
                # this episode, stashed there for exactly this gap.
                file_path = episode_path_cache.load_and_clear(
                    tvshowid, details.get('season'), details.get('episode'))
                if file_path:
                    log.info('get_episode_details: tvshowid={0} S{1}E{2} -- using nfo_rebuild.py\'s '
                             'cached pre-refresh path instead: {3!r}'.format(
                             tvshowid, details.get('season'), details.get('episode'), file_path))
                else:
                    log.warning('get_episode_details: tvshowid={0} S{1}E{2} -- VideoLibrary lookup '
                                'found no file AND no cached pre-refresh path is available -- this '
                                'episode\'s NFO will NOT be written this pass'.format(
                                tvshowid, details.get('season'), details.get('episode')))
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

    # Rating + resume reconciliation, inline with this same scrape -- per-user
    # request (2026-08-30): "I don't want a separate sync task in Kodi for
    # ratings. this needs to happen with the scraper automatically as part of
    # the scrape process." Rating is push-only (see progress_sync module doc).
    # Resume is genuinely bidirectional, using Kodi's own lastplayed the same
    # way Chronicle_Scrobbler's now-retired periodic sync did -- tvshowid was
    # already resolved above (unconditionally, not just during a rebuild) so
    # this lookup is available on every ordinary episode scrape.
    if details.get('userRating'):
        vtag.setUserRating(details['userRating'])

    kodi_state = progress_sync.lookup_episode_state(tvshowid, details.get('season'), details.get('episode'))
    direction, value = progress_sync.resolve_progress_direction(
        details.get('resumePositionPercent'), details.get('resumeUpdatedAt'), kodi_state)
    if direction == 'push':
        progress_sync.apply_resume_push(vtag, value, details.get('runtimeMinutes'))
    elif direction == 'pull':
        ChronicleClient().push_resume(
            episode_id, value, progress_sync.kodi_lastplayed_to_iso(kodi_state.get('lastplayed')))

    # Fully-watched reconciliation -- separate from resume above on purpose, see
    # progress_sync.resolve_watched_direction's own doc (sibling of the movie addon's
    # identical 2026-09-05 fix: an episode completed on one Shield stayed permanently
    # unwatched on another, since resumePositionPercent/resumeUpdatedAt are cleared to null
    # on completion and gave resolve_progress_direction nothing to compare).
    watched_direction, watched_value = progress_sync.resolve_watched_direction(
        details.get('isWatched'), details.get('lastWatchedAt'), kodi_state)
    if watched_direction == 'push':
        progress_sync.apply_watched_push(vtag, watched_value)
    elif watched_direction == 'pull':
        ChronicleClient().push_watched(
            episode_id, progress_sync.kodi_lastplayed_to_iso(watched_value))

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
    log.info('get_episode_details: episode_id={0} S{1}E{2} title={3!r} -- setResolvedUrl sent to Kodi'.format(
             episode_id, details.get('season'), details.get('episode'), details.get('title')))
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
    log.info('run: dispatching action={0!r} handle={1!r} params={2!r}'.format(
             action, params.get('handle'), {k: v for k, v in params.items() if k != 'handle'}))

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
    #
    # THIS is the actual "load the file into the TV library" moment for
    # Kodi's own bookkeeping: until this call, Kodi's plugin handle for this
    # find/getdetails/getepisodedetails/getartwork invocation is still open,
    # and the show/episode this action was scraping does not finish
    # committing to VideoLibrary no matter what setResolvedUrl()/
    # addDirectoryItem() already sent it. If this log line stops appearing
    # for a given action, or appears without ever being followed by
    # "run: endOfDirectory called" further down, that's the bug this fix
    # addresses re-occurring -- not a network/Chronicle timeout (those
    # surface as their own [client]/[scraper] log lines well before this
    # point is ever reached).
    log.info('run: about to call endOfDirectory for action={0!r} handle={1!r}'.format(
             action, params.get('handle')))
    xbmcplugin.endOfDirectory(params['handle'])
    log.info('run: endOfDirectory called for action={0!r} handle={1!r} -- directory call finished'.format(
             action, params.get('handle')))


if __name__ == '__main__':
    run()
