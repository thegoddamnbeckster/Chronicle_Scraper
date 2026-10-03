# -*- coding: utf-8 -*-
"""Once-a-minute "where is the scan" line in kodi.log while Kodi scans the video library.

Added (2026-10-02) because a scan that ends with most of the library missing was impossible to
follow: Kodi names the folder it is on only at DEBUG level, and this addon only ever logged
per-scraped-movie, so a scan that quietly skips folders looked identical to one that is stuck.

What it reports, per tick:
  * folders examined out of the total number of top-level folders across every configured video
    source, overall and per source ("10.0.0.162/Video3/Movies 62/123")
  * the full path of the folder the scanner most recently named
  * the library's movie/show/episode counts and how many were added since the scan began
  * how many scraper calls this addon has handled and how long ago the last one was

"Examined" means a folder the scanner logged a decision for ("Scanning dir", "Skipping dir",
"Rescanning dir" -- the Skipping ones are folders Kodi decided were unchanged). Those lines exist
only while Kodi's debug logging is on; without them the folder figures read "unknown" and the
library/scraper figures are still reported.

Cost: one tick a minute, on a background thread, reading only the bytes Kodi appended to its log
since the last tick (plus a one-time look-back when the service starts mid-scan), and reusing the
movie_art_sync source-listing cache for the totals.
"""

import json
import re
import threading
import time

import xbmc
import xbmcvfs

from lib import activity_tracker
from lib import movie_art_sync
from lib.logger import Logger

log = Logger('scan_progress')

_LOG_PATH = 'special://logpath/kodi.log'

# When the service starts while a scan is already running, how far back in the log to look for the
# folders already examined.
_BACKFILL_BYTES = 4 * 1024 * 1024
# Upper bound on what one tick reads, and the size of each read, so a very chatty log can never make
# a tick expensive.
_MAX_READ_PER_TICK = 8 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024

# "VideoInfoScanner: Scanning dir 'smb://.../Folder/' as not in the database"
# "VideoInfoScanner: Skipping dir 'smb://.../Folder/' due to no change (fasthash)"
_DIR_LINE = re.compile(r"VideoInfoScanner: (?:Scanning|Skipping|Rescanning) dir '([^']+)'")

_LIBRARY_METHODS = (('movies', 'VideoLibrary.GetMovies'),
                    ('shows', 'VideoLibrary.GetTVShows'),
                    ('episodes', 'VideoLibrary.GetEpisodes'))


def parse_scanned_paths(text):
    """Every directory path the scanner logged a decision for in this text, in order."""
    return _DIR_LINE.findall(text)


def locate(path, roots):
    """(root, top_level_folder_name) for a scanned path, or (None, None) when it is not inside any
    configured source, or is the source root itself. Longest matching root wins, so a source nested
    inside another is attributed correctly."""
    lowered = path.lower()
    best = None
    for root in roots:
        if lowered.startswith(root.lower()) and (best is None or len(root) > len(best)):
            best = root
    if best is None:
        return None, None
    top = path[len(best):].split('/')[0]
    return (best, top) if top else (None, None)


def display_name(root):
    """'smb://10.0.0.162/Video3/Movies/' -> '10.0.0.162/Video3/Movies'."""
    return root.split('://', 1)[-1].rstrip('/')


