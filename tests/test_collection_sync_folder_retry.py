# -*- coding: utf-8 -*-
"""Tests for lib/collection_sync.py's folder-access retry (reported on Shield:
"not able to access the collection folder registered in Kodi").

Android's Kodi SMB/network VFS client routinely drops and reconnects its
session (Wi-Fi doze, the NAS still waking up, a brief DHCP/DNS blip) -- a
transient hiccup that normally clears within a second or two. Before this
fix, a single failed mkdirs()/write() against the movie-sets folder was
immediately reported as "folder not reachable"; _mkdirs_with_retry/
_write_with_retry give a reconnect a few short retries before concluding
the folder is actually gone.
"""

import sys


def _import_collection_sync():
    sys.path.insert(0, '.')
    import lib.collection_sync as collection_sync
    return collection_sync


# ── _mkdirs_with_retry ──────────────────────────────────────────────────────

def test_mkdirs_with_retry_succeeds_immediately_when_mkdirs_succeeds(kodi):
    collection_sync = _import_collection_sync()
    collection_sync.xbmcvfs.mkdirs = lambda folder: True
    sleeps = []
    collection_sync.time.sleep = lambda s: sleeps.append(s)

    assert collection_sync._mkdirs_with_retry('smb://nas/Movie Collections/Alien/') is True
    assert sleeps == []


def test_mkdirs_with_retry_recovers_from_a_transient_failure(kodi):
    """The exact scenario reported on the Shield: mkdirs() fails once (a
    momentary SMB reconnect) then succeeds -- must not be reported as
    unreachable."""
    collection_sync = _import_collection_sync()
    calls = {'n': 0}

    def flaky_mkdirs(folder):
        calls['n'] += 1
        return calls['n'] >= 2

    collection_sync.xbmcvfs.mkdirs = flaky_mkdirs
    sleeps = []
    collection_sync.time.sleep = lambda s: sleeps.append(s)

    assert collection_sync._mkdirs_with_retry('smb://nas/Movie Collections/Alien/') is True
    assert calls['n'] == 2
    assert len(sleeps) == 1


def test_mkdirs_with_retry_gives_up_after_max_attempts(kodi):
    collection_sync = _import_collection_sync()
    collection_sync.xbmcvfs.mkdirs = lambda folder: False
    sleeps = []
    collection_sync.time.sleep = lambda s: sleeps.append(s)

    assert collection_sync._mkdirs_with_retry('smb://nas/Movie Collections/Alien/') is False
    assert len(sleeps) == collection_sync._FOLDER_ACCESS_RETRY_ATTEMPTS - 1


# ── _write_with_retry ───────────────────────────────────────────────────────

def test_write_with_retry_recovers_from_transient_write_failure(kodi):
    collection_sync = _import_collection_sync()
    calls = {'n': 0}

    def flaky_write(dest, url):
        calls['n'] += 1
        return 'ok' if calls['n'] >= 2 else 'write_failed'

    collection_sync.write_remote_file = flaky_write
    sleeps = []
    collection_sync.time.sleep = lambda s: sleeps.append(s)

    result = collection_sync._write_with_retry(
        'smb://nas/Movie Collections/Alien/poster.jpg', 'http://x/p.jpg', 'test')

    assert result == 'ok'
    assert calls['n'] == 2
    assert len(sleeps) == 1


def test_write_with_retry_does_not_retry_download_failures(kodi):
    """A download_failed says nothing about the destination folder's own
    reachability -- must return immediately without retrying or blaming the
    folder."""
    collection_sync = _import_collection_sync()
    calls = {'n': 0}

    def always_download_failed(dest, url):
        calls['n'] += 1
        return 'download_failed'

    collection_sync.write_remote_file = always_download_failed
    sleeps = []
    collection_sync.time.sleep = lambda s: sleeps.append(s)

    result = collection_sync._write_with_retry(
        'smb://nas/Movie Collections/Alien/poster.jpg', 'http://x/p.jpg', 'test')

    assert result == 'download_failed'
    assert calls['n'] == 1
    assert sleeps == []


def test_write_with_retry_gives_up_after_max_attempts_all_write_failed(kodi):
    collection_sync = _import_collection_sync()
    calls = {'n': 0}

    def always_write_failed(dest, url):
        calls['n'] += 1
        return 'write_failed'

    collection_sync.write_remote_file = always_write_failed
    sleeps = []
    collection_sync.time.sleep = lambda s: sleeps.append(s)

    result = collection_sync._write_with_retry(
        'smb://nas/Movie Collections/Alien/poster.jpg', 'http://x/p.jpg', 'test')

    assert result == 'write_failed'
    assert calls['n'] == collection_sync._FOLDER_ACCESS_RETRY_ATTEMPTS
    assert len(sleeps) == collection_sync._FOLDER_ACCESS_RETRY_ATTEMPTS - 1
