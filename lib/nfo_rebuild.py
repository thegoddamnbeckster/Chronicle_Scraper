# -*- coding: utf-8 -*-
"""Bulk "nuke and rebuild" for local movie/TV-show/episode NFO and set-art
files -- the actual mechanism the "Recreate local NFO file from Chronicle's
data" setting needs to be useful for a library that already has local
NFOs, not just new additions.

Why this is the ONLY way a local NFO ever gets written, not just a bulk
convenience action: nfo_writer.py/tv_nfo_writer.py's write calls in
get_details()/get_episode_details() are gated on rebuild_state.is_active()
-- they no-op unless this module's own run() is the thing currently invoking
them (see lib/rebuild_state.py's module docstring for why that gate exists:
an ordinary library scan must not pay for local-file NFO writes just to get
new items into Kodi's library, and Kodi's find/getdetails contract gives
those two situations no other way to be told apart). This module is what
sets that flag: it deletes each item's local NFO (and movieset-* art, a
separate but equally local-file-wins convention Kodi supports for movies),
then force-refreshes so Kodi has no choice but to ask Chronicle for a real
answer -- WHILE the flag is set, so this pass's own refreshes are the ones
allowed to actually write. Also the only way to reach items that already
have a local NFO at all: Kodi never calls find/getdetails for anything with
an existing local NFO on disk, from this addon or anywhere else
(tinyMediaManager, another scraper, a hand-written one) -- confirmed
directly via kodi.log and a live VideoLibrary.RefreshMovie test
(2026-07-30) for movies -- so deleting the stale file first is what forces
Chronicle_Scraper to be asked again in the first place.

Still deliberately explicit and confirmed, not automatic -- default.py must
show a clear warning before calling run(), and it can still take a long
time for a large library. But it's no longer a one-way loss for data only
some OTHER tool (tinyMediaManager, hand edits) put in these files: before
each NFO is deleted, its content is parsed and stashed (lib/legacy_nfo.py)
so the very rescan this triggers folds it back into both the freshly
written NFO and Chronicle itself; before each movieset-* file is deleted,
it's copied into the dedicated Movie Set Information folder if nothing is
there already (collection_sync.preserve_local_movieset_file). Every deleted
file is still Kodi's own local-metadata copy, never the video file itself.

Covers three item types in one combined pass -- movies, TV shows (their own
tvshow.nfo), and individual episodes (their own per-file NFO) -- sharing a
single issue-then-wait pipeline (see the pacing/batch-wait comment below)
and a single combined result dict, so default.py's summary stays exactly
as simple as it was when this only handled movies; the counts just now mean
"across everything", not "movies only".

Caveat worth flagging: the movie half of this file was hardened over many
rounds of real, confirmed kodi.log-driven bug fixes (v2.2.0-v2.9.0 -- see
addon.xml's own changelog). The TV half is new and has NOT had that same
live-verification pass yet, in particular whether VideoLibrary.RefreshEpisode
reliably queues a real getepisodedetails call the same dependable way
RefreshMovie does for movies -- worth watching kodi.log closely the first
time this runs against a real TV library.
"""

import json
import posixpath

import xbmc
import xbmcvfs

from lib import episode_path_cache
from lib import legacy_nfo
from lib import rebuild_state
from lib.collection_sync import preserve_local_movieset_file
from lib.logger import Logger
from lib.movie_art_sync import listdir_with_timeout, strip_video_ext

log = Logger('nfo_rebuild')

