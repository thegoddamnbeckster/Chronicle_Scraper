# -*- coding: utf-8 -*-
"""Cross-process "is an NFO rebuild pass currently running" signal.

Why this needs to exist: get_details()/get_episode_details() (python/scraper.py,
python/tvshow_scraper.py) are invoked identically by Kodi whether the item is
brand new (discovered during an ordinary library scan) or being re-scraped on
purpose by nfo_rebuild.py's delete-then-refresh pass -- there is no argument or
context Kodi passes that tells the scraper which situation this is. Local NFO
writing is meant to happen ONLY as part of an explicit rebuild pass (manual
"Rebuild local NFOs" action, or the opt-in "Automatically rebuild NFOs after
every library scan" service) -- never inline during an ordinary scan, since
that only slows down getting new files into Kodi's library for a write that
adds nothing to whether the item itself shows up. This file is the one signal
that lets those two call sites tell the difference.

Same cross-process-file approach as activity_tracker.py, for the same reason:
every scraper action is its own short-lived process, so there is no in-memory
flag any of them could share directly. Unlike activity_tracker.py, a count
(not a boolean) is stored -- nfo_rebuild.run() is guarded by service.py's own
lock against a second AUTO-triggered rebuild overlapping, but the manual
"Rebuild local NFOs" menu action is a separate, unguarded entry point, so a
simple set/clear boolean could have the manual action's finish clear the flag
out from under a still-running auto-triggered one (or vice versa). A count
tolerates that: enter() increments, exit() decrements, active means > 0.

Best-effort, deliberately no locking, same tradeoff activity_tracker.py's own
docstring explains: a rare lost update only ever costs one item's NFO write
happening on the wrong pass (written a scan too early, or skipped and caught
on the next rebuild) -- never anything that corrupts data.
"""

import json

import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('rebuild_state')

ADDON = xbmcaddon.Addon()

_STATE_PATH = 'special://profile/addon_data/{0}/rebuild_active.json'.format(ADDON.getAddonInfo('id'))


def mark_started():
    """Call once, right before starting a rebuild pass (nfo_rebuild.run())."""
    try:
        data = _read() or {'count': 0}
        data['count'] = data.get('count', 0) + 1
        _write(data)
    except Exception as exc:
        log.warning("Couldn't record rebuild-active state: {0}".format(exc))


def mark_finished():
    """Call once, when a rebuild pass finishes (success, cancellation, or
    error alike -- always from a finally block so the count can't get stuck
    above zero and leave NFO writing silently enabled forever)."""
    try:
        data = _read() or {'count': 0}
        data['count'] = max(0, data.get('count', 0) - 1)
        _write(data)
    except Exception as exc:
        log.warning("Couldn't clear rebuild-active state: {0}".format(exc))


def is_active():
    """True if a rebuild pass is currently in progress somewhere -- checked
    by get_details()/get_episode_details() before writing a local NFO.
    Fails safe as False (skip writing) if the state file can't be read, since
    an ordinary scan is the overwhelmingly common case and a missed rebuild
    marker just means those items wait for the next explicit rebuild pass
    rather than silently getting a write nothing asked for."""
    data = _read()
    return bool(data and data.get('count', 0) > 0)


def _read():
    if not xbmcvfs.exists(_STATE_PATH):
        return None
    try:
        f = xbmcvfs.File(_STATE_PATH, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return None


def _write(data):
    folder = _STATE_PATH.rsplit('/', 1)[0] + '/'
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    f = xbmcvfs.File(_STATE_PATH, 'w')
    try:
        f.write(bytearray(json.dumps(data).encode('utf-8')))
    finally:
        f.close()
