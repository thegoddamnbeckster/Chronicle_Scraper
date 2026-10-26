# -*- coding: utf-8 -*-
"""What this Kodi has played lately, so watch-state reconciliation can tell a bookmark from a fresh
partial play apart from a stale one.

Kodi stores no time with a resume bookmark, and stopping a film part-way leaves its play count and
last-played date untouched. A film Chronicle already counts as watched (an earlier viewing, or another
Shield) therefore looks identical whether its bookmark is weeks-old leftover or the half-hour someone
just watched: the sync wiped both, so a rewatch's progress vanished within minutes
(Harry Potter and the Prisoner of Azkaban, 2026-10-25). The one reliable signal is the playback
itself, so the movie add-on's service records every file this Kodi starts and stops playing, and every
reconcile path asks here before it overwrites a bookmark.

Kept in the MOVIE add-on's data folder whichever add-on asks: the movie add-on's service is the one
writer, and the TV add-on's episode scrapes read the same file. Without the movie add-on there is no
file, nothing counts as freshly played, and reconciliation behaves exactly as it did before.
"""

import json
from datetime import datetime, timedelta
from urllib.parse import unquote

import xbmc
import xbmcvfs

from lib import timeutil
from lib.logger import Logger

log = Logger('recent_play')

_PATH = 'special://profile/addon_data/script.chronicle.scraper.movie/recent_plays.json'
_FORMAT = '%Y-%m-%d %H:%M:%S'
_KEEP = timedelta(days=120)
_MAX_ENTRIES = 3000
_SAME_PLAY_MARGIN = timedelta(minutes=5)


def norm(path):
    """A file path in one comparable spelling: Kodi hands the same file out percent-encoded in some places
    and not in others, and with differing case or slashes from different sources."""
    return unquote(path or '').replace('\\', '/').strip().lower()


def load():
    """{normalised path: 'YYYY-MM-DD HH:MM:SS' local time of its last start/stop}, or {} when missing or unreadable."""
    if not xbmcvfs.exists(_PATH):
        return {}
    try:
        f = xbmcvfs.File(_PATH, 'r')
        try:
            raw = bytes(f.readBytes()).decode('utf-8')
        finally:
            f.close()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Couldn't read {0}, treating nothing as recently played: {1}".format(_PATH, exc))
        return {}


def _save(plays):
    try:
        folder = _PATH.rsplit('/', 1)[0] + '/'
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
        f = xbmcvfs.File(_PATH, 'w')
        try:
            f.write(bytearray(json.dumps(plays).encode('utf-8')))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't save {0}: {1}".format(_PATH, exc))


def record(path, now=None):
    """Notes that this file was playing at `now` (default: right now, local time). Best-effort."""
    key = norm(path)
    if not key:
        return
    now = now or datetime.now()
    plays = load()
    plays[key] = now.strftime(_FORMAT)
    cutoff = now - _KEEP
    plays = {k: v for k, v in plays.items() if (timeutil.parse_naive(v) or now) >= cutoff}
    if len(plays) > _MAX_ENTRIES:
        newest = sorted(plays.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_ENTRIES]
        plays = dict(newest)
    _save(plays)


def last_played(path, plays=None):
    """Naive local datetime this file was last started/stopped on this Kodi, or None. Pass `plays` (from load())
    when checking many files, so the file is read once."""
    value = (load() if plays is None else plays).get(norm(path))
    return timeutil.parse_naive(value) if value else None


def is_playing(path):
    """True when Kodi's video player is playing exactly this file right now."""
    if not path:
        return False
    try:
        player = xbmc.Player()
        return bool(player.isPlayingVideo()) and norm(player.getPlayingFile()) == norm(path)
    except Exception:
        return False


def chronicle_stamps(details):
    """The times in a Chronicle details payload that mean "Chronicle already knows about progress up to here": when it
    last saw the item finished (only while it is still counted watched -- after a reset that time is history) and when
    its resume position was last updated."""
    return [details.get('lastWatchedAt') if details.get('isWatched') else None, details.get('resumeUpdatedAt')]


def fresh_bookmark(kodi_item, stamps, plays=None):
    """(percent, played_at_iso) when Kodi holds a resume bookmark that this device itself made AFTER Chronicle's own
    latest word on the item -- genuine new progress that must not be overwritten -- otherwise None.

    A bookmark with no recorded play, or one recorded before Chronicle's latest finish/resume time (a play that has
    since been finished or moved on elsewhere), is a leftover and takes the ordinary path. `stamps` is
    chronicle_stamps(details). Chronicle's resume time for THIS play, sent by a scrobbler a moment after it stopped,
    lands seconds after the recorded stop, so a recorded play within _SAME_PLAY_MARGIN of Chronicle's time still counts
    as the same, fresh one."""
    if not kodi_item:
        return None
    resume = kodi_item.get('resume') or {}
    position = resume.get('position') or 0
    total = resume.get('total') or 0
    if position <= 0 or total <= 0:
        return None
    played = last_played(kodi_item.get('file'), plays)
    if played is None:
        return None
    known = [t for t in (timeutil.chronicle_iso_to_local(s) for s in stamps if s) if t is not None]
    if known and played < max(known) - _SAME_PLAY_MARGIN:
        return None
    return position * 100.0 / total, timeutil.kodi_local_to_iso(played.strftime(_FORMAT))


def protects_progress(kodi_item, details, push_resume):
    """For the reconcile that rides along with a scrape: True when it must leave this item's watched/resume state
    alone -- the file is playing this moment, or this device has made progress on it since Chronicle last heard.
    In the second case push_resume(percent, played_at_iso) is called to tell Chronicle the real position."""
    if not kodi_item:
        return False
    if is_playing(kodi_item.get('file')):
        log.info('"{0}" is playing right now, leaving its watched/resume state alone'.format(details.get('title')))
        return True
    fresh = fresh_bookmark(kodi_item, chronicle_stamps(details))
    if fresh is None:
        return False
    log.info('"{0}" has progress made here since Chronicle last heard ({1:.1f}%), keeping it'.format(
             details.get('title'), fresh[0]))
    push_resume(*fresh)
    return True


class Recorder(xbmc.Player):
    """Keep one alive for the life of the service: records each file as playback starts and again as it stops."""

    def __init__(self):
        super(Recorder, self).__init__()
        self._file = None
        # The service restarts on every install or upgrade, possibly mid-film: pick that film up too, or its stop
        # would record nothing and its bookmark would look like a leftover.
        try:
            if self.isPlayingVideo():
                self._note_started()
        except Exception:
            pass

    def _note_started(self):
        try:
            self._file = self.getPlayingFile()
        except Exception:
            self._file = None
        if self._file:
            record(self._file)

    def _note_stopped(self):
        if self._file:
            record(self._file)
        self._file = None

    def onAVStarted(self):
        self._note_started()

    def onPlayBackStopped(self):
        self._note_stopped()

    def onPlayBackEnded(self):
        self._note_stopped()

    def onPlayBackError(self):
        self._note_stopped()
