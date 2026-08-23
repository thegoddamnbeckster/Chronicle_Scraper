# -*- coding: utf-8 -*-
"""Tests for lib/art_sync_cache.py, including the prune-throttling fix
(previously did a full read-prune-write on every single successful
download; now only prunes once per _PRUNE_INTERVAL_SECONDS)."""


def test_already_synced_false_when_file_missing(kodi):
    from lib import art_sync_cache
    assert art_sync_cache.already_synced('/movies/x/poster.jpg', 'http://example/poster.jpg') is False


def test_already_synced_false_when_no_url(kodi):
    from lib import art_sync_cache
    assert art_sync_cache.already_synced('/movies/x/poster.jpg', None) is False


def test_remember_then_already_synced_true(kodi):
    from lib import art_sync_cache
    import xbmcvfs
    dest = '/movies/x/poster.jpg'
    xbmcvfs.File(dest, 'w').write(bytearray(b'fake image bytes'))

    art_sync_cache.remember(dest, 'http://example/poster.jpg')
    assert art_sync_cache.already_synced(dest, 'http://example/poster.jpg') is True


def test_already_synced_false_when_url_changed(kodi):
    from lib import art_sync_cache
    import xbmcvfs
    dest = '/movies/x/poster.jpg'
    xbmcvfs.File(dest, 'w').write(bytearray(b'fake image bytes'))

    art_sync_cache.remember(dest, 'http://example/old-poster.jpg')
    assert art_sync_cache.already_synced(dest, 'http://example/new-poster.jpg') is False


def test_already_synced_false_when_file_deleted_after_remember(kodi):
    """The correctness-critical check (file still exists) must be re-verified
    against the real filesystem every time -- the cache only ever
    short-circuits a download, never fabricates a file's existence."""
    from lib import art_sync_cache
    import xbmcvfs
    dest = '/movies/x/poster.jpg'
    xbmcvfs.File(dest, 'w').write(bytearray(b'fake image bytes'))
    art_sync_cache.remember(dest, 'http://example/poster.jpg')

    xbmcvfs._files.pop(dest)  # simulate the file having been deleted/moved
    assert art_sync_cache.already_synced(dest, 'http://example/poster.jpg') is False


def test_remember_does_not_prune_before_interval_elapses(kodi):
    import lib.art_sync_cache as asc
    real_time = [1000.0]
    asc.time.time = lambda: real_time[0]

    asc.remember('/a/poster.jpg', 'http://x/a.jpg')
    real_time[0] += 10  # well under _PRUNE_INTERVAL_SECONDS
    asc.remember('/b/poster.jpg', 'http://x/b.jpg')

    raw = asc.json_cache_file.read(asc._CACHE_PATH)
    # No prune ran, so no '__pruned_at__' marker should have been written yet.
    assert '__pruned_at__' not in raw
    assert '/a/poster.jpg' in raw
    assert '/b/poster.jpg' in raw


def test_remember_prunes_stale_entries_after_interval(kodi):
    import lib.art_sync_cache as asc
    real_time = [1000.0]
    asc.time.time = lambda: real_time[0]

    asc.remember('/old/poster.jpg', 'http://x/old.jpg')
    real_time[0] += asc._PRUNE_AFTER_SECONDS + 1
    real_time[0] += asc._PRUNE_INTERVAL_SECONDS  # also past the prune-throttle interval
    asc.remember('/new/poster.jpg', 'http://x/new.jpg')

    raw = asc.json_cache_file.read(asc._CACHE_PATH)
    assert '/old/poster.jpg' not in raw
    assert '/new/poster.jpg' in raw
    assert '__pruned_at__' in raw
