# -*- coding: utf-8 -*-
"""Cross-process "is the scraper doing anything right now" signal, so the
one genuinely long-lived Chronicle_Scraper process (service.py, running
continuously from Kodi startup) can show a corner status indicator for as
long as scraper activity is ongoing -- matching Kodi's own "Scanning
library" indicator, which only ever covers Kodi's own directory-walk phase,
not the (often much longer) tail of per-item scraper calls that follows it.

Why this needs to be a file, not just an in-memory flag: every scraper
action (find/getdetails/getepisodedetails/getartwork) is its own short-lived
process -- Kodi's own C++ core launches a fresh Python interpreter per call,
per the addon's own established contract (see movie_art_sync.py's module
docstring on the same point). There is no single long-lived object any of
those calls could update directly; this file is the only channel between
them and the one process (service.py) that actually could show something.

Deliberately no locking: a rare lost update or a read racing a write (caught
by _read()'s own broad except and treated as "nothing recorded yet") only
ever costs a slightly-stale corner message, never anything that matters --
adding real cross-process locking for a status indicator would be a lot of
complexity for something with no correctness requirement at all.
"""

import json
import time

import xbmcvfs

from lib.logger import Logger

log = Logger('activity_tracker')

# Deliberately NOT special://profile/addon_data/{addon_id}/... -- Chronicle
# Scraper is split into two separate addon packages
# (script.chronicle.scraper.movie, script.chronicle.scraper.tv; Kodi resolves
# a scraper invocation by addon id alone, so movies/TV can't share one addon).
# service.py (the corner-status reader) runs from the movie addon's process;
# mark_active() gets called from either addon's scraper process. special://
# temp/ is the one location both addon ids can read and write without one
# depending on the other's addon_data folder.
_ACTIVITY_PATH = 'special://temp/chronicle_scraper/activity.json'


def mark_active(label):
    """Call at the start of any scraper action Kodi invokes (find,
    getdetails, getepisodedetails, getartwork) -- records that something is
    happening right now, and a running count/last-label for the corner
    status message to show. Best-effort: a failure here never blocks the
    actual scrape, it just means the corner indicator misses this one."""
    try:
        data = _read() or {'count': 0}
        data['timestamp']  = time.time()
        data['count']      = data.get('count', 0) + 1
        data['last_label'] = label
        _write(data)
    except Exception as exc:
        log.warning("Couldn't record activity for {0!r}: {1}".format(label, exc))


def read_activity():
    """Returns the current {'timestamp', 'count', 'last_label'} dict, or
    None if nothing has ever been recorded (or the record is unreadable)."""
    return _read()


def reset():
    """Clears the activity record -- call once, after deciding activity has
    gone idle and hiding the corner indicator, so a stale old count doesn't
    make the NEXT burst of activity's count look wrong (continuing from a
    large old number instead of starting fresh)."""
    try:
        if xbmcvfs.exists(_ACTIVITY_PATH):
            xbmcvfs.delete(_ACTIVITY_PATH)
    except Exception as exc:
        log.warning("Couldn't reset activity record: {0}".format(exc))


def _read():
    if not xbmcvfs.exists(_ACTIVITY_PATH):
        return None
    try:
        f = xbmcvfs.File(_ACTIVITY_PATH, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return None


def _write(data):
    folder = _ACTIVITY_PATH.rsplit('/', 1)[0] + '/'
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    f = xbmcvfs.File(_ACTIVITY_PATH, 'w')
    try:
        f.write(bytearray(json.dumps(data).encode('utf-8')))
    finally:
        f.close()