# Confirmed live 2026-08-03: VideoLibrary.RefreshMovie is fire-and-forget --
# it acks the JSON-RPC call and queues the actual scrape (HTTP call to
# Chronicle, image downloads, an NFO write) on Kodi's own library thread,
# which processes that queue at whatever pace the real work takes -- observed
# anywhere from ~1s to 8+s per movie depending on image downloads. A blind
# fixed-interval sleep between RefreshMovie calls (previously 1.0s) has no
# relationship to that: it made the loop *issue* one request per movie per
# second while the actual rebuild dragged on for another ~20 minutes after
# the loop reported "done", quietly reporting "N movies processed" when that
# only ever meant "N refresh requests were accepted", not "N NFOs were
# rewritten".
#
# First fix attempt (2026-08-03) waited per-movie for its own NFO to reappear
# before issuing the next delete+refresh, capped at 20s. Confirmed WRONG live
# 2026-08-04: Kodi's internal scrape queue does not drain in lockstep with
# issue order -- "Crawl"'s wait gave up at 09:20:36 (no NFO within 20s), but
# the NFO was actually written at 09:22:04, ~88s later. The queue's own depth
# (everything already in flight from prior refreshes, plus whatever a
# concurrent library scan is doing) determines how long any single movie
# actually takes to reach the front, and that has no fixed relationship to
# a per-movie timeout -- a real success just gets misreported as "not
# confirmed", which is exactly the same class of dishonest count as the
# original bug, just inverted (too pessimistic instead of too optimistic).
#
# Correct model: issuing delete+refresh is cheap and shouldn't be gated on
# any one item's own completion -- fire them all (paced only enough to not
# slam Chronicle/the SMB shares with an instant burst), then wait on the
# WHOLE BATCH draining, checking off each item's NFO as it reappears,
# instead of demanding item N finish before item N+1 even starts.
_ISSUE_PACING_SECONDS = 0.3
_POLL_INTERVAL_SECONDS = 3.0
# Overall batch-wait budget scales with how much work was actually queued,
# not a flat per-item number -- floor covers small batches where per-item
# variance dominates, the per-item factor covers large ones where queue
# depth dominates. 6s/item comfortably covers the ~88s-for-one-movie-deep-
# in-the-queue case observed live without being so long a genuinely-failed
# match blocks the run for hours.
_BATCH_WAIT_FLOOR_SECONDS = 180.0
_BATCH_WAIT_PER_ITEM_SECONDS = 6.0


def run(progress_callback=None, is_cancelled=None, wait_progress_callback=None,
        on_issuing_complete=None, wait_is_cancelled=None):
    """Deletes every local .nfo/tvshow.nfo/movieset-* file for every movie,
    TV show, and episode already in Kodi's library, then refreshes each one
    so Chronicle_Scraper repopulates them fresh. Issues all delete+refresh
    requests first (briefly paced so it's not one instant burst), then waits
    on the whole combined batch reappearing together -- see the module-level
    comment above for why waiting on each item individually before starting
    the next was wrong.

    progress_callback(index, total, label), if given, is called once per
    item (movie, then show, then episode) during the issue phase.
    is_cancelled(), if given, is checked between items during the issue
    phase AND during the batch wait, and stops the run early (already-issued
    refreshes keep running in Kodi's own queue either way -- there's no way
    to un-issue them).

    wait_progress_callback(confirmed, pending_total, waited_seconds,
    budget_seconds), if given, is called once per poll during the batch wait
    -- this phase has no natural "index/total item" progress of its own
    (nothing is being issued anymore, just waited on), so without this a
    caller's progress UI has nothing to update for however long the wait
    takes and looks frozen even though the batch is actively draining.

    on_issuing_complete(pending_total, budget_seconds), if given, is called
    once, right as the issue phase ends and the (possibly long) batch wait is
    about to start -- lets a caller swap a foreground/blocking progress UI
    for a background one at exactly the point the user no longer needs to be
    watching: everything left to do from here is Kodi's own library queue
    draining, not anything the addon script is still driving that a user
    could meaningfully interrupt.

    wait_is_cancelled, if given, is checked instead of is_cancelled during
    the batch wait (separate from the issue-phase check) -- a caller that
    moves to a non-interactive background indicator for the wait phase (see
    on_issuing_complete above) has nothing left offering a Cancel button by
    that point, so it should leave this unset rather than reuse a check tied
    to a UI control it already closed.

    Returns a dict (deliberately named, not positional) -- every count is a
    COMBINED total across movies, shows, and episodes (movieset_deleted is
    the one movie-only exception, since only movies have that convention at
    all), so a caller's summary message stays exactly as simple as when this
    only ever handled movies:
      total              -- movies + shows + episodes in the library when
                             this started
      processed          -- how many were actually issued a delete+refresh
                             (< total only if cancelled during the issue phase)
      cancelled           -- True if the issue phase was stopped early via
                             is_cancelled(); the wait phase always runs to
                             completion on whatever was issued regardless
      pending_total       -- of `processed`, how many got a real refresh
                             request accepted and so are expected to produce
                             a new NFO (`processed` minus refresh_errors,
                             minus any whose NFO path couldn't be predicted)
      nfo_confirmed        -- of `pending_total`, how many were actually
                             observed back on disk by the end of the wait --
                             the honest "actually done" count
      unconfirmed_count    -- pending_total - nfo_confirmed; never reappeared
                             within the wait budget (see kodi.log for which)
      nfo_deleted           -- old .nfo/tvshow.nfo files removed during the
                             issue phase, across all three item types
      movieset_deleted      -- old movieset-* art files removed likewise
                             (movies only)
      refresh_errors        -- items where Kodi's own Refresh* JSON-RPC call
                             itself was rejected -- never even entered the wait
    """
    # Set for the entire issue-and-wait pass, not just the issue phase --
    # Kodi's own Refresh* queue can call back into get_details()/
    # get_episode_details() at any point up to the very end of the batch
    # wait (see the queue-timing comment above), and rebuild_state is what
    # tells those calls it's safe to actually write the NFO this time.
    # finally guarantees this clears even on an unexpected exception, so a
    # crash here can't leave inline NFO writing silently stuck on forever.
    rebuild_state.mark_started()
    try:
        return _run(progress_callback, is_cancelled, wait_progress_callback,
                     on_issuing_complete, wait_is_cancelled)
    finally:
        rebuild_state.mark_finished()


