# -*- coding: utf-8 -*-
"""Full-library sync-check: after every Kodi video library scan finishes -- whatever triggered
it (native startup auto-scan, the user's own manual "Update Library", or this addon's own
new-content-detection feature) -- walks every episode Kodi's own VideoLibrary already has and
verifies it still matches Chronicle's current data, pushing a correction only for whatever
field(s) actually disagree.

Per-user direction (2026-09-22): "Kodi has all of the file paths. You should be able to use
that to definitively figure out what item is what in Chronicle." Resolves each Kodi episode to
its Chronicle item purely by the exact video file's own basename
(ChronicleClient.get_episode_details_by_file), never by title/season/episode matching and never
by Chronicle's own kodi_library_ids bookkeeping table -- that table only covers items a NORMAL
scrape has already successfully reported, and can't be trusted as a complete inventory the way
Kodi's own file list is. See the movie addon's own lib/full_sync_check.py for the full
root-cause writeup this mirrors ("Ghostbusters (2016)" scraped with the wrong movie's data and
never corrected).

Per-user direction: only ever updates a field that actually differs from what Chronicle has --
"you're only doing this for items that are not the same as what Chronicle has" -- never a blind
overwrite of everything on every pass. Only ever calls VideoLibrary.SetEpisodeDetails, never
writes an NFO file and never involves Chronicle's own server calling this device's JSON-RPC --
same pull-only architecture as every other feature in this addon.
"""

import json
import posixpath

import xbmc

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('full_sync_check')

_EPISODE_PROPERTIES = ['file', 'title', 'plot', 'firstaired', 'art', 'season', 'episode']


def _get_all_tvshows():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetTVShows',
        'params': {'properties': ['title']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("full_sync_check: couldn't get TV show list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('full_sync_check: VideoLibrary.GetTVShows rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('tvshows') or []


def _get_episodes_for_show(tvshowid):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetEpisodes',
        'params': {'tvshowid': tvshowid, 'properties': _EPISODE_PROPERTIES},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("full_sync_check: couldn't get episodes for tvshowid {0}: {1}".format(tvshowid, exc))
        return []
    if 'error' in response:
        log.error('full_sync_check: VideoLibrary.GetEpisodes rejected tvshowid {0}: {1}'.format(
                  tvshowid, response['error']))
        return []
    return response.get('result', {}).get('episodes') or []


def diff_episode(kodi_item, details):
    """Returns a dict of VideoLibrary.SetEpisodeDetails params for whatever fields actually
    differ between what Kodi currently has (kodi_item, a VideoLibrary.GetEpisodes entry using
    this module's own _EPISODE_PROPERTIES) and what Chronicle currently has (details, a
    get_episode_details()-shaped dict) -- or {} if everything already matches. Only ever adds a
    key when Chronicle actually has a value for it AND that value disagrees with Kodi's own --
    a field Chronicle has nothing for is left alone rather than blanked out."""
    updates = {}

    if details.get('title') and kodi_item.get('title') != details['title']:
        updates['title'] = details['title']
    if details.get('overview') and kodi_item.get('plot') != details['overview']:
        updates['plot'] = details['overview']

    aired = details.get('aired')
    if aired and kodi_item.get('firstaired') != aired[:10]:
        updates['firstaired'] = aired[:10]

    thumb = details.get('thumbUrl')
    kodi_thumb = (kodi_item.get('art') or {}).get('thumb')
    if thumb and kodi_thumb != thumb:
        art = dict(kodi_item.get('art') or {})
        art['thumb'] = thumb
        updates['art'] = art

    return updates


def _set_episode_details(episodeid, updates):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetEpisodeDetails',
        'params': dict(updates, episodeid=episodeid),
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning('full_sync_check: SetEpisodeDetails({0}) failed: {1}'.format(episodeid, exc))
        return False
    if 'error' in response:
        log.warning('full_sync_check: SetEpisodeDetails({0}) rejected: {1}'.format(
                    episodeid, response['error']))
        return False
    return True


def run(is_cancelled=None, progress_callback=None):
    """Best-effort throughout: one episode failing to resolve or update never stops the rest.
    Returns a summary dict; never raises.

    Checks connectivity up front and aborts the whole pass if Chronicle isn't reachable -- see
    the movie addon's own full_sync_check.run() for the full reasoning (ChronicleClient._get()
    returns None identically for "no match" and "Chronicle unreachable", so without this an
    outage would silently look like a completely clean library instead of the error it is)."""
    is_cancelled = is_cancelled or (lambda: False)
    client = ChronicleClient()

    reachable, message = client.test_connection()
    if not reachable:
        log.error('full_sync_check: aborting -- Chronicle not reachable: {0}'.format(message))
        return {'checked': 0, 'updated': 0, 'errors': 0, 'cancelled': False,
                'aborted': 'Chronicle not reachable: {0}'.format(message)}

    shows = _get_all_tvshows()
    episodes = []
    for show in shows:
        if is_cancelled():
            break
        episodes.extend(_get_episodes_for_show(show['tvshowid']))
    log.info('full_sync_check: checking {0} episode(s) across {1} show(s) against '
              'Chronicle'.format(len(episodes), len(shows)))

    checked = 0
    updated = 0
    errors = 0
    for index, episode in enumerate(episodes):
        if is_cancelled():
            break
        if progress_callback:
            progress_callback(index, len(episodes), episode.get('title') or '')

        file_path = episode.get('file')
        if not file_path:
            continue
        file_name = posixpath.basename(file_path)

        details = client.get_episode_details_by_file(
            file_name, season=episode.get('season'), episode=episode.get('episode'))
        if not details:
            continue  # nothing unambiguous in Chronicle for this exact file -- nothing to sync
        checked += 1

        updates = diff_episode(episode, details)
        if not updates:
            continue

        if _set_episode_details(episode['episodeid'], updates):
            updated += 1
            log.info('full_sync_check: "{0}" -- updated {1}'.format(
                episode.get('title'), ', '.join(sorted(updates.keys()))))
        else:
            errors += 1

    result = {'checked': checked, 'updated': updated, 'errors': errors, 'cancelled': is_cancelled(),
              'aborted': None}
    log.info('full_sync_check: complete -- {0}'.format(result))
    return result
