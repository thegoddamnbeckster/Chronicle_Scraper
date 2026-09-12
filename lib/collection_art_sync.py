# -*- coding: utf-8 -*-
"""Periodic pass that refreshes EVERY collection's art in Kodi's "Movie set information
folder" -- not just the ones that happen to get touched as a side effect of some member movie
being rescraped (collection_sync.sync_collection_art() is already called inline from
get_details() for exactly that member-triggered case; this module is the other half).

Why this exists as its own pass: a collection whose every member movie is already fully scraped
never gets its OWN art reconsidered again once that's true, even after the collection's art
changes in Chronicle (a corrected poster, a newly-resolved logo) -- nothing about editing a
collection in Chronicle touches any of its member movies, so nothing would ever trigger a
rescrape that would pick the change up. Same reasoning as watch_rating_sync.py's own "why a
separate pass" doc, applied to collection art instead of ratings/watched status.

Per-user decision (2026-09-12): movie collections belong entirely to the movie scraper now --
this replaces Chronicle_Scrobbler's retired sync_engine.py, which pushed collection art via
VideoLibrary.SetMovieSetDetails directly. Deliberately reuses collection_sync.sync_collection_art()
rather than re-implementing a second, JSON-RPC-only push: that function already handles the real
mechanics Kodi actually needs (writing into the dedicated set folder, stale-art repair, retry-
on-unreachable-share, texture invalidation) that a bare SetMovieSetDetails call skips entirely.
"""

import xbmc

from lib import collection_sync
from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('collection_art_sync')

_MIN_ITEM_SPACING_SECONDS = 0.2


def run(is_cancelled=None, progress_callback=None):
    """Runs one full pass over every collection Chronicle knows about. progress_callback(index,
    total, label), if given, is called once per collection. is_cancelled(), if given, is checked
    between collections and stops the run early -- safe to interrupt at any point, since each
    collection's own art sync is a single, self-contained unit with no cross-collection state.

    Returns a dict: {'collections': N, 'errors': N, 'cancelled': bool} -- N is "collections
    visited", not "collections changed" (sync_collection_art is fill-or-refresh and logs its own
    per-file detail; most passes touch nothing since art rarely changes between runs).
    """
    collections = ChronicleClient().get_all_collections()
    total = len(collections)
    log.info('collection_art_sync: starting -- {0} collection(s) in Chronicle'.format(total))

    counts = {'collections': 0, 'errors': 0}
    cancelled = False

    for index, collection in enumerate(collections):
        if is_cancelled is not None and is_cancelled():
            cancelled = True
            break
        label = collection.get('name') or '?'
        if progress_callback is not None:
            progress_callback(index, total, label)
        try:
            collection_sync.sync_collection_art(collection)
            counts['collections'] += 1
        except Exception as exc:
            counts['errors'] += 1
            log.warning('collection_art_sync: "{0}" failed: {1}'.format(label, exc))
        xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))

    log.info('collection_art_sync: {0} -- {1}/{2} collection(s) processed, {3} error(s)'.format(
             'cancelled' if cancelled else 'complete', counts['collections'], total, counts['errors']))
    counts['cancelled'] = cancelled
    return counts
