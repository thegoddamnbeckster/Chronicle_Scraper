# -*- coding: utf-8 -*-
"""Keeps the artwork of the movie sets (collections) in Kodi's library in step with Chronicle,
touching Kodi only for a set whose art actually differs.

Kodi gives movie sets no scraper hook for art beyond a poster and a fanart offered at scrape time, so
this pass sets the rest. It used to do it by downloading every image of every collection into Kodi's
"Movie set information folder" on a share, every time, and refreshing each one: slow, often unable to
reach the share, and it made Kodi's screens flicker on every pass. Now:

  * Each Kodi keeps its own copy of the time of its last successful sync (the server's asOf, in this
    add-on's profile) and asks Chronicle only for collections changed since. Chronicle keeps no list of
    devices and nothing per device; a library with nothing new answers with an empty list, and nothing
    is compared, written, or shown on screen.
  * The art is set on the set itself with VideoLibrary.SetMovieSetDetails, as the image's own URL (Kodi
    already stores sets' art that way when a scraper supplies it), so no folder or share is involved.
  * A collection is only written when Kodi's current art for it differs from Chronicle's, slot by slot,
    so a collection that merely appears in the list (one of its movies changed) costs nothing visible.

A "changed since" list cannot contain a movie set this device has not had before -- new movies in a new
collection after a scan, or a rebuilt library. This device therefore also keeps the names of the sets it
has already handled and asks for everything when Kodi has one it has not (and on the first run, which
also moves older sets off their folder files).
"""

import json

import urllib.parse

import xbmc
import xbmcaddon
import xbmcvfs

from lib.chronicle_client import ChronicleClient
from lib.kodi_art import kodi_art_url
from lib.logger import Logger

log = Logger('collection_art_sync')

_ADDON = xbmcaddon.Addon()
_STATE_PATH = 'special://profile/addon_data/{0}/collection_sync_state.json'.format(_ADDON.getAddonInfo('id'))

# (Chronicle's field on the collection, Kodi's art type for a movie set)
_ART_FIELDS = (
    ('posterUrl',   'poster'),
    ('backdropUrl', 'fanart'),
    ('bannerUrl',   'banner'),
    ('logoUrl',     'clearlogo'),
    ('clearartUrl', 'clearart'),
    ('discUrl',     'discart'),
    ('thumbUrl',    'thumb'),
)


def desired_art(collection):
    """{kodi art type: url} for every slot Chronicle has art for. A slot Chronicle has nothing for is
    absent, so whatever Kodi holds there is left alone."""
    art = {}
    for field, art_type in _ART_FIELDS:
        url = collection.get(field)
        if url:
            art[art_type] = url
    return art


def art_updates(current_art, collection):
    """The slots of a set whose art in Kodi (current_art, as Kodi reports it) is not Chronicle's."""
    return {art_type: url for art_type, url in desired_art(collection).items()
            if kodi_art_url((current_art or {}).get(art_type)) != url}


def _get_kodi_sets():
    """{set name: (setid, art)} for every movie set in Kodi, or None when Kodi could not be asked --
    which must read as "do nothing", never as "there are no sets"."""
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovieSets',
               'params': {'properties': ['art']}}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't query VideoLibrary.GetMovieSets: {0}".format(exc))
        return None
    if 'error' in response:
        log.warning('VideoLibrary.GetMovieSets rejected: {0}'.format(response['error']))
        return None
    return {s.get('label'): (s.get('setid'), s.get('art') or {})
            for s in response.get('result', {}).get('sets', []) if s.get('label')}


def _set_movie_set_art(setid, art):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetMovieSetDetails',
               'params': {'setid': setid, 'art': art}}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't update set art for setid {0}: {1}".format(setid, exc))
        return False
    if response.get('result') != 'OK':
        log.warning('SetMovieSetDetails for setid {0} returned unexpected response: {1}'.format(setid, response))
        return False
    return True


