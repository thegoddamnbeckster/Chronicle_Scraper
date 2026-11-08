# -*- coding: utf-8 -*-
"""Re-reads every title in Kodi's library from Chronicle, one at a time, so a fix to what the scrapers hand Kodi (actor photos, cast, art)
reaches the titles Kodi already holds. A library scan only looks at new or changed folders, and Kodi has no "refresh everything", only
"refresh this one".

Kodi's own VideoLibrary.Refresh* returns at once and queues the work, so asking for two thousand in a row would pile them all up behind
each other. This asks for one, waits for the scraper to pick it up and go quiet (the scraper marks its activity in
lib/activity_tracker.py), then asks for the next. It waits out a library scan first and goes slowly while a video plays, so it never
competes with either. It takes its turn with every other background sync (lib/sync_baton.py), so the movie and TV refreshes run one after
the other rather than at once (they share the one "scraper is active" signal, so two at once would read each other's activity as their
own). It is safe to stop and run again: nothing is lost by refreshing a title twice.
"""

import json
import time

import xbmc

from lib import activity_tracker
from lib import playback_pacing
from lib import sync_baton
from lib.logger import Logger

log = Logger('bulk_refresh')

# kind -> (list method, list result key, id field, refresh method, extra refresh params)
KINDS = {
    'movie':  ('VideoLibrary.GetMovies',  'movies',  'movieid',  'VideoLibrary.RefreshMovie',  {}),
    'tvshow': ('VideoLibrary.GetTVShows', 'tvshows', 'tvshowid', 'VideoLibrary.RefreshTVShow', {'refreshepisodes': True}),
}

# How long to wait for the scraper to start on a title Kodi was asked to refresh. A title that never reaches the scraper (a folder
# Chronicle does not know, a title whose scraper is another add-on) is counted as skipped, not waited on forever.
START_WAIT_SECONDS = 20
# Quiet time after the scraper's last call that means "this title is done".
QUIET_SECONDS = 6
# Longest one title may take before the next is asked for regardless.
MAX_SECONDS_PER_TITLE = 180
POLL_SECONDS = 1.0
# How long a refresh waits for its turn behind another background sync (the other kind's refresh can run for hours).
TURN_WAIT_SECONDS = 24 * 60 * 60

DEFAULT_MESSAGES = {
    'scan': 'Waiting for the library scan to finish...',
    'turn': 'Waiting for another Chronicle sync to finish...',
}


def _rpc(method, params=None):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params:
        request['params'] = params
    return json.loads(xbmc.executeJSONRPC(json.dumps(request)))


def list_titles(kind):
    """[(kodi id, label)] for every title of this kind, or None when Kodi cannot be asked."""
    list_method, key, id_field, _refresh, _extra = KINDS[kind]
    try:
        response = _rpc(list_method, {'properties': ['year']})
    except Exception as exc:  # noqa: BLE001
        log.warning('bulk_refresh: could not list {0}: {1}'.format(key, exc))
        return None
    if 'error' in response:
        log.warning('bulk_refresh: listing {0} rejected: {1}'.format(key, response['error']))
        return None
    rows = (response.get('result') or {}).get(key) or []
    return [(row[id_field], '{0}{1}'.format(row.get('label') or row.get('title') or row[id_field],
                                            ' ({0})'.format(row['year']) if row.get('year') else ''))
            for row in rows if id_field in row]