def _elapsed(seconds):
    seconds = int(seconds)
    return '{0}:{1:02d}:{2:02d}'.format(seconds // 3600, seconds % 3600 // 60, seconds % 60)


class ScanProgress:

    def __init__(self):
        self._lock = threading.Lock()
        self._reset(backfill=False)

    def _reset(self, backfill):
        self.started_at = time.time()
        self.backfill = backfill
        self.offset = None        # byte offset into kodi.log already consumed
        self.seen = {}            # root -> set of top-level folder names examined
        self.last_path = None
        self.lines_seen = 0
        self.baseline = None      # library counts at the first tick of this scan

    # ── public ────────────────────────────────────────────────────────────────

    def begin(self, backfill=False):
        """A scan has just started (or was already running when the service came up -- pass
        backfill=True then, so folders examined before the service started are still counted)."""
        with self._lock:
            self._reset(backfill)

    def tick(self, final=False):
        """Logs one progress line (two, with the per-source breakdown). Never raises; skipped
        entirely if the previous tick is still running (a cold listing can take a while)."""
        if not self._lock.acquire(False):
            return
        try:
            self._tick(final)
        except Exception as exc:
            log.warning('scan progress failed: {0}'.format(exc))
        finally:
            self._lock.release()

    # ── internals ─────────────────────────────────────────────────────────────

    def _tick(self, final):
        roots = [r.rstrip('/') + '/' for r in movie_art_sync.get_video_sources()]
        self._consume_log(roots)

        totals = {root: len(movie_art_sync.list_source_dirs_cached(root)) for root in roots}
        counts = _library_counts()
        if self.baseline is None:
            self.baseline = counts

        examined = sum(len(names) for names in self.seen.values())
        total = sum(totals.values())
        if self.lines_seen:
            folders = 'examined {0} of {1} top-level folders'.format(examined, total)
        else:
            folders = ('examined: unknown of {0} top-level folders -- Kodi names the folder it is '
                       'on only with debug logging on'.format(total))

        log.info('scan {0} ({1}): {2} | at: {3} | library: {4} | scraper: {5}'.format(
            'finished' if final else 'progress', _elapsed(time.time() - self.started_at), folders,
            self.last_path or '(not known yet)', self._library_text(counts), _scraper_text()))

        per_source = []
        for root in roots:
            done = len(self.seen.get(root, ()))
            if totals[root] or done:
                per_source.append('{0} {1}/{2}'.format(display_name(root), done, totals[root]))
        if per_source and self.lines_seen:
            log.info('scan progress by source: ' + ' | '.join(per_source))

    def _library_text(self, counts):
        parts = []
        for key, label in (('movies', 'movies'), ('shows', 'shows'), ('episodes', 'episodes')):
            now, was = counts.get(key), (self.baseline or {}).get(key)
            if now is None:
                parts.append('{0} ?'.format(label))
            elif was is None:
                parts.append('{0} {1}'.format(now, label))
            else:
                parts.append('{0} {1} ({2:+d})'.format(now, label, now - was))
        return ', '.join(parts)

    def _consume_log(self, roots):
        """Reads whatever Kodi appended to its log since the last tick and records the folders the
        scanner named."""
        f = xbmcvfs.File(_LOG_PATH, 'r')
        try:
            size = f.size()
            if self.offset is None:
                self.offset = max(0, size - _BACKFILL_BYTES) if self.backfill else size
            elif self.offset > size:
                self.offset = 0   # the log was restarted (Kodi restarted): read the new one from the top
            budget = _MAX_READ_PER_TICK
            f.seek(self.offset, 0)
            carry = b''
            while self.offset < size and budget > 0:
                chunk = bytes(f.readBytes(min(_CHUNK_BYTES, size - self.offset)))
                if not chunk:
                    break
                self.offset += len(chunk)
                budget -= len(chunk)
                data = carry + chunk
                cut = data.rfind(b'\n')
                if cut < 0:
                    carry = data
                    continue
                carry = data[cut + 1:]
                self._record(data[:cut + 1].decode('utf-8', 'replace'), roots)
            # An unfinished last line is re-read next tick instead of being dropped.
            self.offset -= len(carry)
        finally:
            f.close()

    def _record(self, text, roots):
        for path in parse_scanned_paths(text):
            self.lines_seen += 1
            self.last_path = path
            root, top = locate(path, roots)
            if root is not None:
                self.seen.setdefault(root, set()).add(top)


def _library_counts():
    counts = {}
    for key, method in _LIBRARY_METHODS:
        request = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {'limits': {'start': 0, 'end': 1}}}
        try:
            response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
            counts[key] = response['result']['limits']['total']
        except Exception:
            counts[key] = None
    return counts


def _scraper_text():
    activity = activity_tracker.read_activity()
    if not activity:
        return 'no calls yet'
    ago = time.time() - activity.get('timestamp', 0)
    label = activity.get('last_label')
    return '{0} calls, last {1:.0f}s ago{2}'.format(
        activity.get('count', 0), ago, ' ({0})'.format(label) if label else '')


_PROGRESS = ScanProgress()
begin = _PROGRESS.begin
tick = _PROGRESS.tick
