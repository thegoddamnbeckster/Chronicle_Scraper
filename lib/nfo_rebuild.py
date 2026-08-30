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
single per-item pipeline (see the sequencing comment below) and a single
combined result dict, so default.py's summary stays exactly as simple as it
was when this only handled movies; the counts just now mean "across
everything", not "movies only".

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

# Per-user correction (2026-08-29): "you delete everything first and then
# rewrite everything ... look at each video item individually and delete
# and rebuild rather than doing all the deletes and then the whole
# rebuild." Confirmed as a real problem, not just a perception one: even
# though the PREVIOUS version of this module issued each item's delete
# immediately before that same item's refresh, delete completes almost
# instantly while VideoLibrary.RefreshMovie is fire-and-forget -- the
# actual scrape+write happens later, whenever Kodi's own library thread
# gets to it. Because every item's delete+refresh was issued in one fast
# burst before ANY waiting began, the entire library could sit with NO
# valid local NFO for the whole multi-minute (or longer) batch-wait
# window -- worse than the "everything is briefly gone" this rewrite is
# meant to fix, and a real data-loss risk if the run is interrupted
# (Kodi crash, network drop, cancel) anywhere in that window: every item
# whose old NFO was already deleted but whose new one hadn't landed yet
# is left with nothing.
#
# This version never issues item N+1's delete until item N's own refresh
# has either been confirmed on disk or timed out -- at most ONE item is
# ever without a valid NFO at a time, for at most _PER_ITEM_TIMEOUT_SECONDS.
#
# This looks superficially like the "confirmed WRONG live 2026-08-04"
# per-item-wait attempt mentioned in this module's git history, so it's
# worth being explicit about why it isn't the same mistake: that attempt
# waited (capped at 20s) for one item's NFO AFTER a whole batch of refreshes
# had ALREADY been issued in a burst -- Kodi's queue was already deep by
# the time the wait started, so a single item's real completion time (one
# case took ~88s) had no relationship to a per-item timeout sized for the
# uncontended case. THIS version never creates that backlog in the first
# place -- at most one refresh is ever in flight, so each item's own
# processing time should stay close to the ~1-8s per-movie range observed
# when Kodi's queue isn't backed up, and _PER_ITEM_TIMEOUT_SECONDS below is
# sized with real headroom above that, not against the deep-queue outlier.
_PER_ITEM_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 3.0
# Tiny pacing floor between items even when one resolves almost instantly
# (e.g. a fast local Chronicle response) -- avoids hammering Chronicle/the
# SMB share with a tight back-to-back loop; negligible next to a real
# item's multi-second wait.
_MIN_ITEM_SPACING_SECONDS = 0.3


def run(progress_callback=None, is_cancelled=None):
    """Deletes and rebuilds every local .nfo/tvshow.nfo/movieset-* file for
    every movie, TV show, and episode already in Kodi's library, ONE ITEM AT
    A TIME: delete this item's old file(s), issue its refresh, wait for its
    own new NFO to actually reappear on disk (or time out), THEN move on to
    the next item. See the module-level comment above for why this
    sequencing -- not a batch delete-everything-then-wait-on-everything --
    is both safer (at most one item is ever without a valid NFO) and, once
    Kodi's queue isn't artificially backlogged by a burst of refreshes,
    about as fast in practice.

    progress_callback(index, total, label), if given, is called once per
    item (movie, then show, then episode), right as that item starts
    processing -- covers the whole delete+refresh+wait for that item, not
    just the issue step, so a caller's progress UI stays accurate for
    however long that one item actually takes.

    is_cancelled(), if given, is checked before each item starts AND between
    polls while waiting on the current item, and stops the run early
    (whatever refresh is already in flight for the current item keeps
    running in Kodi's own queue regardless -- there's no way to un-issue it).

    Returns a dict (deliberately named, not positional) -- every count is a
    COMBINED total across movies, shows, and episodes (movieset_deleted is
    the one movie-only exception, since only movies have that convention at
    all), so a caller's summary message stays exactly as simple as when this
    only ever handled movies:
      total              -- movies + shows + episodes in the library when
                             this started
      processed          -- how many were fully handled (delete+refresh+wait,
                             confirmed or not) before a cancel, if any
      cancelled           -- True if the run was stopped early via
                             is_cancelled()
      pending_total       -- of `processed`, how many got a real refresh
                             request accepted and so were actually waited on
                             (`processed` minus refresh_errors, minus any
                             whose NFO path couldn't be predicted)
      nfo_confirmed        -- of `pending_total`, how many were actually
                             observed back on disk before their own timeout
      unconfirmed_count    -- pending_total - nfo_confirmed; never reappeared
                             within that item's own wait budget (see
                             kodi.log for which)
      nfo_deleted           -- old .nfo/tvshow.nfo files removed, across all
                             three item types
      movieset_deleted      -- old movieset-* art files removed likewise
                             (movies only)
      refresh_errors        -- items where Kodi's own Refresh* JSON-RPC call
                             itself was rejected -- never even entered the wait
    """
    # Set for the entire pass -- Kodi's own Refresh* queue can call back into
    # get_details()/get_episode_details() at any point up to the very end of
    # the current item's wait, and rebuild_state is what tells those calls
    # it's safe to actually write the NFO this time. finally guarantees this
    # clears even on an unexpected exception, so a crash here can't leave
    # inline NFO writing silently stuck on forever.
    rebuild_state.mark_started()
    try:
        return _run(progress_callback, is_cancelled)
    finally:
        rebuild_state.mark_finished()


