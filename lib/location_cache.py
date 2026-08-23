# -*- coding: utf-8 -*-
"""Short-lived cross-process cache bridging find() and getdetails() for the
SAME movie, so the expensive video-source browse in movie_art_sync.py's
_search_sources_for_movie() only ever has to run once per file, not twice.

Why this is needed: search_for_movie() (Kodi's find action) already runs
the full find_movie_location() lookup once, purely to confirm whether this
exact file already belongs to another item before Chronicle considers
creating a new one (see search_for_movie()'s own comment -- this is what
stops fan edits and mismatched-title items from spawning duplicates).
get_details() (getdetails) then runs the SAME lookup again moments later,
for the unrelated purpose of placing local art. For a movie being scraped
for the very first time, it is NOT YET in Kodi's VideoLibrary at either
point -- that only happens once the whole find+getdetails scrape completes
-- so the cheap VideoLibrary paths both calls try first fail identically
both times, and both calls fall all the way through to the same slow
multi-source xbmcvfs.listdir() walk for the exact same file. This cache
lets the second call reuse the first call's already-proven answer instead
of re-discovering it from scratch, which is exactly the "why is this one
movie so much slower than the rest" case for any brand-new addition.

find and getdetails are separate short-lived processes -- Kodi launches a
fresh Python interpreter per scraper action, same reason activity_tracker.py
is a file rather than an in-memory flag (see that module's own docstring)
-- so this has to be a file too. Deliberately no locking, same reasoning as
activity_tracker.py: a lost or racing update just means the second call
falls back to the full walk it would have done anyway, never a correctness
problem.

Entries expire quickly (seconds, not minutes) -- this only ever needs to
bridge the find-to-getdetails gap for ONE scrape, not act as a long-lived
library-wide cache. A short TTL keeps the file from growing unbounded over
a long scan and avoids ever serving a stale answer for a file that's since
moved.

The TTL is also the only defense against a real cross-contamination risk:
the cache key is (normalized title, year) -- the only identity Kodi's find()
step has available, since it runs BEFORE Chronicle has resolved (or created)
an item id there's nothing stronger to key on yet. Two DIFFERENT movies that
happen to share both an exact title and year (not hypothetical -- duplicate-
titled direct-to-video/indie releases in the same year are a known real
category) would collide on this same key if both were mid-scrape at once,
and recall() would hand movie B the folder _search_sources_for_movie()
actually discovered for movie A, causing sync_movie_art/sync_movie_nfo to
write into the wrong movie's directory. A real find()-to-getdetails() gap
for one scrape is observed to be low single-digit seconds (confirmed via
live kodi.log timestamps); keeping the TTL an order of magnitude above that,
rather than minutes, keeps the actual collision window -- two same-titled,
same-year, brand-new-to-the-library movies both scraped within the same
handful of seconds -- as close to the true bridging need as possible without
still defeating the optimization on a slow filesystem.
"""

import re
import time

import xbmcaddon

from lib import json_cache_file
from lib.logger import Logger

log = Logger('location_cache')

ADDON = xbmcaddon.Addon()

_CACHE_PATH = 'special://profile/addon_data/{0}/location_cache.json'.format(ADDON.getAddonInfo('id'))
_TTL_SECONDS = 30

# Deliberately a private copy of movie_art_sync.normalize()'s regex rather
# than importing it -- movie_art_sync imports this module, so importing back
# the other way would be circular. Just a cache-key normalizer, nothing that
# needs to stay byte-for-byte identical to the matching logic itself.
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')


def _key(title, year):
    normalized = _NON_ALNUM_RE.sub('', (title or '').lower())
    return '{0}|{1}'.format(normalized, year or '')


def remember(title, year, folder, video_basename, full_filename):
    """Records a location just discovered the slow way, keyed by title+year,
    for getdetails()'s own imminent lookup of the same movie to reuse.
    Best-effort -- a failure here just means the next call falls back to the
    full walk, same as if this cache didn't exist at all. Also prunes any
    expired entries while it's here, so the file doesn't grow across a long
    scan touching thousands of movies."""
    try:
        data = json_cache_file.read(_CACHE_PATH)
        now = time.time()
        data = {k: v for k, v in data.items() if now - v.get('t', 0) < _TTL_SECONDS}
        data[_key(title, year)] = {
            't': now, 'folder': folder, 'basename': video_basename, 'filename': full_filename,
        }
        json_cache_file.write(_CACHE_PATH, data)
    except Exception as exc:
        log.warning("Couldn't cache location for {0!r} ({1}): {2}".format(title, year, exc))


def recall(title, year):
    """Returns (folder, video_basename, full_filename) if a fresh entry
    exists for this title+year, else None."""
    try:
        entry = json_cache_file.read(_CACHE_PATH).get(_key(title, year))
    except Exception:
        return None
    if not entry or (time.time() - entry.get('t', 0)) >= _TTL_SECONDS:
        return None
    return entry.get('folder'), entry.get('basename'), entry.get('filename')
