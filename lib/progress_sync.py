# -*- coding: utf-8 -*-
"""Reconciles playback progress (resume position) between Kodi's own local library
state and Chronicle, inline as part of a normal scrape -- no separate background
sync task. Per-user request (2026-08-30): "I don't want a separate sync task in
Kodi for ratings. this needs to happen with the scraper automatically as part of
the scrape process. Kodi may not call the scraper for each video, but you will
still need to synchronize the playback progress and the ratings for each video
at some point in the scrape process."

Direction logic ported from Chronicle_Scrobbler's lib/sync_engine.py
(_resolve_progress_direction et al, the 2026-08-30 bidirectional-reconciliation
feature that mechanism replaces). Chronicle_Scraper is a fully separate Kodi
addon with no runtime dependency on Chronicle_Scrobbler, so the logic is
duplicated here rather than imported -- same as chronicle_client.py and
nfo_common.py are already duplicated between this addon and tv_addon.

Ratings are NOT reconciled bidirectionally: Kodi exposes no "when was this
rating set" signal (no lastplayed equivalent for userrating), so there's no
safe way to tell a stale local rating from a fresh one. Chronicle's rating
always wins and is pushed via InfoTagVideo.setUserRating() directly in
get_details()/get_episode_details() -- no lookup needed, so it isn't in this
module at all.
"""

import json
from datetime import datetime

import xbmc

from lib.logger import Logger

log = Logger('progress_sync')

# Matches Chronicle's own ScrobbleService.WatchedThreshold and Chronicle_Scrobbler's
# former _RESUME_SKIP_THRESHOLD_PERCENT -- a resume point this close to the end reads
# as "finished", not "in progress"; pushing it to Kodi would just leave a stray
# resume bar on something the user already completed.
_RESUME_SKIP_THRESHOLD_PERCENT = 98

_STATE_PROPERTIES = ['userrating', 'resume', 'playcount', 'lastplayed']


