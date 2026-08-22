# -*- coding: utf-8 -*-
"""Tests for the shared lib/remote_file.py download+write helper, consolidated
from three previously-independent copies (movie_art_sync.py, collection_sync.py,
tv_art_sync.py) during code review. Covers the 3-state result contract every
caller now relies on, including the silent xbmcvfs.File.write() failure case
the two simpler original copies didn't check for."""

from unittest.mock import patch, MagicMock
import io


def _fake_urlopen(data=b'fake image bytes', raise_exc=None):
    if raise_exc:
        def _raise(*a, **k):
            raise raise_exc
        return _raise

    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = data
    cm.__exit__.return_value = False
    return lambda *a, **k: cm


def test_write_remote_file_ok(kodi):
    from lib import remote_file
    import xbmcvfs
    with patch('urllib.request.urlopen', _fake_urlopen(b'hello')):
        result = remote_file.write_remote_file('/movies/x/poster.jpg', 'http://example/p.jpg')
    assert result == 'ok'
    assert bytes(xbmcvfs.File('/movies/x/poster.jpg', 'r').readBytes()) == b'hello'


def test_write_remote_file_download_failed(kodi):
    from lib import remote_file
    with patch('urllib.request.urlopen', _fake_urlopen(raise_exc=OSError('network down'))):
        result = remote_file.write_remote_file('/movies/x/poster.jpg', 'http://example/p.jpg')
    assert result == 'download_failed'


def test_write_remote_file_write_failed_on_exception(kodi):
    from lib import remote_file
    import xbmcvfs

    def _raise_on_write(path, mode):
        raise OSError('permission denied')
    xbmcvfs.File = _raise_on_write

    with patch('urllib.request.urlopen', _fake_urlopen(b'hello')):
        result = remote_file.write_remote_file('/movies/x/poster.jpg', 'http://example/p.jpg')
    assert result == 'write_failed'


def test_write_remote_file_write_failed_on_falsy_return(kodi):
    """xbmcvfs.File.write() doesn't always raise on a real VFS failure
    (permission denied, unreachable share) -- it can just return a falsy
    value. This is the exact check the two simpler original copies of this
    function (movie_art_sync.py, tv_art_sync.py) were missing before
    consolidation; only collection_sync.py's had it."""
    from lib import remote_file

    class _SilentlyFailingFile:
        def __init__(self, path, mode):
            pass

        def write(self, data):
            return False  # falsy, no exception

        def close(self):
            pass

    import xbmcvfs
    xbmcvfs.File = _SilentlyFailingFile

    with patch('urllib.request.urlopen', _fake_urlopen(b'hello')):
        result = remote_file.write_remote_file('/movies/x/poster.jpg', 'http://example/p.jpg')
    assert result == 'write_failed'
