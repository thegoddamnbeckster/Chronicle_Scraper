# -*- coding: utf-8 -*-
"""Re-reads every title in Kodi's library from Chronicle, one at a time, so a fix to what the scrapers hand Kodi (actor photos, cast, art)
reaches the titles Kodi already holds. A library scan only looks at new or changed folders, and Kodi has no "refresh everything", only
"refresh this one".

Kodi's own VideoLibrary.Refresh* returns at once and queues the work, so asking for two thousand in a row would pile them all up behind
each other. This asks for one, waits for the scraper to pick it up and go quiet (the scraper marks its activity in
lib/activity_tracker.py), then asks for the next. It waits out a library scan first and goes slowly while a video plays, so it never
competes with either.

It takes its turn with every other background sync (lib/sync_baton.py), but only a chunk of titles at a time and then lets go for a moment,
so a refresh that runs for hours does not hold the others off: the post-scan syncs and the TV episode check ask for the baton once per
scan and give up after an hour, so a refresh holding it throughout would lose them. The movie and TV refreshes also take turns with each
other, because they share the one "scraper is active" signal and two at once would read each other's activity as their own.

Light on Kodi: it sleeps between checks (nothing spins), reads the activity file every couple of seconds, and asks Kodi for one title at a
time. It is safe to stop and run again: nothing is lost by refreshing a title twice.
"""

import time

import xbmc

from lib import activity_tracker
from lib import kodi_rpc
from lib import playback_pacing
from lib import sync_baton
from lib.logger import Logger

log = Logger('bulk_refresh')

# kind -> (list method, list result key, id field, refresh episodes too, quiet seconds)
# Quiet seconds: how long after the scraper's last call a title counts as done. A show is refreshed with all its episodes, which the
# scraper fetches one call at a time with gaps between them, so it needs a longer quiet than a movie before it is called finished.
KINDS = {
    'movie':  ('VideoLibrary.GetMovies',  'movies',  'movieid',  False, 6),
    'tvshow': ('VideoLibrary.GetTVShows', 'tvshows', 'tvshowid', True,  20),
}

# How long to wait for the scraper to start on a title Kodi was asked to refresh. A title that never reaches the scraper (a folder
# Chronicle does not know, a title whose scraper is another add-on) is counted as skipped, not waited on forever.
START_WAIT_SECONDS = 20
# Longest one title may take before the next is asked for regardless.
MAX_SECONDS_PER_TITLE = 180
# How often the activity file is looked at while a title is being worked on.
POLL_SECONDS = 2.0
# How long a refresh waits for its turn behind another background sync.
TURN_WAIT_SECONDS = 24 * 60 * 60
# Titles done per hold of the baton, then it is let go for a moment so a waiting sync can take its turn (its poll is POLL_SECONDS).
TITLES_PER_TURN = 20
HANDOVER_SECONDS = sync_baton.POLL_SECONDS + 1

DEFAULT_MESSAGES = {
    'scan': 'Waiting for the library scan to finish...',
    'turn': 'Waiting for another Chronicle sync to finish...',
}


def list_titles(kind):
    """[(kodi id, label)] for every title of this kind, or None when Kodi cannot be asked."""
    list_method, key, id_field = KINDS[kind][:3]
    try:
        response = kodi_rpc.call(list_method, {'properties': ['year']})
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