def lookup_movie_state(title, year):
    """One VideoLibrary.GetMovies properties lookup for resume/playcount/lastplayed.
    No folder-verification safety check here (unlike movie_art_sync.py's file
    lookup) -- we're only reading Kodi's own resume state for the exact item Kodi
    itself just asked this scraper to scrape, not writing a file that has to be
    proven correct first. Returns None for a brand-new item Kodi hasn't
    added to its library yet (mid-import) -- that's a normal, expected miss, not
    an error: resolve_progress_direction() below treats a missing kodi_item as
    "nothing to reconcile against, just push whatever Chronicle has"."""
    if not title:
        return None
    request = {
        'jsonrpc': '2.0', 'id': 1,
        'method': 'VideoLibrary.GetMovies',
        'params': {
            'filter':     {'field': 'title', 'operator': 'is', 'value': title},
            'properties': _STATE_PROPERTIES,
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning('lookup_movie_state({0!r}): failed: {1}'.format(title, exc))
        return None

    movies = response.get('result', {}).get('movies') or []
    if not movies:
        return None
    if year:
        for m in movies:
            if m.get('year') == year:
                return m
    return movies[0]


def resolve_progress_direction(chronicle_pct, chronicle_ts, kodi_item):
    """Per-user request (2026-08-30): "if Chronicle's last watched status is older
    than Kodi's last watched status, Kodi should win. Kodi's data should be added
    into chronicle. if Chronicle's dat is more current than Kodi's, Chronicle's
    dat should sync into Kodi."

    Returns ('push', chronicle_pct) to set Kodi's resume point from Chronicle's
    value, ('pull', kodi_pct) to report Kodi's value into Chronicle instead, or
    (None, None) when neither side has anything meaningful to reconcile. When
    only one side has data at all, that side always wins outright -- the
    timestamp comparison only matters once both sides have something to
    disagree about.
    """
    if kodi_item is None:
        return ('push', chronicle_pct) if chronicle_pct is not None else (None, None)

    kodi_lastplayed = kodi_item.get('lastplayed')
    kodi_playcount  = kodi_item.get('playcount') or 0
    resume          = kodi_item.get('resume') or {}
    kodi_position    = resume.get('position') or 0
    kodi_total       = resume.get('total') or 0

    if kodi_playcount > 0:
        kodi_pct = 100.0
    elif kodi_position > 0 and kodi_total > 0:
        kodi_pct = (kodi_position / kodi_total) * 100.0
    else:
        kodi_pct = None

    # A percent with no timestamp at all (Kodi has literally never played this)
    # isn't a real signal to reconcile -- lastplayed is Kodi's empty-string default.
    has_kodi = kodi_pct is not None and bool(kodi_lastplayed)
    has_chronicle = chronicle_pct is not None

    if not has_kodi and not has_chronicle:
        return None, None
    if has_kodi and not has_chronicle:
        return 'pull', kodi_pct
    if has_chronicle and not has_kodi:
        return 'push', chronicle_pct

    if _kodi_lastplayed_is_newer(kodi_lastplayed, chronicle_ts):
        return 'pull', kodi_pct
    return 'push', chronicle_pct


def resolve_watched_direction(chronicle_watched, chronicle_watched_at, kodi_item):
    """Sibling of resolve_progress_direction, for FULLY WATCHED status rather than partial
    resume position -- per-user request (2026-09-05): a movie completed on one Shield stayed
    permanently unwatched on another. resolve_progress_direction alone can never fix this:
    Chronicle clears ResumePositionPercent/ResumeUpdatedAt to null the moment an item is
    marked watched (nothing left to "resume"), so a completed item carries no signal for that
    function to compare -- it correctly returns (None, None) and nothing happens. This
    function compares Chronicle's own IsWatched/LastWatchedAt (which are NEVER cleared, see
    ScraperMovieDetailsDto's own doc) against Kodi's local playcount/lastplayed instead, using
    the identical "whichever side is more recent wins, one-sided data always wins outright"
    logic as resolve_progress_direction.

    Returns ('push', chronicle_watched_at) to mark Kodi as watched from Chronicle's side,
    ('pull', kodi_lastplayed) to report Kodi's own watched state into Chronicle instead, or
    (None, None) when neither side has anything to reconcile.
    """
    if kodi_item is None:
        return ('push', chronicle_watched_at) if chronicle_watched else (None, None)

    kodi_lastplayed = kodi_item.get('lastplayed')
    kodi_playcount  = kodi_item.get('playcount') or 0

    has_kodi = kodi_playcount > 0 and bool(kodi_lastplayed)
    has_chronicle = bool(chronicle_watched)

    if not has_kodi and not has_chronicle:
        return None, None
    if has_kodi and not has_chronicle:
        return 'pull', kodi_lastplayed
    if has_chronicle and not has_kodi:
        return 'push', chronicle_watched_at

    if _kodi_lastplayed_is_newer(kodi_lastplayed, chronicle_watched_at):
        return 'pull', kodi_lastplayed
    return 'push', chronicle_watched_at


def apply_watched_push(vtag, watched_at_iso):
    """Sets Kodi's playcount/lastplayed via InfoTagVideo -- part of the same getdetails
    response Kodi is already consuming this scrape, no extra JSON-RPC call needed. Mirrors
    apply_resume_push's own no-op guards: nothing to do without a real timestamp."""
    if not watched_at_iso:
        return
    vtag.setPlaycount(1)
    vtag.setLastPlayed(watched_at_iso.replace('T', ' ')[:19])


def apply_resume_push(vtag, resume_pct, runtime_minutes):
    """Sets Kodi's resume point via InfoTagVideo.setResumePoint() -- part of the
    SAME getdetails/getepisodedetails response Kodi is already consuming this
    scrape, no extra JSON-RPC call needed."""
    if resume_pct is None or resume_pct <= 0 or resume_pct >= _RESUME_SKIP_THRESHOLD_PERCENT:
        return
    if not runtime_minutes:
        log.warning('apply_resume_push: no runtime available -- cannot compute a resume '
                    'point in seconds, skipping')
        return
    total_seconds = runtime_minutes * 60
    vtag.setResumePoint(resume_pct / 100.0 * total_seconds, total_seconds)


def kodi_lastplayed_to_iso(kodi_lastplayed):
    """'YYYY-MM-DD HH:MM:SS' -> ISO-8601 'YYYY-MM-DDTHH:MM:SS' for the scrobble payload."""
    if not kodi_lastplayed:
        return None
    return kodi_lastplayed.replace(' ', 'T')


def _kodi_lastplayed_is_newer(kodi_lastplayed, chronicle_last_iso):
    """True if Kodi's lastplayed is strictly more recent than Chronicle's.
    A missing value on either side counts as "epoch" (always older)."""

    def _parse_kodi(s):
        if not s:
            return datetime.min
        try:
            return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return datetime.min

    def _parse_iso(s):
        if not s:
            return datetime.min
        try:
            return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return datetime.min

    return _parse_kodi(kodi_lastplayed) > _parse_iso(chronicle_last_iso)
