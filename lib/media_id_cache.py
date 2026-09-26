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
import re

import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('media_id_cache')
_ADDON = xbmcaddon.Addon()
_CACHE_PATH = 'special://profile/addon_data/{0}/media_id_cache.json'.format(_ADDON.getAddonInfo('id'))


_STOPWORDS = {'the', 'a', 'an', 'and', 'of', 'us', 'uk', 'edition', 'cut', 'version'}
_ROMAN = {'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x'}
_YEAR_TOKEN = re.compile(r'(?:19|20)\d{2}')


def _title_tokens(title):
    t = re.sub(r'\(\s*(?:19|20)\d{2}\s*\)', ' ', (title or '').lower())
    t = t.replace("'", '')
    return [w for w in re.split(r'[^a-z0-9]+', t) if w]


def titles_agree(kodi_title, chronicle_title):
    """Whether a Kodi item's title and the title of the Chronicle item it was matched to are plausibly
    the same work -- the check a cached id must pass before anything is written for it.

    This cache is keyed by Kodi's OWN ids, which Kodi reassigns whenever a library is rebuilt or a
    show removed and re-added. Confirmed live (2026-09-26): 32 movies and 6 shows on one device were
    mapped to a different item entirely ("Scream" -> "Evil Bong 2", "A Knight of the Seven Kingdoms"
    -> "Star Trek: Discovery"), so ratings and watched marks were being written onto the wrong
    items -- and once episode text started syncing, the wrong show's titles and plots too.

    Tolerant of legitimate naming differences ("Anne Rice's Mayfair Witches" vs "Mayfair Witches",
    "... (US)", "... - Defrosted Edition"): the shorter title's words must appear in the longer one.
    Strict about sequel numbers ("Home Alone" is not "Home Alone 2"). With no title on either side
    there is nothing to judge, so it agrees.
    """
    a = [w for w in _title_tokens(kodi_title) if w not in _STOPWORDS]
    b = [w for w in _title_tokens(chronicle_title) if w not in _STOPWORDS]
    if not a or not b:
        return True

    def numbers(tokens):
        return {w for w in tokens if (w.isdigit() and not _YEAR_TOKEN.fullmatch(w)) or w in _ROMAN}

    if numbers(a) != numbers(b):
        return False
    sa, sb = set(a), set(b)
    return len(sa & sb) / float(min(len(sa), len(sb))) >= 0.6


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
