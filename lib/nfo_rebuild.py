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

Root-caused 2026-09-06: this used to source its work list directly from
THIS device's own VideoLibrary.GetMovies/GetTVShows/GetEpisodes (no explicit
sort, so every single invocation walked the exact same stable order
starting at item #1) with zero persisted progress between runs -- fine for
an occasional manual click, but turning on the "automatically rebuild after
every scan" setting for a library with tens of thousands of episodes meant
every trigger re-did the entire multi-hour pass from scratch, forever. Worse,
with several Kodi instances sharing the same library (this user runs five --
upstairs, downstairs, storage, vision, office -- see
[[project_kodi_device_ips]]), each one would have independently re-walked
and re-rebuilt the SAME shared NFO files, racing each other for no benefit.

Now sources its work from Chronicle's own cross-device rebuild queue instead
(lib/chronicle_client.py's claim_rebuild_batch/complete_rebuild_item/
release_rebuild_item, backed server-side by NfoRebuildQueueService) -- any
device can claim a batch of pending MediaItemIds, resolve each one in ITS
OWN local VideoLibrary (find_movie_location()/find_show_location()/
get_episode(), the same matchers get_details()/get_episode_details() already
use), and report back. Once ONE device confirms an item, it's marked done
centrally and no device -- including this one, on its next trigger -- will
be asked to redo it. A device that can't resolve a claimed item locally
(doesn't have that file, or Kodi hasn't indexed it yet) releases the claim
immediately so another device isn't stuck waiting out its full lease.