def _run(progress_callback, is_cancelled, wait_progress_callback,
         on_issuing_complete, wait_is_cancelled):
    movies = _get_all_movies()
    shows = _get_all_tvshows()
    episodes = _get_all_episodes()
    total = len(movies) + len(shows) + len(episodes)

    nfo_deleted = 0
    movieset_deleted = 0
    refresh_errors = 0
    processed = 0
    cancelled = False
    pending = {}  # (kind, id) -> (label, expected_nfo_path)

    log.info('nfo_rebuild: starting -- {0} movies, {1} shows, {2} episodes in library'.format(
             len(movies), len(shows), len(episodes)))

    def _cancelled_now():
        if is_cancelled is not None and is_cancelled():
            log.warning('nfo_rebuild: cancelled by user during issue phase at {0}/{1}'.format(
                        processed, total))
            return True
        return False

    for movie in movies:
        if _cancelled_now():
            cancelled = True
            break
        if progress_callback is not None:
            progress_callback(processed, total, movie.get('label') or '')

        file_path = movie.get('file') or ''
        folder = file_path.rsplit('/', 1)[0] + '/' if '/' in file_path else None
        # Same stem python/scraper.py's get_details() will later derive via
        # find_movie_location() for this same movie -- both read Kodi's own
        # VideoLibrary "file" field, so the two stay in lockstep and the
        # stash this delete step writes is guaranteed to be found again.
        stash_key = strip_video_ext(file_path.rsplit('/', 1)[-1]) if file_path else None
        set_name = movie.get('set') or None
        expected_nfo = _expected_movie_nfo_path(folder, file_path)

        if folder:
            deleted_nfo, deleted_movieset = _delete_local_metadata(folder, stash_key, set_name)
            nfo_deleted += deleted_nfo
            movieset_deleted += deleted_movieset

        if not _refresh_movie(movie['movieid']):
            refresh_errors += 1
        elif expected_nfo:
            pending[('movie', movie['movieid'])] = (movie.get('label') or str(movie['movieid']), expected_nfo)

        processed += 1
        xbmc.sleep(int(_ISSUE_PACING_SECONDS * 1000))

    if not cancelled:
        for show in shows:
            if _cancelled_now():
                cancelled = True
                break
            if progress_callback is not None:
                progress_callback(processed, total, show.get('label') or '')

            folder = show.get('file') or ''
            if folder and not folder.endswith('/'):
                folder += '/'
            stash_key = posixpath.basename(folder.rstrip('/')) if folder else None

            if folder and _delete_show_nfo(folder, stash_key):
                nfo_deleted += 1

            if not _refresh_show(show['tvshowid']):
                refresh_errors += 1
            elif folder:
                pending[('show', show['tvshowid'])] = (show.get('label') or str(show['tvshowid']), folder + 'tvshow.nfo')

            processed += 1
            xbmc.sleep(int(_ISSUE_PACING_SECONDS * 1000))

    if not cancelled:
        for episode in episodes:
            if _cancelled_now():
                cancelled = True
                break
            label = episode.get('label') or 'S{0}E{1}'.format(episode.get('season'), episode.get('episode'))
            if progress_callback is not None:
                progress_callback(processed, total, label)

            file_path = episode.get('file') or ''
            folder = file_path.rsplit('/', 1)[0] + '/' if '/' in file_path else None
            stash_key = strip_video_ext(file_path.rsplit('/', 1)[-1]) if file_path else None
            expected_nfo = _expected_movie_nfo_path(folder, file_path)  # same "<basename>.nfo" convention

            if folder and stash_key and _delete_episode_nfo(folder, stash_key):
                nfo_deleted += 1

            if not _refresh_episode(episode['episodeid']):
                refresh_errors += 1
            else:
                # Stashed only once the refresh is actually issued (RefreshEpisode
                # is fire-and-forget -- confirmed live 2026-08-03 for RefreshMovie,
                # same queuing model here -- so there's no risk get_episode_details()'s
                # callback runs before this line does), using file_path while it's
                # still the real, already-committed value from BEFORE this refresh.
                # See episode_path_cache.py's module docstring for why
                # get_episode_details()'s own live VideoLibrary lookup for this same
                # episode can't be trusted to find this once RefreshEpisode is in
                # flight.
                if file_path:
                    episode_path_cache.save(episode.get('tvshowid'), episode.get('season'),
                                             episode.get('episode'), file_path)
                if expected_nfo:
                    pending[('episode', episode['episodeid'])] = (label, expected_nfo)

            processed += 1
            xbmc.sleep(int(_ISSUE_PACING_SECONDS * 1000))

    budget = max(_BATCH_WAIT_FLOOR_SECONDS, len(pending) * _BATCH_WAIT_PER_ITEM_SECONDS)
    log.info('nfo_rebuild: issue phase done -- {0} refreshes issued, waiting up to {1:.0f}s '
             'for the batch to drain'.format(len(pending), budget))
    if on_issuing_complete is not None:
        on_issuing_complete(len(pending), budget)
    nfo_confirmed, unconfirmed = _wait_for_batch(pending, budget, wait_is_cancelled, wait_progress_callback)

    for label, path in unconfirmed:
        log.warning('nfo_rebuild: "{0}" -- NFO never reappeared at {1} within the batch wait '
                    '(scraper may not have matched this title; check kodi.log)'.format(label, path))

    log.info('nfo_rebuild: done -- {0}/{1} items processed, {2} confirmed rewritten, '
             '{3} nfo deleted, {4} movieset files deleted, {5} refresh errors'.format(
             processed, total, nfo_confirmed, nfo_deleted, movieset_deleted, refresh_errors))
    return {
        'total': total,
        'processed': processed,
        'cancelled': cancelled,
        'pending_total': len(pending),
        'nfo_confirmed': nfo_confirmed,
        'unconfirmed_count': len(unconfirmed),
        'nfo_deleted': nfo_deleted,
        'movieset_deleted': movieset_deleted,
        'refresh_errors': refresh_errors,
    }


