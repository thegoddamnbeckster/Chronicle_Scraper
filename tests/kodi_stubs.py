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
    def __init__(self, path, mode='r'):
        self._path = path
        self._mode = mode
        self._buffer = _FAKE_FILES.get(path, b'') if 'r' in mode else b''

    def write(self, data):
        self._buffer += bytes(data)
        _FAKE_FILES[self._path] = self._buffer
        return True

    def read(self, num_bytes=0):
        return bytes(self.readBytes(num_bytes)).decode('utf-8')

    def readBytes(self, num_bytes=0):
        # num_bytes=0 (the default every existing caller uses) reads everything from the current
        # position; a positive count reads that many bytes, like the real xbmcvfs.File.
        pos = getattr(self, '_pos', 0)
        end = len(self._buffer) if not num_bytes else pos + num_bytes
        data = self._buffer[pos:end]
        self._pos = pos + len(data)
        return bytearray(data)

    def seek(self, offset, whence=0):
        self._pos = offset
        return offset

    def size(self):
        return len(self._buffer)

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


    class _Monitor:
        """Base of the services' monitors: never aborts unless a test sets .aborted; waitForAbort returns at once."""
        aborted = False

        def __init__(self, *args, **kwargs):
            pass

        def abortRequested(self):
            return self.aborted

        def waitForAbort(self, timeout=0):
            return self.aborted

    xbmc.Monitor = _Monitor

    class _Actor:
        """Stands in for xbmc.Actor: keeps what the scraper passed so a test can read it back."""
        def __init__(self, name='', role='', order=0, thumbnail=''):
            self.name, self.role, self.order, self.thumbnail = name, role, order, thumbnail

    xbmc.Actor = _Actor
    xbmc.executeJSONRPC = MagicMock(return_value='{"result": {}}')
    xbmc.getCondVisibility = MagicMock(return_value=False)
    xbmc.getInfoLabel = MagicMock(return_value='')

    xbmcaddon = types.ModuleType('xbmcaddon')
    _addon = MagicMock()
    _addon.getSetting = MagicMock(return_value='')
    _addon.getSettingBool = MagicMock(return_value=False)
    _addon.getSettingInt = MagicMock(return_value=0)
    xbmcaddon.Addon = MagicMock(return_value=_addon)

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui.ListItem = _FakeListItem
    xbmcgui.Dialog = MagicMock
    xbmcgui.DialogProgressBG = MagicMock
    xbmcgui.DialogProgress = MagicMock

    class _WindowDialog:
        """Base of the QR window: every control/window method is a recorded no-op."""
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            if name.startswith('__'):
                raise AttributeError(name)
            return MagicMock(name=name)

    xbmcgui.WindowDialog = _WindowDialog
    for _control in ('ControlImage', 'ControlLabel', 'ControlButton', 'ControlProgress', 'ControlTextBox'):
        setattr(xbmcgui, _control, MagicMock(name=_control))
    xbmcgui.ACTION_PREVIOUS_MENU = 10
    xbmcgui.ACTION_NAV_BACK = 92
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


def _extend_lib_path():
    """Makes the modules the build copies into lib/ importable as lib.<name>: everything in shared/, and (TV add-on) the few modules that
    live in the movie add-on's lib/ and are copied into the TV add-on's lib/ by build.ps1. The TV add-on's own lib/ wins on a name clash."""
    import os
    import lib
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    is_tv = os.path.basename(repo) == 'tv_addon'
    if is_tv:
        repo = os.path.dirname(repo)
    extra = [os.path.join(repo, 'shared')]
    if is_tv:
        extra.append(os.path.join(repo, 'lib'))
    for path in extra:
        path = os.path.normpath(path)
        if path not in lib.__path__:
            lib.__path__.append(path)


_extend_lib_path()
