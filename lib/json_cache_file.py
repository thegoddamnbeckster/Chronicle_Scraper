# -*- coding: utf-8 -*-
"""Shared "read a JSON dict from a VFS path, write one back" helper for the
addon's small best-effort file-based caches (location_cache.py,
art_sync_cache.py). Both previously carried byte-for-byte identical copies
of this read/write logic; a change to one (e.g. handling a new xbmcvfs error
mode) had to be remembered and applied twice. Deliberately does not include
any TTL/pruning logic -- each cache's own key shape and expiry policy stay
in that cache's own module; this only ever moves bytes.
"""

import json

import xbmcvfs


def read(path):
    """Returns the dict stored at `path`, or {} if it doesn't exist or fails
    to parse for any reason -- a corrupt or missing cache file is always
    treated as an empty cache, never an error the caller has to handle."""
    if not xbmcvfs.exists(path):
        return {}
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
        return json.loads(raw.decode('utf-8')) or {}
    except Exception:
        return {}


def write(path, data):
    """Writes `data` (a JSON-serializable dict) to `path`, creating the
    containing directory first if it doesn't exist yet."""
    folder = path.rsplit('/', 1)[0] + '/'
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(bytearray(json.dumps(data).encode('utf-8')))
    finally:
        f.close()