def _expected_movie_nfo_path(folder, file_path):
    """Best-effort prediction of where sync_movie_nfo()/sync_episode_nfo()
    will write this item's NFO -- same naming rule both use: the real video
    file's own basename with a .nfo extension. Not guaranteed (falls back to
    "movie.nfo" if the writer's own location lookup fails for some reason
    this one can't predict), but right often enough to make polling
    worthwhile; a wrong guess just means this item never gets checked off
    during the batch wait and shows up as unconfirmed, even if the real
    write succeeded under a different filename. Despite the name, this
    applies equally to episodes -- they use the identical convention."""
    if not folder or not file_path:
        return None
    basename = file_path.rsplit('/', 1)[-1]
    stem = strip_video_ext(basename)
    return folder + stem + '.nfo'


def _wait_for_batch(pending, budget_seconds, is_cancelled=None, wait_progress_callback=None):
    """Polls every (label, path) in pending until each path exists, checking
    off confirmed ones as it goes, up to budget_seconds total (not per-item
    -- see the module docstring for why a per-item cap was wrong). Returns
    (confirmed_count, [(label, path), ...] for whatever never showed up)."""
    remaining = dict(pending)  # (kind, id) -> (label, path)
    confirmed = 0
    waited = 0.0
    total = len(pending)
    next_log_at = 30.0  # heartbeat in kodi.log every ~30s even with no UI watching

    while remaining and waited < budget_seconds:
        if is_cancelled is not None and is_cancelled():
            log.warning('nfo_rebuild: cancelled during batch wait -- {0}/{1} still pending'.format(
                        len(remaining), len(pending)))
            break

        done = [key for key, (_label, path) in remaining.items() if xbmcvfs.exists(path)]
        for key in done:
            del remaining[key]
            confirmed += 1

        if wait_progress_callback is not None:
            wait_progress_callback(confirmed, total, waited, budget_seconds)

        if waited >= next_log_at:
            log.info('nfo_rebuild: batch wait -- {0}/{1} confirmed, {2:.0f}s/{3:.0f}s elapsed'.format(
                      confirmed, total, waited, budget_seconds))
            next_log_at += 30.0

        if not remaining:
            break

        xbmc.sleep(int(_POLL_INTERVAL_SECONDS * 1000))
        waited += _POLL_INTERVAL_SECONDS

    # Final check -- a path may have appeared in the gap between the last
    # loop iteration's check and the wait budget running out.
    for key, (_label, path) in list(remaining.items()):
        if xbmcvfs.exists(path):
            del remaining[key]
            confirmed += 1

    return confirmed, list(remaining.values())