def _last_activity():
    try:
        return float((activity_tracker.read_activity() or {}).get('timestamp') or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def run(kind, is_cancelled=None, progress_callback=None, sleep=None, clock=time.time, messages=None):
    """Refreshes every title of `kind` ('movie' or 'tvshow'). Returns {'total', 'refreshed', 'skipped', 'errors', 'cancelled'}.
    progress_callback(index, total, label) is told about each title as it starts, and about each wait (a scan, another sync's turn)
    with the matching entry of `messages` as the label."""
    is_cancelled = is_cancelled or (lambda: False)
    monitor = xbmc.Monitor()
    sleep = sleep or (lambda seconds: monitor.waitForAbort(seconds))
    say = dict(DEFAULT_MESSAGES, **(messages or {}))
    result = {'total': 0, 'refreshed': 0, 'skipped': 0, 'errors': 0, 'cancelled': False}

    titles = list_titles(kind)
    if titles is None:
        result['errors'] = 1
        return result
    total = len(titles)
    result['total'] = total
    _list, _key, id_field, refresh_method, extra = KINDS[kind]
    log.info('bulk_refresh: {0} -- {1} title(s) to refresh'.format(kind, total))

    def stopped():
        return is_cancelled() or monitor.abortRequested()

    def tell(index, label):
        if progress_callback:
            progress_callback(index, total, label)

    owner = sync_baton.owner_id('bulk-refresh-' + kind)
    tell(0, say['turn'])
    with sync_baton.hold(owner, 'the {0} refresh'.format(kind), log, is_aborted=stopped, wait_seconds=TURN_WAIT_SECONDS,
                         clock=clock, sleep=sleep) as got:
        if not got:
            result['cancelled'] = True
            return result
        for index, (kodi_id, label) in enumerate(titles, 1):
            sync_baton.renew(owner, 'the {0} refresh'.format(kind), clock=clock)   # a refresh outlasts the baton's expiry

            # Never alongside a library scan, and slowly while something is playing.
            announced = False
            while xbmc.getCondVisibility('Library.IsScanning'):
                if not announced:
                    announced = True
                    log.info('bulk_refresh: {0} -- waiting for the library scan to finish'.format(kind))
                    tell(index - 1, say['scan'])
                if stopped():
                    break
                sleep(5)
            if stopped():
                result['cancelled'] = True
                break
            playback_pacing.yield_to_playback(wait=sleep)
            tell(index, label)

            before = _last_activity()
            params = dict(extra, **{id_field: kodi_id, 'ignorenfo': True})
            try:
                response = _rpc(refresh_method, params)
            except Exception as exc:  # noqa: BLE001
                log.warning('bulk_refresh: {0}({1}) failed: {2}'.format(refresh_method, kodi_id, exc))
                result['errors'] += 1
                continue
            if 'error' in response:
                log.warning('bulk_refresh: {0}({1}) rejected: {2}'.format(refresh_method, kodi_id, response['error']))
                result['errors'] += 1
                continue

            # Wait for the scraper to start on it, then for it to go quiet.
            started = clock()
            picked_up = False
            while not stopped() and clock() - started < MAX_SECONDS_PER_TITLE:
                last = _last_activity()
                if last > before:
                    picked_up = True
                    if clock() - last >= QUIET_SECONDS:
                        break
                elif clock() - started >= START_WAIT_SECONDS:
                    break
                sleep(POLL_SECONDS)
            result['refreshed' if picked_up else 'skipped'] += 1
            if index % 100 == 0:
                log.info('bulk_refresh: {0} -- {1} of {2} done'.format(kind, index, total))
            if stopped():
                result['cancelled'] = True
                break

    log.info('bulk_refresh: {0} -- {1}'.format(kind, result))
    return result


# ── the Settings button ──────────────────────────────────────────────────────────

_WINDOW_ID = 10000
_PROPERTY = 'chronicle.bulkrefresh.'
_NOTIFY_MILLISECONDS = 10000


def run_from_settings(addon, kind, text):
    """What the "Refresh all" button does. `text` maps 'title', 'confirm', 'stop', 'done', 'stopped', 'waiting_scan' and 'waiting_turn' to
    localized string ids. A second press while one is running offers to stop it (the state lives on Kodi's home window, shared by every
    interpreter)."""
    import xbmcgui

    def say(key, *args):
        value = addon.getLocalizedString(text[key])
        return value.format(*args) if args else value

    window = xbmcgui.Window(_WINDOW_ID)
    prop = _PROPERTY + kind
    dialog = xbmcgui.Dialog()
    if window.getProperty(prop) == 'running':
        if dialog.yesno(say('title'), say('stop')):
            window.setProperty(prop, 'stop')
        return None
    if not dialog.yesno(say('title'), say('confirm')):
        return None

    window.setProperty(prop, 'running')
    progress = xbmcgui.DialogProgressBG()
    progress.create(say('title'))

    def on_progress(index, total, label):
        progress.update(min(100, int(index * 100 / total)) if total else 0, message=label)

    try:
        result = run(kind, is_cancelled=lambda: window.getProperty(prop) == 'stop', progress_callback=on_progress,
                     messages={'scan': say('waiting_scan'), 'turn': say('waiting_turn')})
    finally:
        window.setProperty(prop, '')
        progress.close()

    if result['cancelled']:
        message = say('stopped', result['refreshed'] + result['skipped'], result['total'])
    else:
        message = say('done', result['refreshed'], result['total'], result['skipped'], result['errors'])
    dialog.notification(say('title'), message,
                        xbmcgui.NOTIFICATION_WARNING if result['errors'] else xbmcgui.NOTIFICATION_INFO, _NOTIFY_MILLISECONDS)
    return result
