# -*- coding: utf-8 -*-
"""Skips re-downloading local artwork that's already correct.

movie_art_sync.py's sync_movie_art() and collection_sync.py's
sync_collection_art() both deliberately overwrite whatever local art file
is already there on every single scrape -- see those modules' own
docstrings for why (Kodi re-applies a movie's local poster/fanart on its
own schedule, independent of any scraper call, so the local file has to
already agree with Chronicle's pick or Kodi will keep showing something
stale). But "always overwrite" was never paired with "unless it's already
correct" -- every getdetails() call re-downloads and rewrites every art
type Chronicle has a candidate for, even when Chronicle's pick hasn't
changed since the last time this exact file was written, which is the
overwhelming majority of scrapes on any library that isn't brand new.

This is a plain "what URL did we last actually write to this path" record,
keyed by the destination file path (not the movie/set identity) -- a
rename or move naturally orphans the old entry rather than serving a wrong
answer, since a renamed/moved item computes a different dest path and
simply gets a fresh cache miss there.

Deliberately no locking, same reasoning as activity_tracker.py and
location_cache.py: a lost or racing update just means one extra download
happens that didn't strictly need to, never an incorrect skip -- the
correctness-critical check (does the file still exist) is verified fresh
against the real filesystem every time, this cache only ever short-circuits
a download, never fabricates the existence of a file that isn't there.

Entries are pruned lazily (anything untouched for a very long time) so the
file doesn't grow forever from movies that get renamed, removed, or merged
away over the years -- a long TTL, not a short one like location_cache.py's:
this is meant to persist across ordinary rescans, not just bridge one
find-to-getdetails gap.
"""

import json
import time

import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('art_sync_cache')

ADDON = xbmcaddon.Addon()

_CACHE_PATH = 'special://profile/addon_data/{0}/art_sync_cache.json'.format(ADDON.getAddonInfo('id'))
_PRUNE_AFTER_SECONDS = 180 * 24 * 60 * 60  # ~6 months of no re-confirmation


def already_synced(dest, url):
    """True if `dest` was last written from exactly this `url` AND the file
    is still there on disk -- i.e. nothing needs to happen, so the caller
    should skip the download entirely. False means a real download+write is
    needed, whether because the file is missing, was never synced, or
    Chronicle's current pick has changed since the last sync."""
    if not url or not xbmcvfs.exists(dest):
        return False
    try:
        entry = _read().get(dest)
    except Exception:
        return False
    return bool(entry) and entry.get('url') == url


def remember(dest, url):
    """Records that `dest` now holds `url`'s content. Call only after a
    confirmed-successful write -- never speculatively, or a failed/partial
    download would get treated as done forever after."""
    try:
        now = time.time()
        data = _read()
        data = {k: v for k, v in data.items() if now - v.get('t', 0) < _PRUNE_AFTER_SECONDS}
        data[dest] = {'url': url, 't': now}
        _write(data)
    except Exception as exc:
        log.warning("Couldn't record art sync for {0}: {1}".format(dest, exc))


def _read():
    if not xbmcvfs.exists(_CACHE_PATH):
        return {}
    try:
        f = xbmcvfs.File(_CACHE_PATH, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
        return json.loads(raw.decode('utf-8')) or {}
    except Exception:
        return {}


def _write(data):
    folder = _CACHE_PATH.rsplit('/', 1)[0] + '/'
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    f = xbmcvfs.File(_CACHE_PATH, 'w')
    try:
        f.write(bytearray(json.dumps(data).encode('utf-8')))
    finally:
        f.close()
