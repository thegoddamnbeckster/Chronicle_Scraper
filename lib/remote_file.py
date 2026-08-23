# -*- coding: utf-8 -*-
"""Shared download+write helper for every local art syncer (movie, TV, and
movie-set/collection). Previously three separate, independently-maintained
copies of the same logic existed in movie_art_sync.py, collection_sync.py,
and tv_art_sync.py -- collection_sync.py's copy was the most complete of the
three (it also catches xbmcvfs.File.write() returning a falsy value without
raising, which the other two silently missed), so that's the version kept
here; the other two call sites were upgraded to it rather than the reverse.
"""

import urllib.request

from lib.logger import Logger
import xbmcvfs

log = Logger('remote_file')


def write_remote_file(dest_path, url):
    """Downloads `url` and writes it to `dest_path`. Returns 'ok',
    'download_failed', or 'write_failed' -- callers that need to distinguish
    a bad download from an unwritable destination (e.g. to decide whether a
    whole folder, not just one file, is the problem) can check which."""
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = resp.read()
    except Exception as exc:
        log.warning("Couldn't download {0}: {1}".format(url, exc))
        return 'download_failed'

    try:
        f = xbmcvfs.File(dest_path, 'w')
        try:
            written = f.write(bytearray(data))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write {0}: {1}: {2}".format(dest_path, type(exc).__name__, exc))
        return 'write_failed'

    # xbmcvfs.File.write() doesn't raise on most VFS failures (permission denied,
    # unreachable share) -- it just silently returns a falsy value, so that has
    # to be treated as a real failure, not just an exception.
    ok = bool(written) if written is not None else True
    if not ok:
        log.warning('xbmcvfs.File.write() returned falsy for {0} (no exception raised)'.format(dest_path))
        return 'write_failed'
    return 'ok'