def _load_state():
    """(time of the last successful sync, names of the sets handled then) -- either None when unknown
    (first run, unreadable, or an older file that held only the names)."""
    if not xbmcvfs.exists(_STATE_PATH):
        return None, None
    try:
        f = xbmcvfs.File(_STATE_PATH, 'r')
        try:
            raw = bytes(f.readBytes()).decode('utf-8')
        finally:
            f.close()
        data = json.loads(raw) if raw else None
        if isinstance(data, list):
            return None, set(data)
        if isinstance(data, dict):
            names = data.get('sets')
            return data.get('since') or None, set(names) if isinstance(names, list) else None
        return None, None
    except Exception as exc:
        log.warning("Couldn't read {0}, treating every set as new: {1}".format(_STATE_PATH, exc))
        return None, None


def _save_state(since, names):
    """Best-effort -- failing only means the next pass asks for everything again."""
    try:
        folder = _STATE_PATH.rsplit('/', 1)[0] + '/'
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        f = xbmcvfs.File(_STATE_PATH, 'w')
        try:
            f.write(bytearray(json.dumps({'since': since, 'sets': sorted(names)}).encode('utf-8')))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't save {0}: {1}".format(_STATE_PATH, exc))


def run(is_cancelled=None, progress_callback=None, everything=False):
    """Runs one pass. progress_callback(index, total, label), if given, is called once per collection
    to compare -- so nothing is shown at all when Chronicle has none to give. is_cancelled(), if
    given, is checked between collections; stopping early is safe, since the time is only
    remembered once the whole list was applied.

    everything=True compares every collection regardless of what Chronicle says changed (the manual
    "Sync Collection Art Now").

    Returns {'collections': N compared, 'changed': N sets written, 'errors': N, 'cancelled': bool}."""
    counts = {'collections': 0, 'changed': 0, 'errors': 0, 'cancelled': False}

    kodi_sets = _get_kodi_sets()
    if kodi_sets is None:
        counts['errors'] += 1
        return counts

    since, known = _load_state()
    new_sets = set(kodi_sets) - known if known is not None else set(kodi_sets)
    ask_for_all = everything or since is None or known is None or bool(new_sets)

    client = ChronicleClient()
    listing = client.get_changed_collections(since=None if ask_for_all else since)
    if listing is None:
        counts['errors'] += 1
        return counts

    collections = listing['collections']
    total = len(collections)
    log.info('collection_art_sync: {0} collection(s) to compare ({1}), {2} set(s) in Kodi'.format(
             total, 'all' if ask_for_all else 'changed since this device last synced', len(kodi_sets)))

    for index, collection in enumerate(collections):
        if is_cancelled is not None and is_cancelled():
            counts['cancelled'] = True
            break
        label = collection.get('name') or '?'
        if progress_callback is not None:
            progress_callback(index, total, label)
        counts['collections'] += 1

        entry = kodi_sets.get(collection.get('name'))
        if entry is None:
            continue  # Chronicle has the collection, Kodi has no such set (yet) -- nothing to set art on
        setid, current_art = entry
        updates = art_updates(current_art, collection)
        if not updates:
            continue
        if _set_movie_set_art(setid, updates):
            counts['changed'] += 1
            log.info('collection_art_sync: "{0}" -- set {1}'.format(label, ', '.join(sorted(updates))))
        else:
            counts['errors'] += 1

    # Remember the time only after a pass that finished and applied everything: anything else is
    # offered again next time (what was already applied then compares equal and costs nothing).
    if not counts['cancelled'] and not counts['errors'] and listing.get('asOf'):
        _save_state(listing['asOf'], kodi_sets)

    log.info('collection_art_sync: {0} -- {1} compared, {2} changed, {3} error(s)'.format(
             'cancelled' if counts['cancelled'] else 'complete',
             counts['collections'], counts['changed'], counts['errors']))
    return counts


def describe_result(result, get_string):
    """The one-line notification for a finished pass, or None when there is nothing worth saying
    (changed nothing, no errors). String ids: 32147 "Updated {0} collection(s){1}", 32136 the errors
    suffix."""
    errors = result.get('errors') or 0
    if not result.get('changed') and not errors:
        return None
    return get_string(32147).format(
        result.get('changed') or 0, get_string(32136).format(errors) if errors else '')
