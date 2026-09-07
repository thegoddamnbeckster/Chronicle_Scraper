# -*- coding: utf-8 -*-
"""Persists this Kodi instance's own (movieid -> Chronicle MediaItemId) and
(tvshowid -> Chronicle MediaItemId) mappings across runs -- specifically so
lib/watch_rating_sync.py's periodic pass doesn't have to re-run Chronicle's
resolve-OR-CREATE search_movie()/search_show() (a sequential multi-provider
lookup, confirmed elsewhere to take up to 90s in the worst case, and capable
of creating a new item server-side if a title/year match ever drifts) for
every single item, every single pass, forever, just to re-derive an id that's
almost always already stable from the previous pass.

Kept under special://profile/addon_data/{addon_id}/, NOT special://temp/ --
unlike rebuild_state.py/legacy_nfo.py/episode_path_cache.py (which coordinate
a handoff between two Kodi-triggered scrape invocations a few seconds apart,
and are deliberately wiped on every Kodi restart), this cache is meant to
survive restarts: its whole purpose is to make the SECOND-and-later
watch_rating_sync.run() call cheap, whether that call happens 2 hours later
in the same Kodi session or after a reboot. Also, unlike those cross-addon
signals, this cache is never read by the sibling TV addon (tv_addon has no
watch_rating_sync.py of its own), so the addon-id-scoped path is fine here.

A cached id that no longer resolves (the item was deleted/merged/re-created
in Chronicle since) is self-healing, not a permanent wrong answer: callers
that get an empty/failed result back from Chronicle for a cached id are
expected to drop it (see stale-entry handling in watch_rating_sync.py) and
re-resolve via search once, refreshing the cache.
"""

import json

import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('media_id_cache')
_ADDON = xbmcaddon.Addon()
_CACHE_PATH = 'special://profile/addon_data/{0}/media_id_cache.json'.format(_ADDON.getAddonInfo('id'))


def movie_key(movieid):
    return 'movie:{0}'.format(movieid)


def tvshow_key(tvshowid):
    return 'tvshow:{0}'.format(tvshowid)


def load():
    """Returns the cache dict, or {} if it doesn't exist yet or fails to parse (a corrupt or
    missing cache is never fatal -- every entry is just re-resolved via search as if this were
    the very first run, same as an empty cache always would be)."""
    if not xbmcvfs.exists(_CACHE_PATH):
        return {}
    try:
        f = xbmcvfs.File(_CACHE_PATH, 'r')
        try:
            raw = bytes(f.readBytes()).decode('utf-8')
        finally:
            f.close()
        return json.loads(raw) if raw else {}
    except Exception as exc:
        log.warning("Couldn't read {0}, starting with an empty cache: {1}".format(_CACHE_PATH, exc))
        return {}


def save(cache):
    """Best-effort -- a failed save just means the next run re-resolves everything via search
    again (slower, not wrong), same degraded-but-safe fallback as a missing/corrupt cache."""
    try:
        folder = _CACHE_PATH.rsplit('/', 1)[0] + '/'
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        f = xbmcvfs.File(_CACHE_PATH, 'w')
        try:
            f.write(bytearray(json.dumps(cache).encode('utf-8')))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't save {0}: {1}".format(_CACHE_PATH, exc))
