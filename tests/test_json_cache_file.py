# -*- coding: utf-8 -*-
"""Tests for the shared lib/json_cache_file.py read/write helper, consolidated
from identical copies in location_cache.py and art_sync_cache.py."""


def test_read_missing_file_returns_empty_dict(kodi):
    from lib import json_cache_file
    assert json_cache_file.read('special://profile/addon_data/x/nope.json') == {}


def test_write_then_read_roundtrips(kodi):
    from lib import json_cache_file
    path = 'special://profile/addon_data/x/cache.json'
    json_cache_file.write(path, {'a': 1, 'b': [1, 2, 3]})
    assert json_cache_file.read(path) == {'a': 1, 'b': [1, 2, 3]}


def test_read_corrupt_json_returns_empty_dict(kodi):
    from lib import json_cache_file
    import xbmcvfs
    path = 'special://profile/addon_data/x/cache.json'
    f = xbmcvfs.File(path, 'w')
    f.write(bytearray(b'{not valid json'))
    f.close()
    assert json_cache_file.read(path) == {}


def test_write_creates_containing_directory(kodi):
    from lib import json_cache_file
    import xbmcvfs
    path = 'special://profile/addon_data/x/sub/cache.json'
    assert not xbmcvfs.exists('special://profile/addon_data/x/sub/')
    json_cache_file.write(path, {'k': 'v'})
    assert xbmcvfs.exists('special://profile/addon_data/x/sub/')
