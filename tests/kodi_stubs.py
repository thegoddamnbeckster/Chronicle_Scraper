# -*- coding: utf-8 -*-
"""
Minimal Kodi module stubs for unit tests -- same approach as SIMKL_Scrobbler's
own tests/kodi_stubs.py, extended with a working xbmcvfs.File/exists/mkdirs/
delete fake (SIMKL's own stub only needed translatePath; this addon's
tvshow_location.py location cache does real read/write/exists/mkdirs calls
that need to actually round-trip for a test to mean anything).

Import this module BEFORE any addon code, to satisfy the top-level
`import xbmc` / `import xbmcvfs` calls that live in the addon's own modules:

    import tests.kodi_stubs  # noqa: F401

The fake VFS is an in-memory dict, not real disk I/O -- paths are just
dictionary keys (special://... strings are never actually translated to a
real filesystem path, since nothing here calls translatePath to resolve
them). Call kodi_stubs.reset_vfs() between tests that touch the cache so one
test's cache entries can't leak into another's.
"""
import sys
import types
from unittest.mock import MagicMock

_FAKE_FILES = {}
_FAKE_DIRS = set()


def reset_vfs():
    """Clears the fake VFS -- call from setUp() in any test that reads or
    writes the location cache, so tests don't see each other's entries."""
    _FAKE_FILES.clear()
    _FAKE_DIRS.clear()


class _FakeXbmcVfsFile:
    def __init__(self, path, mode):
        self._path = path
        self._mode = mode
        self._buffer = _FAKE_FILES.get(path, b'') if 'r' in mode else b''

    def write(self, data):
        self._buffer += bytes(data)
        _FAKE_FILES[self._path] = self._buffer
        return True

    def readBytes(self, num_bytes=0):
        return bytearray(self._buffer)

    def close(self):
        pass


def _fake_exists(path):
    return path in _FAKE_FILES or path in _FAKE_DIRS


def _fake_mkdirs(path):
    _FAKE_DIRS.add(path)
    return True


def _fake_delete(path):
    _FAKE_FILES.pop(path, None)
    return True


class _FakeXbmcVfsStat:
    """Stands in for xbmcvfs.Stat -- only st_size() is used anywhere in this codebase so far.
    Raises like the real API would for a path that doesn't exist (movie_art_sync._local_file_size
    catches this and treats it as "unknown size")."""
    def __init__(self, path):
        if path not in _FAKE_FILES:
            raise OSError('No such file: {0!r}'.format(path))
        self._size = len(_FAKE_FILES[path])

    def st_size(self):
        return self._size


class _FakeListItem:
    """Stands in for xbmcgui.ListItem for tests that exercise get_details()/get_artwork() end
    to end -- a bare `MagicMock` alias (this module's original stub) breaks the instant real
    addon code calls it, since MagicMock's own __init__ treats the FIRST positional arg as
    `spec`: `ListItem(label, offscreen=True)` silently became `MagicMock(spec=label,
    offscreen=True)`, spec'd to whatever `str` has -- so getVideoInfoTag()/setArt() raised
    AttributeError instead of returning/doing anything, undetected until 2026-09-13 because no
    existing test called a function that reaches either (same bug already found and fixed in
    the TV addon's own kodi_stubs.py -- ported here). getVideoInfoTag() returns a fresh,
    unconstrained MagicMock -- tests that care what got set on it should capture and assert
    against that return value, not construct their own."""

    def __init__(self, label='', offscreen=False):
        self.label = label
        self.offscreen = offscreen
        self._video_info_tag = MagicMock()
        self._art = {}

    def getVideoInfoTag(self):
        return self._video_info_tag

    def setArt(self, art):
        self._art.update(art)


def _install():
    xbmc = types.ModuleType('xbmc')
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 2
    xbmc.LOGWARNING = 3
    xbmc.LOGERROR = 4
    xbmc.log = MagicMock()
    xbmc.sleep = MagicMock()
    xbmc.Monitor = MagicMock
    xbmc.executeJSONRPC = MagicMock(return_value='{"result": {}}')
    xbmc.getCondVisibility = MagicMock(return_value=False)

    xbmcaddon = types.ModuleType('xbmcaddon')
    _addon = MagicMock()
    _addon.getSetting = MagicMock(return_value='')
    _addon.getSettingBool = MagicMock(return_value=False)
    _addon.getSettingInt = MagicMock(return_value=0)
    xbmcaddon.Addon = MagicMock(return_value=_addon)

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui.ListItem = _FakeListItem
    xbmcgui.Dialog = MagicMock
    xbmcgui.NOTIFICATION_INFO = 0
    xbmcgui.NOTIFICATION_WARNING = 1
    xbmcgui.NOTIFICATION_ERROR = 2

    xbmcplugin = types.ModuleType('xbmcplugin')
    xbmcplugin.setResolvedUrl = MagicMock()
    xbmcplugin.addDirectoryItem = MagicMock()
    xbmcplugin.endOfDirectory = MagicMock()

    xbmcvfs = types.ModuleType('xbmcvfs')
    xbmcvfs.translatePath = MagicMock(return_value='')
    xbmcvfs.File = _FakeXbmcVfsFile
    xbmcvfs.exists = _fake_exists
    xbmcvfs.mkdirs = _fake_mkdirs
    xbmcvfs.delete = _fake_delete
    xbmcvfs.Stat = _FakeXbmcVfsStat

    for name, mod in [
        ('xbmc', xbmc), ('xbmcaddon', xbmcaddon), ('xbmcgui', xbmcgui),
        ('xbmcplugin', xbmcplugin), ('xbmcvfs', xbmcvfs),
    ]:
        sys.modules[name] = mod


_install()