def _get_all_movies():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovies',
        'params': {'properties': ['file', 'title', 'set']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get movie list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetMovies rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('movies') or []


def _get_all_tvshows():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetTVShows',
        'params': {'properties': ['file', 'title']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get TV show list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetTVShows rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('tvshows') or []


def _get_all_episodes():
    """Every episode in the whole library, flat across every show -- no
    tvshowid filter, same as Kodi's own library views default to. showtitle
    is requested purely for a readable progress label; tvshowid is requested
    so each episode's file path can be stashed (see episode_path_cache.py)
    under the same (tvshowid, season, episode) key that tvshow_scraper.py's
    get_episode_details() will look it up by."""
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetEpisodes',
        'params': {'properties': ['file', 'season', 'episode', 'showtitle', 'tvshowid']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get episode list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetEpisodes rejected: {0}'.format(response['error']))
        return []
    episodes = response.get('result', {}).get('episodes') or []
    for ep in episodes:
        show = ep.get('showtitle')
        if show:
            ep['label'] = '{0} S{1}E{2}'.format(show, ep.get('season'), ep.get('episode'))
    return episodes


def _delete_local_metadata(folder, stash_key, set_name):
    """Deletes any .nfo and movieset-* file directly in folder (timeout-
    guarded the same way movie_art_sync.py's own source browsing is, since
    this walks every movie's real folder and an unresponsive share here would
    otherwise hang the whole rebuild the same way it once hung art syncing).

    Before any .nfo file is deleted, its content is parsed and stashed (see
    lib/legacy_nfo.py) so the scrape this deletion triggers can fold
    whatever it contained into both the freshly-written NFO and Chronicle
    itself. Before any movieset-* file is deleted, it's salvaged into the
    dedicated Movie Set Information folder instead (see
    collection_sync.preserve_local_movieset_file) if that folder doesn't
    already have art for the same slot. Either way, nothing a previous tool
    (e.g. tinyMediaManager) already wrote is silently thrown away anymore.

    stash_key identifies the movie this folder belongs to (the real video
    file's own basename, stem only) -- NOT the existing NFO's own filename,
    since a legacy NFO (e.g. tinyMediaManager's generic "movie.nfo") is very
    often named differently than what this addon will write; keying by the
    video file itself is the only name guaranteed to line up with the later
    lookup in scraper.py's get_details(). set_name is the movie's own set,
    if any (Kodi's own "set" property on VideoLibrary.GetMovies) -- needed
    to know which dedicated set folder a movieset-* file's data belongs to.

    Returns (nfo_deleted, movieset_deleted)."""
    _dirs, files = listdir_with_timeout(folder)
    if files is None:
        return 0, 0

    nfo_count = 0
    movieset_count = 0
    harvested = {}
    for name in files:
        is_nfo = name.lower().endswith('.nfo')
        is_movieset = name.lower().startswith('movieset-')
        if not (is_nfo or is_movieset):
            continue
        path = folder + name

        if is_nfo:
            parsed = _read_and_parse(path, legacy_nfo.parse_legacy_nfo)
            if parsed:
                for key, value in parsed.items():
                    harvested.setdefault(key, value)
        elif is_movieset:
            preserve_local_movieset_file(set_name, path, name)

        try:
            if xbmcvfs.delete(path):
                if is_nfo:
                    nfo_count += 1
                else:
                    movieset_count += 1
            else:
                log.warning("xbmcvfs.delete() returned falsy for {0}".format(path))
        except Exception as exc:
            log.warning("Couldn't delete {0}: {1}".format(path, exc))

    if harvested:
        legacy_nfo.save_stash(stash_key, harvested)

    return nfo_count, movieset_count


def _delete_show_nfo(folder, stash_key):
    """Deletes tvshow.nfo in folder if present, harvesting its content
    first (see lib/legacy_nfo.py's parse_legacy_tvshow_nfo) and stashing it
    keyed by the show's own folder name -- the same key
    tv_nfo_writer.py's caller (tvshow_scraper.py's get_details()) derives
    for the same show. Returns True if a file was deleted."""
    path = folder + 'tvshow.nfo'
    if not xbmcvfs.exists(path):
        return False

    parsed = _read_and_parse(path, legacy_nfo.parse_legacy_tvshow_nfo)
    if parsed:
        legacy_nfo.save_stash(stash_key, parsed)

    try:
        if xbmcvfs.delete(path):
            return True
        log.warning("xbmcvfs.delete() returned falsy for {0}".format(path))
    except Exception as exc:
        log.warning("Couldn't delete {0}: {1}".format(path, exc))
    return False


def _delete_episode_nfo(folder, stash_key):
    """Deletes this episode's own "<basename>.nfo" in folder if present,
    harvesting its content first (see lib/legacy_nfo.py's
    parse_legacy_episode_nfo) and stashing it keyed by the video basename --
    the same key tv_nfo_writer.py's caller (tvshow_scraper.py's
    get_episode_details()) derives for the same episode. Returns True if a
    file was deleted."""
    path = folder + stash_key + '.nfo'
    if not xbmcvfs.exists(path):
        return False

    parsed = _read_and_parse(path, legacy_nfo.parse_legacy_episode_nfo)
    if parsed:
        legacy_nfo.save_stash(stash_key, parsed)

    try:
        if xbmcvfs.delete(path):
            return True
        log.warning("xbmcvfs.delete() returned falsy for {0}".format(path))
    except Exception as exc:
        log.warning("Couldn't delete {0}: {1}".format(path, exc))
    return False


def _read_and_parse(path, parser):
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't read {0} before deleting it: {1}".format(path, exc))
        return None
    return parser(raw)


def _refresh_movie(movieid):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.RefreshMovie',
        'params': {'movieid': movieid},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't refresh movieid {0}: {1}".format(movieid, exc))
        return False
    if 'error' in response:
        log.warning('RefreshMovie rejected movieid {0}: {1}'.format(movieid, response['error']))
        return False
    return True


def _refresh_show(tvshowid):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.RefreshTVShow',
        'params': {'tvshowid': tvshowid},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't refresh tvshowid {0}: {1}".format(tvshowid, exc))
        return False
    if 'error' in response:
        log.warning('RefreshTVShow rejected tvshowid {0}: {1}'.format(tvshowid, response['error']))
        return False
    return True


def _refresh_episode(episodeid):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.RefreshEpisode',
        'params': {'episodeid': episodeid},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't refresh episodeid {0}: {1}".format(episodeid, exc))
        return False
    if 'error' in response:
        log.warning('RefreshEpisode rejected episodeid {0}: {1}'.format(episodeid, response['error']))
        return False
    return True