def _run(progress_callback, is_cancelled):
    movies = _get_all_movies()
    shows = _get_all_tvshows()
    episodes = _get_all_episodes()
    total = len(movies) + len(shows) + len(episodes)

    nfo_deleted = 0
    movieset_deleted = 0
    refresh_errors = 0
    pending_total = 0
    nfo_confirmed = 0
    unconfirmed = []
    processed = 0
    cancelled = False

    log.info('nfo_rebuild: starting -- {0} movies, {1} shows, {2} episodes in library'.format(
             len(movies), len(shows), len(episodes)))

    def _cancelled_now():
        if is_cancelled is not None and is_cancelled():
            log.warning('nfo_rebuild: cancelled by user at {0}/{1}'.format(processed, total))
            return True
        return False

    def _finish_item(label, expected_nfo, refresh_ok):
        """Shared tail end of processing one item, once its delete+refresh
        have already been issued -- waits for expected_nfo (if predictable
        and the refresh was accepted), tallies every counter this function
        closes over. Nothing to wait on (refresh rejected, or the path
        couldn't be predicted) just falls straight through."""
        nonlocal refresh_errors, pending_total, nfo_confirmed, unconfirmed
        if not refresh_ok:
            refresh_errors += 1
            return
        if not expected_nfo:
            return
        pending_total += 1
        if _wait_for_one(expected_nfo, _PER_ITEM_TIMEOUT_SECONDS, is_cancelled):
            nfo_confirmed += 1
        else:
            unconfirmed.append((label, expected_nfo))
            log.warning('nfo_rebuild: "{0}" -- NFO never reappeared at {1} within {2:.0f}s '
                        '(scraper may not have matched this title; check kodi.log)'.format(
                        label, expected_nfo, _PER_ITEM_TIMEOUT_SECONDS))

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

        refresh_ok = _refresh_movie(movie['movieid'])
        _finish_item(movie.get('label') or str(movie['movieid']), expected_nfo, refresh_ok)

        processed += 1
        xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))

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

            refresh_ok = _refresh_show(show['tvshowid'])
            expected_nfo = folder + 'tvshow.nfo' if folder else None
            _finish_item(show.get('label') or str(show['tvshowid']), expected_nfo, refresh_ok)

            processed += 1
            xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))

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

            refresh_ok = _refresh_episode(episode['episodeid'])
            if refresh_ok:
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
            _finish_item(label, expected_nfo, refresh_ok)

            processed += 1
            xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))

    log.info('nfo_rebuild: done -- {0}/{1} items processed, {2} confirmed rewritten, '
             '{3} nfo deleted, {4} movieset files deleted, {5} refresh errors'.format(
             processed, total, nfo_confirmed, nfo_deleted, movieset_deleted, refresh_errors))
    return {
        'total': total,
        'processed': processed,
        'cancelled': cancelled,
        'pending_total': pending_total,
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
    this one can't predict), but right often enough to make waiting
    worthwhile; a wrong guess just means this item never gets checked off
    and shows up as unconfirmed, even if the real write succeeded under a
    different filename. Despite the name, this applies equally to episodes
    -- they use the identical convention."""
    if not folder or not file_path:
        return None
    basename = file_path.rsplit('/', 1)[-1]
    stem = strip_video_ext(basename)
    return folder + stem + '.nfo'


def _wait_for_one(path, budget_seconds, is_cancelled=None):
    """Polls a single expected NFO path until it exists, up to budget_seconds.
    Returns True if it appeared in time, False otherwise (timeout, or
    cancelled mid-wait)."""
    waited = 0.0
    while waited < budget_seconds:
        if xbmcvfs.exists(path):
            return True
        if is_cancelled is not None and is_cancelled():
            return False
        xbmc.sleep(int(_POLL_INTERVAL_SECONDS * 1000))
        waited += _POLL_INTERVAL_SECONDS
    # Final check -- the file may have appeared in the gap between the last
    # loop iteration's check and the budget running out.
    return xbmcvfs.exists(path)


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