def run(kind, is_cancelled=None, progress_callback=None, sleep=None, clock=time.time, messages=None, heartbeat=None):
    """Refreshes every title of `kind` ('movie' or 'tvshow'). Returns {'total', 'refreshed', 'skipped', 'errors', 'cancelled'} (plus
    'unreadable': True when Kodi could not list the library). progress_callback(index, total, label) is told about each title as it
    starts, and about each wait (a scan, another sync's turn) with the matching entry of `messages` as the label. heartbeat() is called
    on every pause, so a caller can show the run is alive."""
    is_cancelled = is_cancelled or (lambda: False)
    monitor = xbmc.Monitor()
    raw_sleep = sleep or (lambda seconds: monitor.waitForAbort(seconds))

    def nap(seconds):
        if heartbeat:
            heartbeat()
        return raw_sleep(seconds)

    say = dict(DEFAULT_MESSAGES, **(messages or {}))
    result = {'total': 0, 'refreshed': 0, 'skipped': 0, 'errors': 0, 'cancelled': False}

    titles = list_titles(kind)
    if titles is None:
        result['errors'] = 1
        result['unreadable'] = True
        return result
    total = len(titles)
    result['total'] = total
    with_episodes, quiet = KINDS[kind][3:]
    log.info('bulk_refresh: {0} -- {1} title(s) to refresh'.format(kind, total))

    def stopped():
        return is_cancelled() or monitor.abortRequested()

    def tell(index, label):
        if progress_callback:
            progress_callback(index, total, label)

    def refresh_one(index, kodi_id, label):
        """One title. False when the run should stop."""
        # Never alongside a library scan, and slowly while something is playing.
        announced = False
        while xbmc.getCondVisibility('Library.IsScanning'):
            if not announced:
                announced = True
                log.info('bulk_refresh: {0} -- waiting for the library scan to finish'.format(kind))
                tell(index - 1, say['scan'])
            if stopped():
                break
            nap(5)
        if stopped():
            return False
        playback_pacing.yield_to_playback(wait=nap)
        tell(index, label)

        before = _last_activity()
        if not kodi_rpc.refresh_item(kind, kodi_id, ignore_nfo=True, with_episodes=with_episodes, who='bulk_refresh'):
            result['errors'] += 1
            return True

        # Wait for the scraper to start on it, then for it to go quiet.
        started = clock()
        picked_up = False
        while not stopped() and clock() - started < MAX_SECONDS_PER_TITLE:
            last = _last_activity()
            if last > before:
                picked_up = True
                if clock() - last >= quiet:
                    break
            elif clock() - started >= START_WAIT_SECONDS:
                break
            nap(POLL_SECONDS)
        result['refreshed' if picked_up else 'skipped'] += 1
        if index % 100 == 0:
            log.info('bulk_refresh: {0} -- {1} of {2} done'.format(kind, index, total))
        return not stopped()

    owner = sync_baton.owner_id('bulk-refresh-' + kind)
    label_for_baton = 'the {0} refresh'.format(kind)
    position = 0
    while position < total and not result['cancelled']:
        tell(position, say['turn'])
        with sync_baton.hold(owner, label_for_baton, log, is_aborted=stopped, wait_seconds=TURN_WAIT_SECONDS,
                             clock=clock, sleep=nap) as got:
            if not got:
                result['cancelled'] = True
                break
            for index in range(position + 1, min(position + TITLES_PER_TURN, total) + 1):
                kodi_id, label = titles[index - 1]
                if not refresh_one(index, kodi_id, label):
                    result['cancelled'] = True
                    break
            position += TITLES_PER_TURN
        if position < total and not result['cancelled']:
            nap(HANDOVER_SECONDS)       # let a sync that has been waiting for its turn see the baton free

    log.info('bulk_refresh: {0} -- {1}'.format(kind, result))
    return result


# ── the Settings button ──────────────────────────────────────────────────────────

_WINDOW_ID = 10000
_PROPERTY = 'chronicle.bulkrefresh.'
_NOTIFY_MILLISECONDS = 10000
# A run writes "running|<time>" on every pause. A flag no run has refreshed for this long belongs to a script that was killed (an
# add-on update stops its scripts without letting them clean up), and is ignored rather than blocking the button until Kodi restarts.
STALE_SECONDS = 120


def _state(window, prop, clock=time.time):
    """'running', 'stop' or '' for a run that is alive."""
    raw = window.getProperty(prop) or ''
    state, _, stamp = raw.partition('|')
    if state not in ('running', 'stop'):
        return ''
    try:
        return state if clock() - float(stamp) < STALE_SECONDS else ''
    except ValueError:
        return ''


def run_from_settings(addon, kind, text):
    """What the "Refresh all" button does. `text` maps 'title', 'confirm', 'stop', 'done', 'stopped', 'failed', 'waiting_scan' and
    'waiting_turn' to localized string ids. A second press while one is running offers to stop it (the state lives on Kodi's home window,
    shared by every interpreter)."""
    import xbmcgui

    def say(key, *args):
        value = addon.getLocalizedString(text[key])
        return value.format(*args) if args else value

    window = xbmcgui.Window(_WINDOW_ID)
    prop = _PROPERTY + kind
    dialog = xbmcgui.Dialog()
    if _state(window, prop):
        if dialog.yesno(say('title'), say('stop')):
            window.setProperty(prop, 'stop|{0}'.format(time.time()))
        return None
    if not dialog.yesno(say('title'), say('confirm')):
        return None

    def beat():
        # Never overwrites a stop request.
        if _state(window, prop) != 'stop':
            window.setProperty(prop, 'running|{0}'.format(time.time()))

    beat()
    progress = xbmcgui.DialogProgressBG()
    progress.create(say('title'))

    def on_progress(index, total, label):
        progress.update(min(100, int(index * 100 / total)) if total else 0, message=label)

    try:
        result = run(kind, is_cancelled=lambda: _state(window, prop) == 'stop', progress_callback=on_progress,
                     messages={'scan': say('waiting_scan'), 'turn': say('waiting_turn')}, heartbeat=beat)
    finally:
        window.setProperty(prop, '')
        progress.close()

    if result.get('unreadable'):
        message, icon = say('failed'), xbmcgui.NOTIFICATION_WARNING
    elif result['cancelled']:
        message, icon = say('stopped', result['refreshed'] + result['skipped'], result['total']), xbmcgui.NOTIFICATION_INFO
    else:
        message = say('done', result['refreshed'], result['total'], result['skipped'], result['errors'])
        icon = xbmcgui.NOTIFICATION_WARNING if result['errors'] else xbmcgui.NOTIFICATION_INFO
    dialog.notification(say('title'), message, icon, _NOTIFY_MILLISECONDS)
    return result
