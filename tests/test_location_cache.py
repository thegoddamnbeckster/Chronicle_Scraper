# -*- coding: utf-8 -*-
"""Tests for lib/location_cache.py -- the find()-to-getdetails() folder
bridge, including the TTL fix for the cross-contamination risk flagged in
code review (two different same-titled/same-year movies scraped close
together could otherwise share a cache entry)."""

def test_remember_then_recall_roundtrips(kodi):
    from lib import location_cache
    location_cache.remember('Warrior', 2011, '/movies/Warrior (2011)/', 'Warrior (2011)', 'Warrior (2011).mkv')
    result = location_cache.recall('Warrior', 2011)
    assert result == ('/movies/Warrior (2011)/', 'Warrior (2011)', 'Warrior (2011).mkv')


def test_recall_miss_returns_none(kodi):
    from lib import location_cache
    assert location_cache.recall('Nonexistent Movie', 1999) is None


def test_recall_is_case_and_punctuation_insensitive_but_year_exact(kodi):
    from lib import location_cache
    location_cache.remember('The Running Man', 1987, '/movies/rm87/', 'x', 'x.mkv')
    assert location_cache.recall('the running MAN!!', 1987) is not None
    # A different year for the same normalized title must NOT hit -- this is
    # exactly the Running-Man-style year-mismatch this addon's other fix
    # (FindByTitleAsync on the server side) guards against; the cache must
    # not reintroduce that risk on the client side.
    assert location_cache.recall('The Running Man', 2025) is None


def test_entries_expire_after_ttl(kodi):
    import lib.location_cache as lc
    real_time = [1000.0]
    lc.time.time = lambda: real_time[0]

    lc.remember('Warrior', 2011, '/movies/w/', 'w', 'w.mkv')
    assert lc.recall('Warrior', 2011) is not None

    real_time[0] += lc._TTL_SECONDS + 1
    assert lc.recall('Warrior', 2011) is None


def test_ttl_is_short_not_minutes(kodi):
    """Locks in the code-review fix: the TTL must stay an order of magnitude
    above a real find-to-getdetails gap (observed as low single-digit
    seconds), not the original 600s, which left a wide window for two
    different same-titled/same-year movies scraped close together to
    collide on the same cache key."""
    from lib import location_cache
    assert location_cache._TTL_SECONDS <= 60


def test_remember_prunes_expired_entries(kodi):
    from lib import location_cache

    real_time = [1000.0]
    import lib.location_cache as lc
    lc.time.time = lambda: real_time[0]

    lc.remember('Old Movie', 2000, '/a/', 'a', 'a.mkv')
    real_time[0] += lc._TTL_SECONDS + 5
    lc.remember('New Movie', 2020, '/b/', 'b', 'b.mkv')

    # The expired 'Old Movie' entry should be gone from the underlying store,
    # not just unreachable via recall() -- prevents the cache file from
    # growing unbounded across a long scan.
    raw = lc.json_cache_file.read(lc._CACHE_PATH)
    assert lc._key('Old Movie', 2000) not in raw
    assert lc._key('New Movie', 2020) in raw


def test_recall_survives_corrupt_cache_file(kodi):
    from lib import location_cache
    import xbmcvfs
    f = xbmcvfs.File(location_cache._CACHE_PATH, 'w')
    f.write(bytearray(b'not valid json {{{'))
    f.close()
    assert location_cache.recall('Anything', 2020) is None
