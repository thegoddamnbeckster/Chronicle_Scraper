# -*- coding: utf-8 -*-
"""Finds and removes "orphaned" episode rows in THIS device's own Kodi video database --
episode rows whose idShow points at a TV show that no longer exists.

Root-caused live (2026-09-12): VideoLibrary.RemoveTVShow (and even Kodi's own "reset content
type + rescan" UI flow) never actually deletes a show's `episode`/`files` rows -- it only
unlinks them from the `tvshow` row, leaving the `episode` row behind with idShow pointing at
nothing. Kodi's own library scanner treats "a `files` row already exists for this exact
filename" as "already known, don't ask the scraper about it again" -- regardless of whether the
owning `episode` row is a live, linked one or one of these orphans. Once a file's episode row
gets orphaned this way, that exact file can never be rescanned into Kodi again through any means
in Kodi's own UI or API -- not Remove-and-rescan, not VideoLibrary.Clean, nothing. Only a direct
database DELETE of the orphaned rows fixes it, permanently, for that file.

Confirmed and fixed live on a real device the same day: 1,048 such orphaned rows found and
removed across dozens of shows on one Shield, with every affected show reaching its full real
episode count the instant a rescan ran afterward (previously capped indefinitely, in some cases
for months, regardless of how many times the show had been removed and rescanned).

## Design constraints (read before changing anything here)

This module writes directly to Kodi's own live SQLite video database from a second, independent
connection, while Kodi itself keeps its own connection to the same file open essentially the
entire time it runs. Every function here is written under these non-negotiable rules:

- **Never guess.** If the located database file can't be confirmed to be the one Kodi is
  actually using right now (schema mismatch, MySQL-backed library, live JSON-RPC counts that
  don't match the file's own counts, more than one candidate file found), abort with no write of
  any kind. A destructive feature must be certain, not merely probable.
- **Backup before any write, verified before it's trusted.** Uses SQLite's own online backup API
  (`sqlite3.Connection.backup`), not a raw file copy -- a plain filesystem copy of a file Kodi
  may be actively writing can produce a torn snapshot, and would silently drop data if the
  install ever uses WAL journal mode (a copy of just the `.db` file, without its `-wal`/`-shm`
  sidecars, misses anything not yet checkpointed). The backup API handles this correctly
  regardless of journal mode. The backup itself is then verified (`PRAGMA integrity_check`) --
  never assumed good just because the copy call returned.
- **Every destructive operation is a single transaction**, opened with `BEGIN IMMEDIATE` (fail
  fast rather than partway through), re-checks Kodi isn't scanning immediately before COMMIT,
  and rolls back cleanly on any unexpected error.
- **Chunk every `IN (...)` list.** SQLite's host-parameter limit can be as low as 999 on some
  builds -- a real repair run hit 1,048 affected rows on one device, so this is a proven
  necessity, not a hypothetical.
- **No `xbmcgui` dialog calls anywhere in this module.** Every function here does work and
  returns data (or raises `LibraryRepairError` with an already-human-readable `.user_message`);
  `default.py` owns 100% of when and how the user is asked or told anything. This keeps the
  module unit-testable without stubbing dialogs.

## Why this duplicates lib/kodi_scan_signal.py's db-path lookup instead of importing it

`kodi_scan_signal.py` already does this same class of read-only lookup (`_find_video_db_path()`,
globbing `special://database/MyVideos*.db`), but it's a *movie-root* file that build.ps1 only
copies into tv_addon/lib/ at build time -- it isn't present in tv_addon/lib/ in source control,
so nothing under tv_addon/tests/ can import it today, and it has no unit tests of its own either.
Promoting it to a real shared module for one single-sided consumer (this feature has no
movie-addon counterpart yet) would invert the point of that sync mechanism, which exists because
*both* addons already use a file, not to manufacture a shared one for a single consumer. If
movie-side support is ever added, that's the point to promote a real shared helper -- not before.

## Scope

TV episode orphans only. Movies are deliberately out of scope: the `movie` table has no parent
FK to dangle in the first place, so it's unverified whether an analogous orphan pattern even
exists there, and this must not be extended on a guess.
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import time

import xbmc
import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('library_repair')

ADDON = xbmcaddon.Addon()

# Same cross-process-file location family as lib/rebuild_state.py, though this addon (unlike
# the movie/TV NFO-rebuild split) has no cross-addon sharing need -- kept under the shared
# special://temp/chronicle_scraper/ tree purely for consistency with the rest of this codebase.
_LOCK_PATH = 'special://temp/chronicle_scraper/library_repair_active.json'

# Long enough to cover any real repair (even a very large library), short enough that a crashed
# or killed Kodi process doesn't leave this locked forever.
_LOCK_STALE_SECONDS = 600

# Comfortably under SQLite's host-parameter limit (as low as 999 on some builds) for every
# chunked IN (...) list this module builds.
_SQLITE_MAX_VARS = 900

_BACKUP_SUFFIX_RE = re.compile(r'\.repair_backup_\d{8}_\d{6}$')


def _report(progress_callback, message):
    """Calls progress_callback(message) if one was given, swallowing anything it raises -- a
    UI-side callback failure must never abort a real repair in progress. Every high-level entry
    point below accepts an optional progress_callback for exactly this: a plain function taking
    one string, with no xbmcgui dependency here (see this module's own docstring on why --
    default.py owns 100% of the UI, this just tells it what's happening right now)."""
    if progress_callback is None:
        return
    try:
        progress_callback(message)
    except Exception as exc:
        log.warning('_report: progress_callback raised ({0}) -- ignoring'.format(exc))

# Tables this repair unconditionally requires to exist, with the exact columns it touches on
# each -- verified via PRAGMA table_info before anything is trusted, never assumed. Missing any
# of these aborts the whole operation; see _verify_schema().
_REQUIRED_SCHEMA = {
    'tvshow':        ['idShow'],
    'episode':       ['idEpisode', 'idShow', 'idFile'],
    'files':         ['idFile', 'idPath'],
    'path':          ['idPath', 'strPath'],
    'art':           ['media_type', 'media_id'],
    'uniqueid':      ['media_type', 'media_id'],
    'bookmark':      ['idBookmark', 'idFile'],
    'streamdetails': ['idFile'],
}

# Extra dependent tables some Kodi schema versions carry that this module cleans up IF present,
# never required -- so a different schema version than the one this was validated against
# (Kodi 21.2, MyVideos131) can't silently leave a smaller class of orphaned rows behind just
# because it happens to also have one of these. Never aborts if any of these are absent.
_OPTIONAL_MEDIA_TABLES = ['rating', 'tag_link']   # media_type/media_id keyed, same shape as art/uniqueid
_OPTIONAL_FILE_TABLES = ['settings', 'stacktimes']  # idFile keyed, same shape as bookmark/streamdetails

# Tables that can legitimately reference the same idFile as an orphaned episode (never expected
# in practice for a real TV episode file, but checked defensively before ever deleting a `files`
# row -- see _find_shared_file_ids()).
_SHARED_FILE_CHECK_TABLES = ['movie', 'musicvideo']


class LibraryRepairError(Exception):
    """Raised for any precondition failure or unexpected error that must abort before (or
    during) a write. `.reason_code` is a short machine-readable string for tests/logging;
    `.user_message` is already phrased for a Kodi dialog -- default.py shows it directly."""

    def __init__(self, reason_code, user_message):
        super().__init__(user_message)
        self.reason_code = reason_code
        self.user_message = user_message


# ── Locating and validating the database ─────────────────────────────────────────────────────

def find_video_db_path():
    """Locates Kodi's own current video database file. Unlike kodi_scan_signal.py's identical
    lookup (fine to guess there -- a wrong pick only costs one missed scan-signal check), this
    refuses to guess: more than one MyVideos*.db candidate aborts rather than picking the
    highest-versioned one, since a destructive feature must be certain which file is live, not
    merely probable. Returns None (not an error) if none exist at all."""
    db_dir = xbmcvfs.translatePath('special://database/')
    candidates = sorted(glob.glob(os.path.join(db_dir, 'MyVideos*.db')))
    if len(candidates) > 1:
        raise LibraryRepairError(
            'multiple_db_files',
            'Multiple video database files were found on this device -- refusing to guess '
            'which one is active. No changes made.')
    return candidates[0] if candidates else None


def check_preconditions(db_path):
    """Every read-only safety check that must pass before this module will touch the database
    at all. Raises LibraryRepairError with an explanatory message on the first thing that
    doesn't check out; makes no changes of any kind."""
    if _is_scanning():
        raise LibraryRepairError(
            'scanning',
            'A library scan is currently running on this device -- try again once it finishes.')
    if _is_mysql_library():
        raise LibraryRepairError(
            'mysql_library',
            "This device's video library is configured to use a shared MySQL database -- this "
            'repair only works with a local database file. No changes made.')
    _verify_schema(db_path)
    _cross_check_live_counts(db_path)


def _is_scanning():
    """Fails CLOSED (treats an unreadable result as "yes, scanning") -- unlike
    lib/rebuild_state.py's own is_active(), which fails open because a missed rebuild marker
    only costs one skipped NFO write. Here, colliding with an active scan is exactly the failure
    this check exists to prevent, so an inability to tell must be treated as the unsafe case."""
    try:
        return bool(xbmc.getCondVisibility('Library.IsScanning'))
    except Exception as exc:
        log.warning('Could not determine Kodi scan state ({0}) -- treating as scanning, for safety'.format(exc))
        return True


def _is_mysql_library():
    """Best-effort check for a MySQL-backed video library (a real, supported Kodi configuration
    for multi-device shared libraries) via advancedsettings.xml. A secondary defense -- the live
    count cross-check below is the primary one and would also catch this case (a MySQL-backed
    setup's local MyVideosNNN.db file, if one even exists, would never match live counts) -- so
    this fails OPEN (returns False, i.e. "assume not MySQL") on any read error rather than
    blocking a legitimate local-SQLite device over a file it merely couldn't parse."""
    try:
        path = xbmcvfs.translatePath('special://profile/advancedsettings.xml')
        if not path or not os.path.exists(path):
            return False
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as exc:
        log.warning("Could not check advancedsettings.xml for a MySQL video database config: {0}".format(exc))
        return False
    return bool(re.search(
        r'<videodatabase>.*?<type>\s*mysql\s*</type>', content, re.IGNORECASE | re.DOTALL))


def _verify_schema(db_path):
    try:
        conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
    except Exception as exc:
        raise LibraryRepairError(
            'db_unreachable', "Couldn't open the Kodi library database: {0}. No changes made.".format(exc))
    try:
        for table, cols in _REQUIRED_SCHEMA.items():
            if not _table_has_columns(conn, table, cols):
                raise LibraryRepairError(
                    'schema_mismatch',
                    'This database has an unsupported schema (missing or incompatible "{0}" table) -- '
                    'no changes made. Schema version: {1}.'.format(table, _schema_version(conn)))
    finally:
        conn.close()


def _table_has_columns(conn, table, required_cols):
    try:
        rows = conn.execute('PRAGMA table_info({0})'.format(table)).fetchall()
    except Exception:
        return False
    if not rows:
        return False
    existing = {row[1] for row in rows}
    return all(col in existing for col in required_cols)


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _schema_version(conn):
    try:
        row = conn.execute('SELECT idVersion FROM version').fetchone()
        return row[0] if row else 'unknown'
    except Exception:
        return 'unknown'


def _cross_check_live_counts(db_path):
    """The single strongest defense against operating on a stale or wrong database file
    (leftover from a schema upgrade, wrong profile, a copy left over from migrating to MySQL and
    back) -- compares the candidate file's own counts against what Kodi itself is reporting
    RIGHT NOW over local JSON-RPC. A real mismatch means this file, however plausible it looked,
    is not the one actually backing Kodi's live library."""
    conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
    try:
        db_shows = conn.execute('SELECT COUNT(*) FROM tvshow').fetchone()[0]
    finally:
        conn.close()

    live_shows = _live_show_count()
    if live_shows is None:
        raise LibraryRepairError(
            'live_check_failed',
            "Couldn't confirm this database file matches Kodi's active library -- no changes made.")

    if live_shows == 0 and db_shows > 0:
        raise LibraryRepairError(
            'live_count_mismatch',
            "Kodi currently reports 0 TV shows, but the database file found on disk has {0} -- "
            "refusing to guess which is right. No changes made.".format(db_shows))
    if db_shows > 0 and abs(live_shows - db_shows) > max(5, live_shows * 0.5):
        raise LibraryRepairError(
            'live_count_mismatch',
            "Kodi's active library ({0} shows) doesn't match the database file found on disk "
            "({1} shows) -- no changes made.".format(live_shows, db_shows))


def _live_show_count():
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetTVShows', 'params': {'properties': []}}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't get live TV show count: {0}".format(exc))
        return None
    if 'error' in response:
        log.warning('VideoLibrary.GetTVShows rejected: {0}'.format(response['error']))
        return None
    return len(response.get('result', {}).get('tvshows') or [])


# ── Detection (read-only) ─────────────────────────────────────────────────────────────────────

def detect_orphans(db_path):
    """Read-only pass, on a `mode=ro` connection so it can never contend for a write lock.
    Returns an OrphanReport dict: episode_ids/file_ids to act on, and a `groups` list for a
    human-readable summary. Groups by the dangling idShow value with example file paths rather
    than a show title -- once the owning tvshow row is gone, there IS no title left to show."""
    conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
    try:
        rows = conn.execute('''
            SELECT e.idEpisode, e.idFile, e.idShow, f.idPath, f.strFilename
            FROM episode e
            JOIN files f ON e.idFile = f.idFile
            LEFT JOIN tvshow t ON e.idShow = t.idShow
            WHERE t.idShow IS NULL
        ''').fetchall()

        path_cache = {}
        groups = {}
        for id_episode, id_file, id_show, id_path, filename in rows:
            if id_path not in path_cache:
                path_row = conn.execute(
                    'SELECT strPath FROM path WHERE idPath=?', (id_path,)).fetchone()
                path_cache[id_path] = path_row[0] if path_row else ''
            group = groups.setdefault(id_show, {
                'former_idShow': id_show, 'episode_count': 0, 'example_paths': [],
            })
            group['episode_count'] += 1
            if len(group['example_paths']) < 3:
                group['example_paths'].append((path_cache[id_path] or '') + (filename or ''))
    finally:
        conn.close()

    episode_ids = [row[0] for row in rows]
    file_ids = sorted({row[1] for row in rows})
    groups_list = sorted(groups.values(), key=lambda g: -g['episode_count'])

    return {
        'episode_ids': episode_ids,
        'file_ids': file_ids,
        'total_episodes': len(episode_ids),
        'groups': groups_list,
    }


# ── Stale season-folder detection (a second, unrelated bug with the same symptom) ──────────────
#
# Root-caused live (2026-09-13): a show can get stuck with missing/zero episodes for a reason
# that has nothing to do with orphaned rows above -- Kodi caches each season's folder path in its
# `path` table, and if that folder is ever renamed on disk (e.g. "Season 1" -> "Season 01"), Kodi
# keeps expecting the OLD name forever and silently skips the show on every future scan ("Process
# directory '...' does not exist - skipping scan" in kodi.log), never erroring, never fixing
# itself. Confirmed: only removing the show and rescanning its real folder from scratch clears it.
# A directory-scoped rescan right after removal was tried and found unreliable (removing a show
# can also clear the path-table link a scoped scan needs), so the fix here is a plain full-library
# scan, same as a user picking "Update library" from Kodi's own menu.

def detect_stale_shows(db_path):
    """Read-only: flags shows where Kodi's own recorded season folder path no longer exists on
    disk. Compares the `path` table (what Kodi expects) against a live Files.GetDirectory listing
    (what's actually there) -- never guesses from episode counts alone, since a legitimately
    not-yet-aired show also has zero episodes. Returns a list of {tvshowid, name} dicts."""
    conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
    try:
        if not _table_exists(conn, 'tvshowlinkpath'):
            log.warning('tvshowlinkpath table not found -- skipping stale-season-folder detection '
                        'on this schema version')
            return []
        try:
            shows = conn.execute('''
                SELECT t.idShow, t.c00, p.idPath, p.strPath
                FROM tvshow t
                JOIN tvshowlinkpath lp ON lp.idShow = t.idShow
                JOIN path p ON p.idPath = lp.idPath
            ''').fetchall()
        except sqlite3.OperationalError as exc:
            # This is a second, independent detection pass layered on top of the established
            # orphan-row repair -- a schema variant this pass doesn't understand must never take
            # down orphan detection too. Fail closed locally (skip this pass, keep going).
            log.warning('detect_stale_shows: query failed on this schema ({0}) -- skipping'.format(exc))
            return []

        seen_ids = set()
        stale = []
        for id_show, name, id_path, root_path in shows:
            if id_show in seen_ids:
                continue  # a show linked to more than one root path is still only ONE show to repair
            try:
                # Root-caused live (2026-09-22): `path WHERE idParentPath=?` returns EVERY child
                # path Kodi has ever recorded under the show's root -- not just season folders.
                # A real show's root also contains Kodi's own housekeeping folders (.actors/,
                # extrafanart/, confirmed present live on a real device sitting right alongside
                # the real season folders), and any of those can independently go missing/
                # inconsistent in a live directory listing without the season folders themselves
                # having moved at all -- false-flagging the ENTIRE show as stale. Confirmed live:
                # this was flagging ~117 of ~130 shows across two independent devices, one of
                # which had never had any repair run on it, and a from-scratch reproduction
                # using each show's own ACTUAL episode file paths (bypassing this table
                # entirely) found zero genuinely stale shows in the same library. Scoping to
                # paths that actually own an episode file -- the same signal that from-scratch
                # reproduction used -- is what those non-season folders can never satisfy.
                season_paths = conn.execute('''
                    SELECT DISTINCT p2.strPath
                    FROM episode e
                    JOIN files f ON f.idFile = e.idFile
                    JOIN path p2 ON p2.idPath = f.idPath
                    WHERE e.idShow = ?
                ''', (id_show,)).fetchall()
            except sqlite3.OperationalError as exc:
                log.warning('detect_stale_shows: could not read season paths for show {0} ({1})'.format(
                    id_show, exc))
                continue
            if not season_paths:
                continue
            real_paths = _list_real_subfolders(root_path)
            if real_paths is None:
                continue  # couldn't browse it right now (e.g. share offline) -- skip, don't guess
            if any(sp[0] not in real_paths for sp in season_paths):
                stale.append({'tvshowid': id_show, 'name': name})
                seen_ids.add(id_show)
    finally:
        conn.close()
    return stale


# ── Stuck-file detection (a THIRD, unrelated bug with the same "capped episode count" symptom) ─
#
# Root-caused live (2026-09-21): a show can get stuck with missing episodes for a reason neither
# of the two passes above can ever detect -- detect_orphans() above only ever finds an `episode`
# row whose OWN show is gone, and detect_stale_shows() only ever finds a show whose recorded
# season folder no longer matches disk. Both require an `episode` row (or a `tvshow` row) to
# already exist. This is the case where neither does: a scan registers a file (inserting its
# `files` row) but never gets as far as creating the matching `episode` row for it -- e.g. an
# addon-side error partway through that one file's scrape. This module's own top docstring
# already explains why that's permanent: Kodi's scanner treats "a `files` row already exists for
# this exact filename" as "already known, don't ask the scraper about it again", regardless of
# whether an episode row actually exists for it. Confirmed live: "Lanterns" S01E04-E06 stuck
# exactly this way while its own tvshow row, and its already-scanned S01E01-E03, were completely
# intact -- the show never showed up as broken or missing anywhere in Kodi's own UI, it just
# silently never grew past 3 episodes no matter how many times it was rescanned.

def detect_stuck_files(db_path):
    """Read-only: finds `files` rows with NO matching `episode` row at all, scoped to season
    folders belonging to a show THIS addon scraped (via tvshowlinkpath, the same linkage
    detect_stale_shows() above already relies on) -- never touches a file under some other
    scraper/content type. Returns a StuckFilesReport dict: file_ids to act on, and a
    human-readable `groups` list keyed by the OWNING show's current name (unlike
    detect_orphans()'s groups, there IS a live show to name here)."""
    conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
    try:
        if not _table_exists(conn, 'tvshowlinkpath'):
            log.warning('tvshowlinkpath table not found -- skipping stuck-file detection on '
                        'this schema version')
            return {'file_ids': [], 'groups': []}

        own_id = ADDON.getAddonInfo('id')
        try:
            show_roots = conn.execute('''
                SELECT DISTINCT t.idShow, t.c00, p.idPath
                FROM tvshow t
                JOIN tvshowlinkpath lp ON lp.idShow = t.idShow
                JOIN path p ON p.idPath = lp.idPath
                WHERE p.strScraper = ?
            ''', (own_id,)).fetchall()
        except sqlite3.OperationalError as exc:
            # Same defensive posture as detect_stale_shows() -- a schema variant this query
            # doesn't understand must never take down the other two passes too.
            log.warning('detect_stuck_files: query failed on this schema ({0}) -- skipping'.format(exc))
            return {'file_ids': [], 'groups': []}

        rows = []
        for id_show, show_name, root_path_id in show_roots:
            # Episodes normally live in season-level subfolders of the show's own root path, but
            # a flat/single-season show can also have its files directly under the root -- check
            # both, the same way an episode's own idSeason isn't assumed here either.
            season_path_ids = [row[0] for row in conn.execute(
                'SELECT idPath FROM path WHERE idParentPath = ?', (root_path_id,)).fetchall()]
            candidate_path_ids = season_path_ids + [root_path_id]
            placeholders = ','.join('?' * len(candidate_path_ids))
            stuck = conn.execute('''
                SELECT f.idFile, f.strFilename
                FROM files f
                LEFT JOIN episode e ON e.idFile = f.idFile
                WHERE e.idFile IS NULL AND f.idPath IN ({0})
            '''.format(placeholders), candidate_path_ids).fetchall()
            for id_file, filename in stuck:
                rows.append((id_file, filename, show_name))
    finally:
        conn.close()

    file_ids = sorted({r[0] for r in rows})
    groups = {}
    for id_file, filename, show_name in rows:
        group = groups.setdefault(show_name, {'show_name': show_name, 'file_count': 0, 'example_files': []})
        group['file_count'] += 1
        if len(group['example_files']) < 3:
            group['example_files'].append(filename or '')
    groups_list = sorted(groups.values(), key=lambda g: -g['file_count'])

    return {'file_ids': file_ids, 'groups': groups_list}


def _list_real_subfolders(root_path):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'Files.GetDirectory',
        'params': {'directory': root_path, 'media': 'video'},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("Couldn't browse {0}: {1}".format(root_path, exc))
        return None
    if 'error' in response:
        return None
    files = response.get('result', {}).get('files') or []
    return {f['file'] for f in files if f.get('filetype') == 'directory'}


def repair_stale_shows(stale_shows, db_path):
    """Removes each stale show, then triggers a scan of this addon's own configured source
    folder(s) so Kodi re-derives every real season folder name from scratch. Resets
    watched/resume/rating status for each show removed this way (a fresh scan assigns a new
    internal show id) -- an accepted tradeoff, since Chronicle's own sync restores it. Returns
    the list of show names actually removed.

    Root-caused live (2026-09-18): this used to fire an UNSCOPED VideoLibrary.Scan (params={}) --
    scanning every configured video source regardless of content type, not just this addon's own.
    That's the exact anti-pattern own_source_directories()/trigger_scan() (below, and
    lib/kodi_scan_signal.py's identical logic) already exist to avoid: VideoLibrary.Scan finishing
    fires Kodi's global onScanFinished(video) event, which every other addon's own Monitor reacts
    to independently (confirmed live: Chronicle_Scrobbler's own throttled VideoLibrary.Clean, and
    SIMKL Scrobbler's own full sync pass, both re-fire on every single occurrence). Worse per this
    module's OWN docstring: VideoLibrary.RemoveTVShow just above never actually deletes a show's
    episode/files rows, only unlinks them -- so every removed show here is a fresh batch of the
    exact orphaned rows this whole module exists to clean up, which the NEXT orphan-detection pass
    (not this one -- see this function's own caller) picks up. Scoping the scan doesn't fix that
    orphan-recreation tradeoff, only the needless whole-library-scan blast radius; the orphan
    tradeoff is inherent to VideoLibrary.RemoveTVShow itself and is called out to the user in the
    repair confirmation dialog (see default.py) rather than silently hidden."""
    if not stale_shows:
        return []
    if _is_scanning():
        log.warning('repair_stale_shows: a scan started -- skipping this step for now')
        return []

    removed = []
    for show in stale_shows:
        request = {
            'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.RemoveTVShow',
            'params': {'tvshowid': show['tvshowid']},
        }
        try:
            response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        except Exception as exc:
            log.warning('RemoveTVShow({0}) failed: {1}'.format(show['tvshowid'], exc))
            continue
        if 'error' in response:
            log.warning('RemoveTVShow({0}) rejected: {1}'.format(show['tvshowid'], response['error']))
            continue
        removed.append(show['name'])

    if removed:
        trigger_scan(own_source_directories(db_path))

    return removed


# ── Backup ────────────────────────────────────────────────────────────────────────────────────

def _backup_dir():
    return xbmcvfs.translatePath(ADDON.getAddonInfo('profile') + 'library_repair_backups/')


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def _check_free_space(db_path, backup_dir):
    _ensure_dir(backup_dir)
    needed = os.path.getsize(db_path)
    try:
        free = shutil.disk_usage(backup_dir).free
    except Exception as exc:
        log.warning("Couldn't check free disk space ({0}) -- proceeding anyway".format(exc))
        return
    if free < needed:
        raise LibraryRepairError(
            'insufficient_space',
            'Not enough free space to safely back up the library database ({0} MB needed). '
            'No changes made.'.format(needed // (1024 * 1024)))


def create_backup(db_path, backup_dir):
    """Uses SQLite's own online backup API (not a raw file copy -- see this module's own
    docstring for why) and verifies the result before returning. Raises LibraryRepairError,
    with the live database completely untouched, on any failure at any step."""
    _check_free_space(db_path, backup_dir)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, '{0}.repair_backup_{1}'.format(
        os.path.basename(db_path), timestamp))

    source = dest = None
    try:
        source = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
        dest = sqlite3.connect(backup_path)
        source.backup(dest)
    except Exception as exc:
        raise LibraryRepairError(
            'backup_failed', "Couldn't create a safety backup ({0}) -- no changes made.".format(exc))
    finally:
        if dest is not None:
            dest.close()
        if source is not None:
            source.close()

    _verify_backup(backup_path)
    return backup_path


def _verify_backup(backup_path):
    """Checks the BACKUP's own internal consistency (integrity_check) -- not a byte comparison
    against the live source, which isn't meaningful here: the source can keep changing after the
    backup completes, so a hash comparison would be flaky by construction, not a real check."""
    try:
        conn = sqlite3.connect('file:{0}?mode=ro'.format(backup_path), uri=True, timeout=5)
    except Exception as exc:
        raise LibraryRepairError(
            'backup_verify_failed', "Couldn't verify the safety backup ({0}) -- no changes made.".format(exc))
    try:
        result = conn.execute('PRAGMA integrity_check').fetchone()
        if not result or result[0] != 'ok':
            raise LibraryRepairError(
                'backup_corrupt', 'The safety backup failed an integrity check -- no changes made.')
    finally:
        conn.close()


def prune_old_backups(backup_dir, keep=5):
    """Only ever called AFTER a new backup is created and verified -- pruning first would risk a
    zero-backup window if the new one then failed. Oldest-first, never drops below `keep`."""
    candidates = sorted(
        p for p in glob.glob(os.path.join(backup_dir, '*'))
        if _BACKUP_SUFFIX_RE.search(p)
    )
    removed = []
    while len(candidates) > keep:
        oldest = candidates.pop(0)
        try:
            os.remove(oldest)
            removed.append(oldest)
        except Exception as exc:
            log.warning("Couldn't remove old backup {0}: {1}".format(oldest, exc))
    return removed


# ── Repair (the actual delete pass) ──────────────────────────────────────────────────────────

def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _find_shared_file_ids(conn, file_ids):
    """idFile values also referenced by a movie/musicvideo row -- never expected for a real TV
    episode file, but checked before ever deleting a `files` row so a genuinely bizarre schema
    state can't have this module remove a file another item still legitimately needs."""
    shared = set()
    for table in _SHARED_FILE_CHECK_TABLES:
        if not _table_exists(conn, table):
            continue
        for chunk in _chunks(file_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            rows = conn.execute(
                'SELECT idFile FROM {0} WHERE idFile IN ({1})'.format(table, placeholders), chunk
            ).fetchall()
            shared.update(row[0] for row in rows)
    return shared


def run_repair(db_path, orphan_report, backup_dir, progress_callback=None):
    """The actual repair: mandatory verified backup, then a single transaction deleting exactly
    the rows named in orphan_report (plus their dependents), then a manifest for Undo Last
    Repair. Returns a RepairResult dict; never raises for a locked-database or scan-collision
    abort (those are reported via result['aborted']/['abort_reason']) -- only for something that
    happened before any write was attempted (backup failure) does this raise LibraryRepairError,
    since nothing has changed yet in that case either way. progress_callback: see _report()'s own
    doc -- called before the two slowest steps here (the backup, then the delete transaction)."""
    episode_ids = orphan_report['episode_ids']
    file_ids = orphan_report['file_ids']

    result = {
        'backup_path': None, 'deleted_episodes': 0, 'deleted_files': 0, 'deleted_art': 0,
        'deleted_uniqueid': 0, 'deleted_bookmark': 0, 'deleted_streamdetails': 0,
        'skipped_shared_files': 0, 'aborted': False, 'abort_reason': None,
    }
    if not episode_ids:
        return result

    _report(progress_callback, 'Backing up your library...')
    backup_path = create_backup(db_path, backup_dir)
    prune_old_backups(backup_dir)
    result['backup_path'] = backup_path

    _report(progress_callback, 'Removing stuck episode entries...')
    conn = None
    for attempt in range(1, 4):
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.isolation_level = None  # manual BEGIN/COMMIT/ROLLBACK below, not sqlite3's own implicit ones
            conn.execute('PRAGMA busy_timeout = 5000')
            conn.execute('BEGIN IMMEDIATE')
            break
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
                conn = None
            if 'locked' in str(exc).lower() and attempt < 3:
                log.warning('Database locked, retrying (attempt {0}/3)'.format(attempt))
                time.sleep(1.0 * attempt)
                continue
            result['aborted'] = True
            result['abort_reason'] = 'database_locked'
            return result

    try:
        shared_ids = _find_shared_file_ids(conn, file_ids)
        result['skipped_shared_files'] = len(shared_ids)
        deletable_file_ids = [f for f in file_ids if f not in shared_ids]

        for chunk in _chunks(episode_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            result['deleted_art'] += conn.execute(
                'DELETE FROM art WHERE media_type=? AND media_id IN ({0})'.format(placeholders),
                ['episode'] + chunk).rowcount
            result['deleted_uniqueid'] += conn.execute(
                'DELETE FROM uniqueid WHERE media_type=? AND media_id IN ({0})'.format(placeholders),
                ['episode'] + chunk).rowcount
            for table in _OPTIONAL_MEDIA_TABLES:
                if _table_exists(conn, table):
                    conn.execute(
                        'DELETE FROM {0} WHERE media_type=? AND media_id IN ({1})'.format(table, placeholders),
                        ['episode'] + chunk)

        for chunk in _chunks(deletable_file_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            result['deleted_bookmark'] += conn.execute(
                'DELETE FROM bookmark WHERE idFile IN ({0})'.format(placeholders), chunk).rowcount
            result['deleted_streamdetails'] += conn.execute(
                'DELETE FROM streamdetails WHERE idFile IN ({0})'.format(placeholders), chunk).rowcount
            for table in _OPTIONAL_FILE_TABLES:
                if _table_exists(conn, table):
                    conn.execute('DELETE FROM {0} WHERE idFile IN ({1})'.format(table, placeholders), chunk)

        for chunk in _chunks(episode_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            result['deleted_episodes'] += conn.execute(
                'DELETE FROM episode WHERE idEpisode IN ({0})'.format(placeholders), chunk).rowcount

        for chunk in _chunks(deletable_file_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            result['deleted_files'] += conn.execute(
                'DELETE FROM files WHERE idFile IN ({0})'.format(placeholders), chunk).rowcount

        if _is_scanning():
            conn.execute('ROLLBACK')
            result['aborted'] = True
            result['abort_reason'] = 'scan_started_during_repair'
            return result

        conn.execute('COMMIT')
    except Exception as exc:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        log.error('run_repair: unexpected error, rolled back: {0}'.format(exc))
        result['aborted'] = True
        result['abort_reason'] = 'unexpected_error'
        return result
    finally:
        conn.close()

    _write_manifest(backup_dir, episode_ids, deletable_file_ids, backup_path)
    return result


def repair_stuck_files(db_path, stuck_report, backup_dir, progress_callback=None):
    """The stuck-files counterpart to run_repair() above, same transactional/backup/chunking
    rules -- but structurally simpler, since a stuck `files` row by definition never had an
    `episode` row (so there's no `episode`/`art`/`uniqueid` data to also clean up or preserve).

    Deliberately NOT covered by Undo Last Repair: unlike a real orphaned episode (which carries
    watched status, custom art, external ids -- genuine data that would be lost forever without
    the manifest/undo system), a stuck file has no episode data to lose. Removing its `files` row
    only clears the "already known" marker blocking Kodi's scanner -- the very next scan simply
    recreates an identical `files` row and, this time, successfully creates the episode alongside
    it. Nothing here is destructive in the way the episode-orphan repair is.
    """
    file_ids = stuck_report.get('file_ids') or []
    result = {
        'backup_path': None, 'deleted_files': 0, 'deleted_bookmark': 0,
        'deleted_streamdetails': 0, 'skipped_shared_files': 0,
        'aborted': False, 'abort_reason': None,
    }
    if not file_ids:
        return result

    _report(progress_callback, 'Backing up your library...')
    backup_path = create_backup(db_path, backup_dir)
    prune_old_backups(backup_dir)
    result['backup_path'] = backup_path

    _report(progress_callback, 'Clearing stuck file entries...')
    conn = None
    for attempt in range(1, 4):
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.isolation_level = None
            conn.execute('PRAGMA busy_timeout = 5000')
            conn.execute('BEGIN IMMEDIATE')
            break
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
                conn = None
            if 'locked' in str(exc).lower() and attempt < 3:
                log.warning('Database locked, retrying (attempt {0}/3)'.format(attempt))
                time.sleep(1.0 * attempt)
                continue
            result['aborted'] = True
            result['abort_reason'] = 'database_locked'
            return result

    try:
        # Same defensive guard as run_repair() -- never expected for a genuinely stuck TV
        # episode file, but checked before ever deleting a `files` row regardless.
        shared_ids = _find_shared_file_ids(conn, file_ids)
        result['skipped_shared_files'] = len(shared_ids)
        deletable_file_ids = [f for f in file_ids if f not in shared_ids]

        for chunk in _chunks(deletable_file_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            result['deleted_bookmark'] += conn.execute(
                'DELETE FROM bookmark WHERE idFile IN ({0})'.format(placeholders), chunk).rowcount
            result['deleted_streamdetails'] += conn.execute(
                'DELETE FROM streamdetails WHERE idFile IN ({0})'.format(placeholders), chunk).rowcount
            for table in _OPTIONAL_FILE_TABLES:
                if _table_exists(conn, table):
                    conn.execute('DELETE FROM {0} WHERE idFile IN ({1})'.format(table, placeholders), chunk)

        for chunk in _chunks(deletable_file_ids, _SQLITE_MAX_VARS):
            placeholders = ','.join('?' * len(chunk))
            result['deleted_files'] += conn.execute(
                'DELETE FROM files WHERE idFile IN ({0})'.format(placeholders), chunk).rowcount

        if _is_scanning():
            conn.execute('ROLLBACK')
            result['aborted'] = True
            result['abort_reason'] = 'scan_started_during_repair'
            return result

        conn.execute('COMMIT')
    except Exception as exc:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        log.error('repair_stuck_files: unexpected error, rolled back: {0}'.format(exc))
        result['aborted'] = True
        result['abort_reason'] = 'unexpected_error'
        return result
    finally:
        conn.close()

    return result


# ── Undo (re-insert exactly what the last repair removed) ──────────────────────────────────────

def _manifest_path(backup_dir):
    return os.path.join(backup_dir, 'last_repair_manifest.json')


def _write_manifest(backup_dir, episode_ids, file_ids, backup_path):
    manifest = {
        'backup_path': backup_path,
        'episode_ids': episode_ids,
        'file_ids': file_ids,
        'created_at': time.time(),
    }
    try:
        with open(_manifest_path(backup_dir), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
    except Exception as exc:
        # The repair itself already committed successfully -- a failure here only means Undo
        # Last Repair won't have anything to act on, not that anything is broken or lost (the
        # backup file itself is untouched by this).
        log.warning("Couldn't write repair manifest ({0}) -- Undo Last Repair won't be available "
                    'for this run, but the repair itself completed normally.'.format(exc))


def peek_last_manifest():
    """Read-only, no lock -- for building the Undo confirmation dialog's summary text before
    committing to anything. Returns None if no repair has been run yet on this device, or its
    manifest/backup is missing."""
    return _read_manifest(_backup_dir())


def _read_manifest(backup_dir):
    path = _manifest_path(backup_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception as exc:
        log.warning("Couldn't read repair manifest ({0})".format(exc))
        return None
    if not os.path.exists(manifest.get('backup_path', '')):
        return None
    return manifest


def _fetch_rows(conn, table, key_col, ids):
    if not ids:
        return []
    conn.row_factory = sqlite3.Row
    rows = []
    for chunk in _chunks(ids, _SQLITE_MAX_VARS):
        placeholders = ','.join('?' * len(chunk))
        rows.extend(conn.execute(
            'SELECT * FROM {0} WHERE {1} IN ({2})'.format(table, key_col, placeholders), chunk
        ).fetchall())
    return [dict(row) for row in rows]


def _fetch_rows_by_media(conn, table, media_type, media_ids):
    if not media_ids:
        return []
    conn.row_factory = sqlite3.Row
    rows = []
    for chunk in _chunks(media_ids, _SQLITE_MAX_VARS):
        placeholders = ','.join('?' * len(chunk))
        rows.extend(conn.execute(
            'SELECT * FROM {0} WHERE media_type=? AND media_id IN ({1})'.format(table, placeholders),
            [media_type] + chunk
        ).fetchall())
    return [dict(row) for row in rows]


def _insert_rows(conn, table, rows):
    """INSERT OR IGNORE -- safe to call twice (e.g. a retried/duplicate Undo) without raising on
    a primary-key collision; a row that already exists is simply left as-is."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = 'INSERT OR IGNORE INTO {0} ({1}) VALUES ({2})'.format(
        table, ','.join(cols), ','.join('?' * len(cols)))
    conn.executemany(sql, [tuple(row[c] for c in cols) for row in rows])
    return len(rows)


def undo_last_repair(db_path, backup_dir):
    """Reverses exactly the most recent repair run, and nothing else: reads the specific rows
    named in its manifest back out of ITS backup file, and re-inserts them into the live
    database. Deliberately NOT a whole-file restore -- see this module's own docstring and the
    design plan for why a file-swap restore is a worse idea than it sounds. Because this is an
    additive re-insert of specifically-known rows, it can't clobber anything legitimate that
    happened to the live library after the repair ran (a new episode added by a scan in between,
    for instance), and it never requires Kodi to stop."""
    manifest = _read_manifest(backup_dir)
    if not manifest:
        raise LibraryRepairError(
            'no_manifest', 'No repair has been run on this device yet, or its backup is missing '
                            '-- there is nothing to undo.')

    backup_path = manifest['backup_path']
    episode_ids = manifest['episode_ids']
    file_ids = manifest['file_ids']

    source = sqlite3.connect('file:{0}?mode=ro'.format(backup_path), uri=True, timeout=5)
    try:
        episodes = _fetch_rows(source, 'episode', 'idEpisode', episode_ids)
        files = _fetch_rows(source, 'files', 'idFile', file_ids)
        art = _fetch_rows_by_media(source, 'art', 'episode', episode_ids)
        uniqueid = _fetch_rows_by_media(source, 'uniqueid', 'episode', episode_ids)
        bookmark = _fetch_rows(source, 'bookmark', 'idFile', file_ids)
        streamdetails = _fetch_rows(source, 'streamdetails', 'idFile', file_ids)
    finally:
        source.close()

    result = {
        'restored_episodes': 0, 'restored_files': 0, 'restored_art': 0, 'restored_uniqueid': 0,
        'restored_bookmark': 0, 'restored_streamdetails': 0, 'aborted': False, 'abort_reason': None,
    }

    conn = None
    for attempt in range(1, 4):
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.isolation_level = None
            conn.execute('PRAGMA busy_timeout = 5000')
            conn.execute('BEGIN IMMEDIATE')
            break
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
                conn = None
            if 'locked' in str(exc).lower() and attempt < 3:
                time.sleep(1.0 * attempt)
                continue
            result['aborted'] = True
            result['abort_reason'] = 'database_locked'
            return result

    try:
        # Parents before children, mirroring run_repair()'s children-before-parents delete order.
        result['restored_files'] = _insert_rows(conn, 'files', files)
        result['restored_episodes'] = _insert_rows(conn, 'episode', episodes)
        result['restored_art'] = _insert_rows(conn, 'art', art)
        result['restored_uniqueid'] = _insert_rows(conn, 'uniqueid', uniqueid)
        result['restored_bookmark'] = _insert_rows(conn, 'bookmark', bookmark)
        result['restored_streamdetails'] = _insert_rows(conn, 'streamdetails', streamdetails)

        if _is_scanning():
            conn.execute('ROLLBACK')
            result['aborted'] = True
            result['abort_reason'] = 'scan_started_during_undo'
            return result

        conn.execute('COMMIT')
    except Exception as exc:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        log.error('undo_last_repair: unexpected error, rolled back: {0}'.format(exc))
        result['aborted'] = True
        result['abort_reason'] = 'unexpected_error'
        return result
    finally:
        conn.close()

    return result


# ── Cross-process lock (one repair/undo at a time on this device) ──────────────────────────────

def _acquire_lock():
    if xbmcvfs.exists(_LOCK_PATH):
        try:
            f = xbmcvfs.File(_LOCK_PATH, 'r')
            try:
                raw = bytes(f.readBytes())
            finally:
                f.close()
            data = json.loads(raw.decode('utf-8'))
            age = time.time() - data.get('started_at', 0)
            if age < _LOCK_STALE_SECONDS:
                raise LibraryRepairError(
                    'already_running',
                    'A library repair is already running on this device -- try again shortly.')
            log.warning('Stale library-repair lock ({0:.0f}s old) -- proceeding anyway'.format(age))
        except LibraryRepairError:
            raise
        except Exception:
            pass  # unreadable lock file -- treat as stale/corrupt, proceed

    folder = _LOCK_PATH.rsplit('/', 1)[0] + '/'
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    f = xbmcvfs.File(_LOCK_PATH, 'w')
    try:
        f.write(bytearray(json.dumps({'started_at': time.time()}).encode('utf-8')))
    finally:
        f.close()


def _release_lock():
    try:
        xbmcvfs.delete(_LOCK_PATH)
    except Exception:
        pass


# ── High-level entry points for default.py (each acquires/releases the lock itself) ────────────

def preview(progress_callback=None):
    """Read-only: locate the db, run every precondition check, detect orphans, stale season
    folders, AND stuck files (three unrelated bugs, same "capped episode count" symptom -- see
    this module's own docs for each). Raises LibraryRepairError on any abort condition; otherwise
    returns an OrphanReport with added 'stale_shows' and 'stuck_files' keys. progress_callback, if
    given, is called with a short status string at each step -- see _report()'s own doc; this can
    genuinely take a few seconds on a large library, and the caller needs something to show for
    that time besides an unresponsive remote click."""
    _acquire_lock()
    try:
        _report(progress_callback, 'Locating your Kodi library database...')
        db_path = find_video_db_path()
        if not db_path:
            raise LibraryRepairError('no_db_found', "Couldn't locate the Kodi library database file.")
        _report(progress_callback, 'Checking library health...')
        check_preconditions(db_path)
        _report(progress_callback, 'Scanning for stuck episodes...')
        report = detect_orphans(db_path)
        _report(progress_callback, 'Checking for missing season folders...')
        report['stale_shows'] = detect_stale_shows(db_path)
        _report(progress_callback, 'Checking for permanently-skipped files...')
        report['stuck_files'] = detect_stuck_files(db_path)
        # Fabricated-watched detection is deliberately NOT run here any more: identical watch
        # times also mark genuinely watched shows (a one-time history sync), so it cannot decide
        # on its own and asking the user to pick from dozens of shows was rejected. The durable
        # answer is Chronicle's rewatch reset (episode/season/show), which stamps a reset time
        # that Kodi devices honour on their next sync. detect_fabricated_watched() and the undo
        # path stay for manifests written by 1.14.8/1.14.9.
        return report
    finally:
        _release_lock()


def prepare_repair(progress_callback=None):
    """First half of the Repair action: locate the db, check preconditions, detect orphans,
    stale season folders, and stuck files -- everything needed to show the user a confirmation
    dialog. Raises LibraryRepairError on any abort condition. On success, the CALLER is
    responsible for calling finish_repair() exactly once afterward (whether the user confirms or
    declines) to release the lock this acquires. progress_callback: see preview()'s own doc --
    identical detection steps."""
    _acquire_lock()
    try:
        _report(progress_callback, 'Locating your Kodi library database...')
        db_path = find_video_db_path()
        if not db_path:
            raise LibraryRepairError('no_db_found', "Couldn't locate the Kodi library database file.")
        _report(progress_callback, 'Checking library health...')
        check_preconditions(db_path)
        _report(progress_callback, 'Scanning for stuck episodes...')
        report = detect_orphans(db_path)
        _report(progress_callback, 'Checking for missing season folders...')
        report['stale_shows'] = detect_stale_shows(db_path)
        _report(progress_callback, 'Checking for permanently-skipped files...')
        report['stuck_files'] = detect_stuck_files(db_path)
        # Fabricated-watched detection is deliberately NOT run here any more: identical watch
        # times also mark genuinely watched shows (a one-time history sync), so it cannot decide
        # on its own and asking the user to pick from dozens of shows was rejected. The durable
        # answer is Chronicle's rewatch reset (episode/season/show), which stamps a reset time
        # that Kodi devices honour on their next sync. detect_fabricated_watched() and the undo
        # path stay for manifests written by 1.14.8/1.14.9.
    except Exception:
        _release_lock()
        raise
    return db_path, report


def finish_repair(db_path, report, execute, progress_callback=None):
    """Second half of the Repair action -- always releases the lock prepare_repair() acquired.
    execute=False (user declined) makes no changes and returns None. execute=True runs the
    orphaned-row repair, THEN removes+rescans any stale-season-folder shows, THEN clears any
    stuck files, and returns a combined RepairResult (adds 'repaired_stale_shows': [name, ...]
    and the stuck-files result's own keys merged in). progress_callback: see preview()'s own doc
    -- this is the long-running half (backup + delete transaction), where ongoing feedback
    matters even more than during detection."""
    try:
        if not execute:
            return None
        result = run_repair(db_path, report, _backup_dir(), progress_callback=progress_callback)
        stale_shows = report.get('stale_shows') or []
        if not result['aborted'] and stale_shows:
            _report(progress_callback, 'Fixing shows with missing season folders...')
        result['repaired_stale_shows'] = (
            [] if result['aborted'] else repair_stale_shows(stale_shows, db_path))

        stuck_files = report.get('stuck_files') or {'file_ids': []}
        if not result['aborted'] and stuck_files.get('file_ids'):
            _report(progress_callback, 'Clearing permanently-skipped files...')
            stuck_result = repair_stuck_files(db_path, stuck_files, _backup_dir(), progress_callback=progress_callback)
            result['deleted_stuck_files'] = stuck_result['deleted_files']
            result['stuck_files_aborted'] = stuck_result['aborted']
        else:
            result['deleted_stuck_files'] = 0
            result['stuck_files_aborted'] = False

        fabricated = report.get('fabricated_watched') or {'episodes': []}
        result['fabricated_watched'] = {'cleared': 0, 'chronicle_reset': 0, 'skipped': 0}
        result['fabricated_watched_error'] = None
        if not result['aborted'] and fabricated.get('episodes'):
            try:
                result['fabricated_watched'] = repair_fabricated_watched(
                    fabricated, _backup_dir(), progress_callback=progress_callback)
            except LibraryRepairError as exc:
                result['fabricated_watched_error'] = exc.user_message
        return result
    finally:
        _release_lock()


def run_undo():
    """The Undo Last Repair action, in one call -- unlike Repair, there's no separate detection
    step to split out (peek_last_manifest() already covers building the confirmation dialog)."""
    _acquire_lock()
    try:
        # With neither manifest present, fall through to undo_last_repair() so it raises its own
        # 'no_manifest' error as it always has.
        if _read_manifest(_backup_dir()) is not None or peek_fabricated_manifest() is None:
            db_path = find_video_db_path()
            if not db_path:
                raise LibraryRepairError('no_db_found', "Couldn't locate the Kodi library database file.")
            check_preconditions(db_path)
            result = undo_last_repair(db_path, _backup_dir())
        else:
            result = {'aborted': False, 'abort_reason': None, 'restored_episodes': 0}
        result['restored_watched'] = undo_fabricated_watched()
        return result
    finally:
        _release_lock()


# ── Fabricated watched status ───────────────────────────────────────────────────────────────

# Fourth pass (added 2026-09-23), unrelated to the database file: episodes Kodi reports as watched
# whose lastplayed is IDENTICAL, to the second, across several episodes of the same show. Real
# playback of even two episodes can't share a timestamp (each takes ~20+ minutes), so a group like
# that is a bulk artifact, not viewing -- confirmed live on several shows the user never watched
# (one had 6 of 9 episodes stamped 2026-09-19 17:10:40, another 17 of 24), most likely left behind
# by the pre-v1.14.4 stale-shows repair bug's remove-and-re-add cycles.
#
# Why this lives here rather than only in Chronicle's own cleanup: watch_rating_sync's
# resolve_watched_direction() treats Kodi's local playcount as authoritative whenever Chronicle
# has nothing more recent, so any cleanup done only on Chronicle's side is undone by the very next
# reconciliation pass (confirmed live, same day: cleaned shows were re-corrupted within minutes).
# The bad data has to be cleared at the source, in Kodi -- AND in Chronicle in the same run, or
# Chronicle's still-watched state would be pushed straight back down into Kodi instead.
#
# Everything here goes through Kodi's own JSON-RPC (VideoLibrary.SetEpisodeDetails) rather than
# writing to the database file: unlike the passes above, nothing here is something JSON-RPC can't
# do, so Kodi itself does the write and none of the backup/schema-verification machinery is
# needed. Undo instead restores from its own small manifest of each episode's prior values.
#
# A group this size CAN also be a genuine "mark season watched" click, which is why this only
# ever acts after the user has seen it in Preview/the confirmation dialog and said yes.

_FABRICATED_WATCHED_MIN_GROUP = 3
_FABRICATED_MANIFEST_NAME = 'fabricated_watched_manifest.json'


def _jsonrpc(method, params):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning('{0} call failed: {1}'.format(method, exc))
        return None
    if 'error' in response:
        log.warning('{0} rejected: {1}'.format(method, response['error']))
        return None
    return response.get('result') or {}


def detect_fabricated_watched():
    """Read-only. Returns {'episodes': [...], 'groups': [...]}: every watched episode that shares
    its exact lastplayed with at least _FABRICATED_WATCHED_MIN_GROUP-1 other watched episodes of
    the same show, and a per-(show, timestamp) summary for the dialogs."""
    shows = (_jsonrpc('VideoLibrary.GetTVShows', {'properties': ['title']}) or {}).get('tvshows') or []
    episodes = []
    groups = []
    for show in shows:
        if show.get('tvshowid') is None:
            continue
        result = _jsonrpc('VideoLibrary.GetEpisodes', {
            'tvshowid': show['tvshowid'],
            'properties': ['title', 'season', 'episode', 'playcount', 'lastplayed', 'file'],
        })
        by_timestamp = {}
        for ep in (result or {}).get('episodes') or []:
            if (ep.get('playcount') or 0) > 0 and ep.get('lastplayed'):
                by_timestamp.setdefault(ep['lastplayed'], []).append(ep)
        for timestamp, group in by_timestamp.items():
            if len(group) < _FABRICATED_WATCHED_MIN_GROUP:
                continue
            groups.append({'show_name': show.get('title'), 'count': len(group), 'lastplayed': timestamp})
            for ep in group:
                episodes.append({
                    'episodeid': ep['episodeid'], 'show_name': show.get('title'),
                    'title': ep.get('title'), 'season': ep.get('season'), 'episode': ep.get('episode'),
                    'playcount': ep.get('playcount'), 'lastplayed': ep.get('lastplayed'),
                    'file': ep.get('file'),
                })
    groups.sort(key=lambda g: -g['count'])
    return {'episodes': episodes, 'groups': groups}


def _fabricated_manifest_path(backup_dir):
    return os.path.join(backup_dir, _FABRICATED_MANIFEST_NAME)


def peek_fabricated_manifest():
    """Read-only -- what Undo would restore, or None if there's nothing to undo."""
    path = _fabricated_manifest_path(_backup_dir())
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Couldn't read fabricated-watched manifest ({0})".format(exc))
        return None


def repair_fabricated_watched(report, backup_dir, client=None, progress_callback=None):
    """Resets each flagged episode to unwatched in Chronicle FIRST, then in Kodi -- in that order
    on purpose: if Chronicle's own reset failed and Kodi's were cleared anyway, the next
    reconciliation would push Chronicle's still-watched state straight back into Kodi. An episode
    whose Chronicle reset fails is left completely untouched and counted in 'skipped' (re-running
    Repair retries it). An episode Chronicle has no record of at all needs no Chronicle reset.

    Writes the undo manifest (prior playcount/lastplayed for every flagged episode) BEFORE
    changing anything, and aborts if that can't be written. Returns
    {'cleared', 'chronicle_reset', 'skipped'}."""
    flagged = report.get('episodes') or []
    result = {'cleared': 0, 'chronicle_reset': 0, 'skipped': 0}
    if not flagged:
        return result

    if _is_scanning():
        raise LibraryRepairError(
            'scanning', 'A library scan is currently running on this device -- try again once it finishes.')

    if client is None:
        from lib.chronicle_client import ChronicleClient
        client = ChronicleClient()
    reachable, message = client.test_connection()
    if not reachable:
        raise LibraryRepairError(
            'chronicle_unreachable',
            "Chronicle can't be reached ({0}) -- this repair resets watched status there too, so "
            'it will not run without it. No changes made.'.format(message))

    _ensure_dir(backup_dir)
    manifest = {
        'created_at': time.time(),
        'episodes': [{'episodeid': e['episodeid'], 'playcount': e['playcount'],
                      'lastplayed': e['lastplayed']} for e in flagged],
    }
    try:
        with open(_fabricated_manifest_path(backup_dir), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
    except Exception as exc:
        raise LibraryRepairError(
            'manifest_write_failed',
            "Couldn't save the undo record ({0}) -- no changes made.".format(exc))

    for index, ep in enumerate(flagged):
        _report(progress_callback, 'Resetting watched status ({0}/{1})...'.format(index + 1, len(flagged)))
        file_path = ep.get('file') or ''
        details = None
        if file_path:
            details = client.get_episode_details_by_file(
                file_path.replace('\\', '/').rsplit('/', 1)[-1],
                season=ep.get('season'), episode=ep.get('episode'))
        media_item_id = (details or {}).get('mediaItemId')
        if media_item_id:
            if not client.reset_watch_progress(media_item_id):
                result['skipped'] += 1
                continue
            result['chronicle_reset'] += 1

        if _jsonrpc('VideoLibrary.SetEpisodeDetails',
                    {'episodeid': ep['episodeid'], 'playcount': 0, 'lastplayed': ''}) is None:
            result['skipped'] += 1
        else:
            result['cleared'] += 1
    return result


def undo_fabricated_watched(backup_dir=None):
    """Restores each episode's prior playcount/lastplayed in Kodi from the manifest the last
    repair wrote. Chronicle's own reset isn't reversed -- the next reconciliation pass simply
    pulls Kodi's restored state back in on its own. Returns {'restored', 'errors'}; removes the
    manifest only when every episode restored."""
    backup_dir = backup_dir or _backup_dir()
    manifest = peek_fabricated_manifest()
    result = {'restored': 0, 'errors': 0}
    if not manifest:
        return result
    for ep in manifest.get('episodes') or []:
        if _jsonrpc('VideoLibrary.SetEpisodeDetails', {
                'episodeid': ep['episodeid'], 'playcount': ep.get('playcount') or 0,
                'lastplayed': ep.get('lastplayed') or ''}) is None:
            result['errors'] += 1
        else:
            result['restored'] += 1
    if not result['errors']:
        try:
            os.remove(_fabricated_manifest_path(backup_dir))
        except OSError as exc:
            log.warning("Couldn't remove fabricated-watched manifest after undo: {0}".format(exc))
    return result


# ── Optional post-repair scan trigger ────────────────────────────────────────────────────────

def own_source_directories(db_path):
    """Same query lib/kodi_scan_signal.py's own _find_own_source_directories() uses, against the
    already-located db_path this module resolved for the repair itself -- restricted to this
    addon's own configured source folder(s), never a blind whole-library scan (see that module's
    own docstring for why: VideoLibrary.Scan finishing cascades into every other addon's own
    onScanFinished handler too)."""
    own_id = ADDON.getAddonInfo('id')
    try:
        conn = sqlite3.connect('file:{0}?mode=ro'.format(db_path), uri=True, timeout=5)
        try:
            rows = conn.execute(
                'SELECT DISTINCT strPath FROM path WHERE strScraper = ? AND idParentPath IS NULL',
                (own_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Couldn't read source directories for the post-repair scan prompt: {0}".format(exc))
        return []
    return [row[0] for row in rows]


def trigger_scan(directories):
    """Same JSON-RPC shape as lib/kodi_scan_signal.py's own scan trigger. Returns True if at
    least one directory's scan was accepted."""
    any_accepted = False
    for directory in directories:
        request = {
            'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.Scan',
            'params': {'directory': directory, 'showdialogs': False},
        }
        try:
            response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        except Exception as exc:
            log.warning('VideoLibrary.Scan({0}) call failed: {1}'.format(directory, exc))
            continue
        if 'error' in response:
            log.warning('VideoLibrary.Scan({0}) rejected: {1}'.format(directory, response['error']))
            continue
        any_accepted = True
    return any_accepted
