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

Deliberately thin: all the actual "which source, which title, resolve or
create" logic lives in Chronicle itself (ScraperController). This file only
translates between Kodi's plugin-handle protocol and Chronicle's HTTP API --
it never talks to TMDB or anything else directly.
"""

import json
import sys
from urllib.parse import parse_qsl

import xbmcgui
import xbmcplugin

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient

log = Logger('scraper')


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
        return

    label = result.get('title') or title
    if result.get('year'):
        label = '{0} ({1})'.format(label, result['year'])

    listitem = xbmcgui.ListItem(label, offscreen=True)
    listitem.setInfo('video', {'title': result.get('title') or title, 'year': result.get('year')})
    if result.get('posterUrl'):
        listitem.setArt({'thumb': result['posterUrl']})

    xbmcplugin.addDirectoryItem(
        handle=handle,
        url=build_lookup_string(result['id']),
        listitem=listitem,
        isFolder=True,
    )


# Kodi's classic cast-entry shape: list of {'name','role','thumbnail','order'} dicts
# (confirmed against the real TMDB addon's own tmdb.py, not xbmc.Actor objects).
def _build_cast(names):
    return [
        {'name': name, 'role': '', 'order': i}
        for i, name in enumerate(names or [])
    ]


_ART_FIELD_MAP = (
    ('poster',    'posterUrl'),
    ('fanart',    'backdropUrl'),
    ('clearlogo', 'logoUrl'),
    ('banner',    'bannerUrl'),
    ('clearart',  'clearartUrl'),
    ('discart',   'discUrl'),
)


def get_details(media_item_id, handle):
    if media_item_id is None:
        return False

    details = ChronicleClient().get_movie_details(media_item_id)
    if not details:
        return False

    info = {
        'title':    details.get('title'),
        'plot':     details.get('overview'),
        'year':     details.get('year'),
        'genre':    details.get('genres') or [],
        'director': details.get('directors') or [],
        'duration': (details.get('runtimeMinutes') or 0) * 60,   # Kodi wants seconds
    }

    listitem = xbmcgui.ListItem(details.get('title') or '', offscreen=True)
    listitem.setInfo('video', info)
    listitem.setCast(_build_cast(details.get('cast')))

    if details.get('rating') is not None:
        listitem.setRating('chronicle', float(details['rating']), defaultt=True)

    art = {kodi_key: details[field] for kodi_key, field in _ART_FIELD_MAP if details.get(field)}
    if art:
        listitem.setArt(art)

    xbmcplugin.setResolvedUrl(handle=handle, succeeded=True, listitem=listitem)
    return True


def run():
    params = get_params(sys.argv[1:])
    enddir = True

    action = params.get('action')
    if action == 'find' and 'title' in params:
        search_for_movie(params['title'], params.get('year'), params['handle'])
    elif action == 'getdetails' and 'url' in params:
        enddir = not get_details(parse_lookup_string(params['url']), params['handle'])
    else:
        log.warning('unhandled or missing action: {0}'.format(action))

    if enddir:
        xbmcplugin.endOfDirectory(params['handle'])


if __name__ == '__main__':
    run()
