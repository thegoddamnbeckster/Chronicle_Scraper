# -*- coding: utf-8 -*-
"""Reads Kodi's own live settings via JSON-RPC.

Never hardcode a Kodi path/setting in this addon -- folder conventions like
the movie-sets information folder are entirely local to each install (and
each user's own NAS layout), so the only correct source of truth is Kodi's
own Settings API, queried fresh each time.
"""

import json

import xbmc

from lib.logger import Logger

log = Logger('kodi_settings')


def get_setting_value(setting_id):
    """Returns the current value of a Kodi setting (e.g. 'videolibrary.moviesetsfolder'),
    or None if it can't be read."""
    request = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'Settings.GetSettingValue',
        'params': {'setting': setting_id},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't read Kodi setting {0!r}: {1}".format(setting_id, exc))
        return None

    if 'error' in response:
        log.warning("Kodi rejected setting lookup {0!r}: {1}".format(setting_id, response['error']))
        return None

    return response.get('result', {}).get('value')
