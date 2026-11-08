# -*- coding: utf-8 -*-
"""The one place the add-ons talk to Kodi's JSON-RPC: sending a request, and "re-scrape this library item".

Every module used to hand-build the same json.loads(xbmc.executeJSONRPC(json.dumps(request))) call, and the start-up refresh signal, the
episode/cast repair and the Refresh-all button each built the same VideoLibrary.Refresh* request. They all go through here now.
"""

import json

import xbmc

from lib.logger import Logger

log = Logger('kodi_rpc')

# kind -> (VideoLibrary refresh method, the id parameter it takes)
REFRESH = {
    'movie':   ('VideoLibrary.RefreshMovie',   'movieid'),
    'episode': ('VideoLibrary.RefreshEpisode', 'episodeid'),
    'tvshow':  ('VideoLibrary.RefreshTVShow',  'tvshowid'),
}


def send(request):
    """Sends a JSON-RPC request (a dict) to Kodi and returns the decoded answer ({'result': ...} or {'error': ...})."""
    return json.loads(xbmc.executeJSONRPC(json.dumps(request)))


def call(method, params=None):
    """send() for a method and its params."""
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params:
        request['params'] = params
    return send(request)


def refresh_item(kind, kodi_id, ignore_nfo=False, with_episodes=False, who='kodi_rpc'):
    """Asks Kodi to re-scrape one library item of `kind` ('movie', 'episode', 'tvshow'): Kodi queues it and the scrape runs through the
    scraper add-on. Returns True when Kodi accepted the request; a refusal or failure is logged (as `who`) and returns False.
    `with_episodes` also refreshes a show's episodes."""
    method, id_field = REFRESH[kind]
    params = {id_field: kodi_id}
    if ignore_nfo:
        params['ignorenfo'] = True
    if with_episodes and kind == 'tvshow':
        params['refreshepisodes'] = True
    try:
        response = call(method, params)
    except Exception as exc:  # noqa: BLE001
        log.warning('{0}: {1}({2}={3}) call failed: {4}'.format(who, method, id_field, kodi_id, exc))
        return False
    if 'error' in response:
        log.warning('{0}: {1}({2}={3}) rejected: {4}'.format(who, method, id_field, kodi_id, response['error']))
        return False
    return True