Caveat worth flagging: the movie half of the per-item delete+refresh+wait
pipeline was hardened over many rounds of real, confirmed kodi.log-driven
bug fixes (v2.2.0-v2.9.0 -- see addon.xml's own changelog) and is unchanged
by the 2026-09-06 rework above; only the SOURCING of what to process (queue
claims instead of a full local VideoLibrary walk) is new.

Per-user request (2026-09-06), after confirming this device alone was
carrying a 26,000+ item backlog with no other Kodi instance running
concurrently: a batch's items are now processed _CONCURRENT_ITEMS at a time
instead of strictly one at a time. This does NOT touch the 2026-08-29
per-user correction quoted above _process_movie_claim/etc. below -- each
ITEM's own delete-then-refresh-then-wait pipeline is still one indivisible
unit, never split into a separate delete pass and a separate rewrite pass.
What's new is that several DIFFERENT items' own pipelines can now be in
flight at once. This is safe for the same reason cross-device claiming is
safe: every queue row still resolves to exactly one claimant (the
server-side claim lock, unchanged), so two threads here can never process
the SAME item -- they're always working on distinct queue rows, distinct
local files, distinct VideoLibrary ids. The dominant cost per item is
waiting on Kodi's own scrape+write round trip (a network call to Chronicle,
then local/SMB file I/O), not CPU, which is exactly the kind of wait that
benefits from overlapping instead of queueing behind each other.
"""

import json
import posixpath
import threading

import xbmc
import xbmcaddon
import xbmcvfs

from lib import episode_path_cache
from lib import legacy_nfo
from lib import rebuild_state
from lib.chronicle_client import ChronicleClient
from lib.collection_sync import preserve_local_movieset_file
from lib.logger import Logger
from lib.movie_art_sync import find_movie_location, listdir_with_timeout, strip_video_ext
from lib.tvshow_location import find_show_location, get_episode

log = Logger('nfo_rebuild')
_ADDON = xbmcaddon.Addon()


def format_duration(seconds):
    """Renders a countdown as 'Xh Ym' / 'Ym' / 'less than a minute', for a rebuild's
    wait-phase ETA -- always rounds up so a shown estimate never expires before the
    thing it's estimating actually can. Shared by default.py's manual "Rebuild local
    NFOs" action and service.py's auto-rebuild-on-scan path, so both progress
    indicators render an ETA identically -- previously only default.py had this,
    duplicated rather than shared, when service.py grew its own progress callback."""
    minutes = int(seconds // 60) + (1 if seconds % 60 else 0)
    if minutes <= 0:
        return _ADDON.getLocalizedString(32104)  # "less than a minute"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return '{0}h {1}m'.format(hours, minutes)
    if hours:
        return '{0}h'.format(hours)
    return '{0}m'.format(minutes)


# Per-user correction (2026-08-29): "you delete everything first and then rewrite everything
# ... look at each video item individually and delete and rebuild rather than doing all the
# deletes and then the whole rebuild." This module still honors that -- each item's OWN
# delete-then-refresh-then-wait pipeline is still one indivisible unit, never split into a
# separate delete pass and a separate rewrite pass. What changed 2026-09-06 is WHERE the list
# of items comes from (a shared queue, not a full local walk), and later the same day, that
# _CONCURRENT_ITEMS distinct items' own pipelines can now be in flight together -- see module
# docstring's concurrency addendum for why that's a different question from the one this
# correction was about.
_PER_ITEM_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 3.0
# Minimum gap between DISPATCHING two successive items -- i.e. between issuing two RefreshMovie/
# RefreshTVShow/RefreshEpisode calls, not between two items finishing. Deliberately enforced at
# the dispatch loop (see the sleep right after each t.start() in _run), not inside each worker
# thread: this file was already confirmed live (2026-08-04, see addon.xml's own changelog) to
# make Kodi's own refresh pipeline back up badly when several refreshes were issued without
# pacing, ballooning per-item time well past its timeout -- the exact failure shape
# _CONCURRENT_ITEMS risks reintroducing at a smaller scale. Pacing the ISSUE rate globally
# (regardless of how many items are already mid-wait) is what actually protects against that,
# where a per-worker post-completion sleep would not.
_MIN_ITEM_SPACING_SECONDS = 0.3

# How many queue items to claim per round-trip to Chronicle -- large enough that claiming
# isn't a noticeable fraction of overall time (each item's own delete+refresh+wait already
# takes seconds), small enough that a cancelled/interrupted run only ever has this many items
# "checked out" (claimed-but-unprocessed) at once, each still recoverable by any device once
# its lease lapses. The lease duration itself is a server-side concern (NfoRebuildQueueService's
# own ClaimLease constant) -- this addon never sees or sets it.
_CLAIM_BATCH_SIZE = 25

# How many items within a claimed batch are processed at once -- see module docstring's
# 2026-09-06 concurrency addendum for why this is safe. Chosen conservatively: high enough to
# meaningfully overlap each item's dominant cost (waiting on Kodi's own scrape+write round
# trip), low enough that Kodi's own JSON-RPC dispatcher and Chronicle's single API instance
# aren't asked to do too many things at once. Tune here if real-world logs show either headroom
# (raise it) or contention (lower it) -- no code changes needed elsewhere.
_CONCURRENT_ITEMS = 3

# How many CONSECUTIVE resolution failures of the same kind ("movie"/"tvshow"/"episode") this
# device will absorb before giving up on that kind for the rest of THIS run. Root-caused live
# (2026-09-08): a device whose local library covers only a fraction of the shared catalog
# claimed, failed to resolve, and released 20,722 of 20,875 items in a single run -- almost
# entirely the same doomed kind, one at a time, each still costing a JSON-RPC lookup and an HTTP
# release round-trip. Per-user report the same day: "it should absolutely skip work that it
# can't resolve." Deliberately small: unlike SIMKL's own analogous streak-based backstop (a
# genuinely rare, transient condition worth 20 tries before concluding it's real), a device's
# own local library coverage for a given kind doesn't fluctuate mid-run -- if it's failed this
# many in a row, the next one is not meaningfully more likely to succeed, so there is little
# value in a higher threshold and real cost (more wasted round-trips) in one.
_KIND_FAILURE_STREAK_THRESHOLD = 10


def run(progress_callback=None, is_cancelled=None):
    """Claims batches of pending work from Chronicle's cross-device rebuild queue and, for
    each item, resolves it in this device's own local VideoLibrary, deletes its local
    NFO/movieset-* file(s), force-refreshes it, and waits for its own new NFO to actually
    reappear on disk (or time out) -- up to _CONCURRENT_ITEMS at once within a batch (see
    module docstring's concurrency addendum for why running several of these pipelines
    together is safe, and _MIN_ITEM_SPACING_SECONDS' own doc for how issuing them is still
    paced).

    progress_callback(index, total, label), if given, is called once per item, right as that
    item starts processing. `index` is a dispatch-order sequence number (unique and
    monotonically increasing across the whole run), NOT a count of items already fully
    finished -- with several items in flight at once, "finished so far" can no longer serve as
    a stable per-item index. `total` is recomputed at the start of each newly-claimed batch as
    (items already processed this run + the queue's own current pending count) -- an evolving
    estimate of the whole remaining backlog across every device, not just "this batch of 25".

    is_cancelled(), if given, is checked before dispatching each new item (at least once a
    second even while every concurrency slot is occupied, so a cancellation is never stuck
    waiting behind a slow item's own up-to-60s wait) and stops the run early -- whatever's
    already dispatched at that point keeps running in Kodi's own queue regardless, there's no
    way to un-issue it; those items stay claimed until this device's own lease lapses, at which
    point any device -- including this one, next run -- can pick them back up.

    Returns a dict (deliberately named, not positional):
      total               -- processed + the last-seen overall pending count (see above);
                              NOT "everything in the library" any more, since this device may
                              only see a fraction of the backlog before the queue runs dry or
                              the run is cancelled
      processed           -- items this device claimed and attempted this run
      cancelled           -- True if the run was stopped early via is_cancelled()
      resolution_failures -- of `processed`, how many this device released right back
                              (couldn't find the item in its own local VideoLibrary at all --
                              expected and normal for a device that doesn't have every shared
                              drive mounted, or hasn't scanned this file in yet)
      pending_total       -- of `processed`, how many got a real refresh request accepted and
                              so were actually waited on (excludes resolution_failures and
                              refresh_errors)
      nfo_confirmed       -- of `pending_total`, how many were actually observed back on disk
                              before their own timeout
      unconfirmed_count   -- pending_total - nfo_confirmed; never reappeared within that
                              item's own wait budget (see kodi.log for which)
      nfo_deleted         -- old .nfo/tvshow.nfo files removed, across all three item types
      movieset_deleted    -- old movieset-* art files removed likewise (movies only)
      refresh_errors      -- items where Kodi's own Refresh* JSON-RPC call itself was
                              rejected -- never even entered the wait, and left claimed
                              (not completed) so another device can retry once the lease lapses
      excluded_kinds      -- kind(s) ("movie"/"tvshow"/"episode") this run gave up requesting
                              after _KIND_FAILURE_STREAK_THRESHOLD consecutive resolution
                              failures on this device -- empty on a device whose library covers
                              everything it was asked to resolve
    """
    rebuild_state.mark_started()
    try:
        return _run(progress_callback, is_cancelled)
    finally:
        rebuild_state.mark_finished()


def _run(progress_callback, is_cancelled):
    client = ChronicleClient()

    # A single mutable dict (rather than a fresh set of `nonlocal` counters) so
    # _process_*_claim() below can feed nfo_deleted/movieset_deleted back into this run's own
    # totals without resorting to module-level globals -- those would leak across separate
    # run() calls within the same long-lived Kodi process (service.py's background service
    # never restarts between scans) and, in principle, corrupt each other's counts if two
    # rebuilds somehow overlapped. Every read/write of counters or unconfirmed from a worker
    # thread below goes through state_lock -- see module docstring's concurrency addendum.
    # _wait_for_one() itself (the actual up-to-60s block) deliberately happens OUTSIDE the
    # lock in _finish_item below: holding it there would serialize every item's own wait right
    # back into the one-at-a-time behaviour concurrency exists to avoid.
    counters = {
        'processed': 0,
        # Dispatch-order sequence number, separate from 'processed' (items fully FINISHED) --
        # see run()'s own doc for why the progress-display index needs its own counter now that
        # several items can be mid-flight (and hence "not yet processed") at once. Incremented
        # once per item at the moment it's dispatched, never decremented.
        'dispatched': 0,
        'resolution_failures': 0,
        'nfo_deleted': 0,
        'movieset_deleted': 0,
        'refresh_errors': 0,
        'pending_total': 0,
        'nfo_confirmed': 0,
    }
    unconfirmed = []
    cancelled = False
    # Per-kind consecutive-failure streak and the kinds it's already tripped for -- see
    # _KIND_FAILURE_STREAK_THRESHOLD's own doc. Both read/written only under state_lock, same as
    # counters/unconfirmed above. Scoped to this one run() call (fresh on every trigger, not
    # persisted) -- deliberately: it self-heals the moment this device's own library coverage
    # actually improves (a rescan, a newly-mounted share), and a fresh run only ever "re-pays"
    # up to _KIND_FAILURE_STREAK_THRESHOLD wasted attempts per kind to re-learn that, not
    # thousands, so there's little to gain and real staleness risk in persisting it further.
    consecutive_failures_by_kind = {}
    excluded_kinds = set()
    # Guards counters/unconfirmed -- brief, in-memory mutations only, see this comment's own
    # note above about why _wait_for_one() is deliberately never called while this is held.
    state_lock = threading.Lock()
    # Guards the actual local file I/O two items could otherwise race on: collection_sync.
    # preserve_local_movieset_file() and legacy_nfo.save_stash() both key their writes by a
    # SHARED name (a collection's own set name; a video file's own basename) rather than by
    # queue-item id, so two DIFFERENT claimed items (two movies in the same collection; two
    # episodes that happen to share a generic filename) can legitimately target the very same
    # destination file. Serializing just the delete+harvest+stash step -- fast, local/SMB file
    # I/O, never the up-to-60s Kodi wait -- costs almost nothing while removing the race
    # entirely; see _process_movie_claim/_process_show_claim/_process_episode_claim's own use.
    io_lock = threading.Lock()
    # Separate from state_lock deliberately: this only ever wraps the progress_callback
    # invocation itself (a call into Kodi's GUI subsystem), so a slow GUI update can never
    # block another thread's own counters mutation -- state_lock is for counters, this is for
    # display only, and the two must never share a lock.
    progress_lock = threading.Lock()

    log.info('nfo_rebuild: starting -- claiming from Chronicle\'s cross-device rebuild queue '
             'in batches of {0}, {1} at a time'.format(_CLAIM_BATCH_SIZE, _CONCURRENT_ITEMS))

    def _cancelled_now():
        if is_cancelled is not None and is_cancelled():
            with state_lock:
                processed_so_far = counters['processed']
            log.warning('nfo_rebuild: cancelled by user after {0} item(s) processed this run'.format(
                        processed_so_far))
            return True
        return False

    def _finish_item(label, expected_nfo, refresh_ok, queue_item_id):
        if not refresh_ok:
            with state_lock:
                counters['refresh_errors'] += 1
            log.warning('nfo_rebuild: "{0}" -- Kodi rejected the refresh call; leaving queue '
                        'item {1} claimed so it becomes retryable (by any device) once this '
                        'lease lapses'.format(label, queue_item_id))
            return
        if not expected_nfo:
            log.info('nfo_rebuild: "{0}" -- refresh accepted, nothing predictable to wait on; '
                     'marking queue item {1} done'.format(label, queue_item_id))
            client.complete_rebuild_item(queue_item_id)
            return

        with state_lock:
            counters['pending_total'] += 1
        # Deliberately unlocked -- see this function's own note in _run's own comment above.
        # Multiple threads block here concurrently, each on their own item's own path; that
        # overlap is the entire point of _CONCURRENT_ITEMS.
        wait_result = _wait_for_one(expected_nfo, _PER_ITEM_TIMEOUT_SECONDS, is_cancelled)
        if wait_result is None:
            # Cancelled mid-wait, NOT a genuine timeout -- see _wait_for_one's own doc for why
            # these two must be told apart. The refresh really was issued and may well still be
            # running in Kodi's own queue after this process gives up watching it, but nothing
            # here confirmed it landed -- leaving the item claimed (not completed) lets its
            # lease lapse naturally so any device (including this one, next run) re-checks it,
            # instead of permanently dropping a possibly-unwritten NFO from the queue forever.
            log.warning('nfo_rebuild: "{0}" -- cancelled while waiting for {1}; leaving queue '
                        'item {2} claimed so it gets rechecked once this lease lapses'.format(
                        label, expected_nfo, queue_item_id))
            return
        if wait_result:
            with state_lock:
                counters['nfo_confirmed'] += 1
            log.info('nfo_rebuild: "{0}" -- NFO confirmed rewritten at {1}'.format(label, expected_nfo))
        else:
            with state_lock:
                unconfirmed.append((label, expected_nfo))
            log.warning('nfo_rebuild: "{0}" -- NFO never reappeared at {1} within {2:.0f}s '
                        '(scraper may not have matched this title; check kodi.log)'.format(
                        label, expected_nfo, _PER_ITEM_TIMEOUT_SECONDS))
        # Completed either way (confirmed or genuinely timed out) once the refresh was accepted:
        # an unconfirmed-by-timeout item's refresh genuinely was issued and Chronicle's data was
        # already correct at the time -- our wait loop simply gave up watching first. Leaving it
        # claimed forever (or forcing another device to redo the whole delete+refresh dance)
        # would buy nothing; the warning above is what makes an unconfirmed item discoverable in
        # kodi.log. Cancellation (wait_result is None) is handled separately above and returns
        # before reaching here.
        client.complete_rebuild_item(queue_item_id)

    # Fallback only for the (rare) case the loop below exits on its very first cancellation
    # check, before ever claiming a batch -- always 0 at this point, recomputed for real the
    # moment the first batch comes back (see inside the loop).
    total_estimate = counters['processed']

    while True:
        if _cancelled_now():
            cancelled = True
            break

        with state_lock:
            exclude_kinds_snapshot = sorted(excluded_kinds)
        batch = client.claim_rebuild_batch(_CLAIM_BATCH_SIZE, exclude_kinds=exclude_kinds_snapshot)
        items = batch['items']
        if not items:
            if exclude_kinds_snapshot:
                log.info('nfo_rebuild: queue is empty (or unreachable) for the remaining kind(s) '
                         '-- nothing left to rebuild right now, {0} item(s) processed this run, '
                         '{1} excluded this run: {2}'.format(
                         counters['processed'], len(exclude_kinds_snapshot),
                         ', '.join(exclude_kinds_snapshot)))
            else:
                log.info('nfo_rebuild: queue is empty (or unreachable) -- nothing left to rebuild '
                         'right now, {0} item(s) processed this run'.format(counters['processed']))
            break

        # Recomputed once per batch, not per item -- see run()'s own doc for what this
        # estimates and why it can legitimately move around as other devices also work the
        # same queue.
        total_estimate = counters['processed'] + batch['totalPending']

        # Bounded worker pool via a counting semaphore + plain threads, rather than
        # concurrent.futures -- matches this codebase's existing threading style (service.py's
        # own background-service threads). semaphore.acquire() blocks the dispatch loop below
        # once _CONCURRENT_ITEMS threads are already in flight; each worker releases it in its
        # own finally block the moment IT finishes, immediately freeing a slot for the next
        # claim in this batch -- not a fixed "wait for the whole group of 3, then start the
        # next 3" barrier, which would let one slow straggler (e.g. a 60s timeout) idle the
        # other slots instead of letting them pick up more work.
        semaphore = threading.Semaphore(_CONCURRENT_ITEMS)
        threads = []

        def _process_claim_in_thread(claim):
            queue_item_id = claim.get('queueItemId')
            try:
                if _cancelled_now():
                    return

                kind = claim.get('kind')
                label = _label_for_claim(claim)

                # A batch already in hand can still contain a kind that JUST tripped
                # _KIND_FAILURE_STREAK_THRESHOLD from an earlier item in this same batch (the
                # exclude_kinds sent with the claim request only takes effect on the NEXT claim
                # round-trip) -- release immediately without even attempting local resolution,
                # rather than paying for a lookup already known to fail. See
                # _KIND_FAILURE_STREAK_THRESHOLD's own doc.
                with state_lock:
                    kind_already_excluded = kind in excluded_kinds
                if kind_already_excluded:
                    log.info('nfo_rebuild: queue item {0} ({1}) "{2}" -- {1} already excluded '
                             'this run (see earlier warning), releasing without attempting local '
                             'resolution'.format(queue_item_id, kind, label))
                    client.release_rebuild_item(queue_item_id)
                    with state_lock:
                        counters['resolution_failures'] += 1
                        counters['processed'] += 1
                    return

                with state_lock:
                    counters['dispatched'] += 1
                    index_for_display = counters['dispatched'] - 1  # zero-based, matches old semantics
                if progress_callback is not None:
                    with progress_lock:
                        progress_callback(index_for_display, total_estimate, label)

                log.info('nfo_rebuild: processing queue item {0} ({1}) "{2}"'.format(
                         queue_item_id, kind, label))

                resolved = False
                if kind == 'movie':
                    resolved = _process_movie_claim(
                        client, claim, label, queue_item_id, _finish_item, counters, state_lock, io_lock)
                elif kind == 'tvshow':
                    resolved = _process_show_claim(
                        client, claim, label, queue_item_id, _finish_item, counters, state_lock, io_lock)
                elif kind == 'episode':
                    resolved = _process_episode_claim(
                        client, claim, label, queue_item_id, _finish_item, counters, state_lock, io_lock)
                else:
                    log.warning('nfo_rebuild: queue item {0} has unrecognized kind {1!r} -- '
                                'releasing'.format(queue_item_id, kind))
                    client.release_rebuild_item(queue_item_id)

                with state_lock:
                    if not resolved:
                        counters['resolution_failures'] += 1
                        streak = consecutive_failures_by_kind.get(kind, 0) + 1
                        consecutive_failures_by_kind[kind] = streak
                        if streak >= _KIND_FAILURE_STREAK_THRESHOLD and kind not in excluded_kinds:
                            excluded_kinds.add(kind)
                            log.warning(
                                'nfo_rebuild: {0} consecutive "{1}" resolution failures on this '
                                'device -- its local library doesn\'t cover this kind well enough '
                                'right now; excluding {1} from further claims for the rest of '
                                'this run'.format(streak, kind))
                    else:
                        consecutive_failures_by_kind[kind] = 0
                    counters['processed'] += 1
            except Exception as exc:
                # Deliberately broad: an uncaught exception here would otherwise kill this
                # thread silently (Python's default unhandled-thread-exception handling is a
                # console traceback, not this addon's own log), permanently undercounting
                # 'processed'/'resolution_failures' and leaving queue_item_id's fate unresolved
                # with no trace in kodi.log. Left claimed (not completed/released) so it becomes
                # retryable once the lease lapses -- same fail-safe as every other failure path
                # in this module; we genuinely don't know enough about an UNEXPECTED exception
                # to say a release (implying "definitely not found here") is more correct.
                log.error('nfo_rebuild: queue item {0} ("{1}") failed unexpectedly: {2} -- '
                          'leaving it claimed so it becomes retryable once this lease '
                          'lapses'.format(queue_item_id, _label_for_claim(claim), exc))
            finally:
                semaphore.release()

        for claim in items:
            if _cancelled_now():
                cancelled = True
                break
            # Polls with a short timeout rather than a bare, indefinite semaphore.acquire() --
            # see run()'s own doc: without this, once all _CONCURRENT_ITEMS slots are occupied
            # (e.g. by items each mid-way through their own up-to-60s wait), this loop would
            # block on acquire() and never re-check is_cancelled() until a slot happened to
            # free, delaying a cancellation by up to a full item timeout instead of ~1s.
            while not semaphore.acquire(timeout=1.0):
                if _cancelled_now():
                    cancelled = True
                    break
            if cancelled:
                break
            # Paces the ISSUE rate of new refreshes globally (see _MIN_ITEM_SPACING_SECONDS'
            # own doc) -- run from the dispatch loop, not from inside each worker thread, so it
            # bounds how fast NEW Kodi refreshes are fired regardless of how many are already
            # mid-wait, rather than only pacing a single thread's own successive items.
            xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))
            t = threading.Thread(target=_process_claim_in_thread, args=(claim,),
                                  name='chronicle-nfo-rebuild-item', daemon=True)
            threads.append(t)
            t.start()

        # Always waits for every thread THIS batch already started, even after a cancellation
        # broke the dispatch loop above -- same "whatever's already in flight keeps running"
        # semantics run()'s own doc describes, just applied to however many threads (up to
        # _CONCURRENT_ITEMS) happen to be live at the moment of cancellation instead of one.
        for t in threads:
            t.join()

        if cancelled:
            break

    excluded_kinds_final = sorted(excluded_kinds)
    log.info(
        'nfo_rebuild: done -- {0} item(s) processed this run, {1} resolution failure(s), '
        '{2} confirmed rewritten, {3} nfo deleted, {4} movieset file(s) deleted, '
        '{5} refresh error(s){6}'.format(
            counters['processed'], counters['resolution_failures'], counters['nfo_confirmed'],
            counters['nfo_deleted'], counters['movieset_deleted'], counters['refresh_errors'],
            ', excluded this run: {0}'.format(', '.join(excluded_kinds_final))
            if excluded_kinds_final else ''))
    return {
        # total_estimate should already be >= processed by construction (it's computed as
        # processed-so-far + the server's own pending count, which can't be negative) -- the
        # max() is just a floor, not a sign this can normally go the other way, so a final
        # summary line never reads as "we did MORE than the total" even in a scenario this
        # module's own reasoning didn't anticipate.
        'total': max(total_estimate, counters['processed']),
        'processed': counters['processed'],
        'cancelled': cancelled,
        'resolution_failures': counters['resolution_failures'],
        'pending_total': counters['pending_total'],
        'nfo_confirmed': counters['nfo_confirmed'],
        'unconfirmed_count': len(unconfirmed),
        'nfo_deleted': counters['nfo_deleted'],
        'movieset_deleted': counters['movieset_deleted'],
        'refresh_errors': counters['refresh_errors'],
        # Kinds this run gave up requesting after _KIND_FAILURE_STREAK_THRESHOLD consecutive
        # failures -- see that constant's own doc. Empty on a device whose library covers
        # everything it was asked to resolve.
        'excluded_kinds': excluded_kinds_final,
    }


def _label_for_claim(claim):
    kind = claim.get('kind')
    if kind == 'episode':
        # No "?" placeholder when showName is missing -- the old per-Kodi-list label this
        # replaces (_get_all_episodes()'s own showtitle-or-nothing fallback) degraded to a
        # plain "S1E2" in that case, not a show name replaced by a literal question mark.
        show_name = claim.get('showName')
        episode_code = 'S{0}E{1}'.format(claim.get('season'), claim.get('episode'))
        return '{0} {1}'.format(show_name, episode_code) if show_name else episode_code
    if claim.get('year'):
        return '{0} ({1})'.format(claim.get('name') or '?', claim.get('year'))
    return claim.get('name') or '?'


def _process_movie_claim(client, claim, label, queue_item_id, finish_item, counters, state_lock, io_lock):
    """Resolves a claimed movie to this device's own local VideoLibrary via the exact same
    matcher get_details() already uses, then runs the same delete+refresh pipeline the old
    full-library walk used to. Returns True if the item resolved locally (regardless of
    whether the refresh/wait that followed succeeded), False if it was released back to the
    queue for another device. counters is _run()'s own shared dict -- see its own comment for
    why this is threaded through explicitly rather than via module-level globals; state_lock
    guards every counters mutation below since this can now run concurrently with sibling calls
    for other claims in the same batch (see module docstring's concurrency addendum). io_lock
    guards the actual local file writes (see _run's own comment on it) -- a SEPARATE movie in
    the same Chronicle collection, processed by a sibling thread right now, could otherwise
    target the exact same movieset-* art file at the exact same time."""
    # Already a bare basename (with extension), no directory prefix to strip -- Chronicle's own
    # FileIdentityJson.GetKnownFileName computed this server-side (splitting on both '\' and '/'
    # itself, since its own fileScanner.filePaths entries are Windows-style paths), specifically
    # so this addon never has to re-derive a basename from a raw path. Passing the raw path
    # through posixpath.basename() here instead would have been a no-op on a '\'-separated
    # Windows path (posixpath only splits on '/'), silently defeating find_movie_location()'s
    # own known_filename fast path for every claimed movie.
    known_filename = claim.get('knownFileName')

    folder, _video_basename, full_filename, _discovered_via_fallback, kodi_movie_id = \
        find_movie_location(claim.get('name'), claim.get('year'), known_filename=known_filename)

    if kodi_movie_id is None:
        log.info('nfo_rebuild: "{0}" -- not found in this device\'s own VideoLibrary (wrong '
                 'device for this file, or not scanned in yet) -- releasing queue item {1} for '
                 'another device'.format(label, queue_item_id))
        client.release_rebuild_item(queue_item_id)
        return False

    set_name = _get_movie_set_name(kodi_movie_id)
    file_path = (folder + full_filename) if (folder and full_filename) else None
    stash_key = strip_video_ext(full_filename) if full_filename else None
    expected_nfo = _expected_movie_nfo_path(folder, file_path) if file_path else None

    if folder:
        with io_lock:
            deleted_nfo, deleted_movieset = _delete_local_metadata(folder, stash_key, set_name)
        with state_lock:
            counters['nfo_deleted'] += deleted_nfo
            counters['movieset_deleted'] += deleted_movieset

    refresh_ok = _refresh_movie(kodi_movie_id)
    finish_item(label, expected_nfo, refresh_ok, queue_item_id)
    return True


def _process_show_claim(client, claim, label, queue_item_id, finish_item, counters, state_lock, io_lock):
    """Sibling of _process_movie_claim for a show's own tvshow.nfo. Returns True/False with
    the same meaning; state_lock/io_lock have the same purpose as in _process_movie_claim."""
    folder, tvshowid = find_show_location(claim.get('name'), claim.get('year'))

    if tvshowid is None:
        log.info('nfo_rebuild: "{0}" -- show not found in this device\'s own VideoLibrary -- '
                 'releasing queue item {1} for another device'.format(label, queue_item_id))
        client.release_rebuild_item(queue_item_id)
        return False

    stash_key = posixpath.basename(folder.rstrip('/')) if folder else None
    if folder:
        with io_lock:
            deleted = _delete_show_nfo(folder, stash_key)
        if deleted:
            with state_lock:
                counters['nfo_deleted'] += 1

    refresh_ok = _refresh_show(tvshowid)
    expected_nfo = (folder + 'tvshow.nfo') if folder else None
    finish_item(label, expected_nfo, refresh_ok, queue_item_id)
    return True


def _process_episode_claim(client, claim, label, queue_item_id, finish_item, counters, state_lock, io_lock):
    """Sibling of _process_movie_claim for one episode's own per-file NFO. Returns True/False
    with the same meaning -- released if EITHER the parent show or this exact (season, episode)
    can't be found in this device's own VideoLibrary. state_lock/io_lock have the same purpose
    as in _process_movie_claim."""
    _show_folder, tvshowid = find_show_location(claim.get('showName'), claim.get('showYear'))
    if tvshowid is None:
        log.info('nfo_rebuild: "{0}" -- parent show not found in this device\'s own '
                 'VideoLibrary -- releasing queue item {1} for another device'.format(
                 label, queue_item_id))
        client.release_rebuild_item(queue_item_id)
        return False

    file_path, _streamdetails, kodi_episode_id = get_episode(tvshowid, claim.get('season'), claim.get('episode'))
    if kodi_episode_id is None:
        log.info('nfo_rebuild: "{0}" -- episode not found under tvshowid={1} in this device\'s '
                 'own VideoLibrary -- releasing queue item {2} for another device'.format(
                 label, tvshowid, queue_item_id))
        client.release_rebuild_item(queue_item_id)
        return False

    folder = posixpath.dirname(file_path) + '/' if file_path else None
    stash_key = strip_video_ext(posixpath.basename(file_path)) if file_path else None
    if folder and stash_key:
        with io_lock:
            deleted = _delete_episode_nfo(folder, stash_key)
        if deleted:
            with state_lock:
                counters['nfo_deleted'] += 1

    refresh_ok = _refresh_episode(kodi_episode_id)
    if refresh_ok and file_path:
        # Same pre-refresh path stash as before the 2026-09-06 rework -- see
        # episode_path_cache.py's module docstring for why get_episode_details()'s own live
        # VideoLibrary lookup can't be trusted to find this episode while its own
        # RefreshEpisode call is still in flight.
        episode_path_cache.save(tvshowid, claim.get('season'), claim.get('episode'), file_path)

    expected_nfo = _expected_movie_nfo_path(folder, file_path) if (folder and file_path) else None
    finish_item(label, expected_nfo, refresh_ok, queue_item_id)
    return True


def _get_movie_set_name(kodi_movie_id):
    """One extra VideoLibrary.GetMovieDetails call for the 'set' property -- the old
    full-library walk got this for free from its own VideoLibrary.GetMovies properties list;
    resolving a single claimed movie by title/year doesn't, so this fills the same gap. Needed
    only so _delete_local_metadata knows which dedicated set folder a movieset-* file's
    salvaged content belongs to (see preserve_local_movieset_file) -- best-effort, None on any
    failure just means a movieset-* file gets deleted without an attempted salvage, same as
    when a movie genuinely has no set."""
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovieDetails',
        'params': {'movieid': kodi_movie_id, 'properties': ['set']},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        return response.get('result', {}).get('moviedetails', {}).get('set') or None
    except Exception as exc:
        log.warning('Could not fetch set name for movieid {0}: {1}'.format(kodi_movie_id, exc))
        return None


def _expected_movie_nfo_path(folder, file_path):
    """Best-effort prediction of where sync_movie_nfo()/sync_episode_nfo() will write this
    item's NFO -- same naming rule both use: the real video file's own basename with a .nfo
    extension. Despite the name, this applies equally to episodes -- they use the identical
    convention."""
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
    if any -- needed to know which dedicated set folder a movieset-* file's
    data belongs to.

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
