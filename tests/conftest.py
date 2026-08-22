# -*- coding: utf-8 -*-
"""Test scaffold for this Kodi addon's pure-Python logic.

Kodi's own xbmc/xbmcvfs/xbmcaddon/xbmcgui/xbmcplugin modules only exist
inside a running Kodi process, so every lib/ and python/ module imports them
at module scope. This installs minimal fakes into sys.modules BEFORE any
addon code is imported, sufficient for unit-testing the addon's own logic
(caching, matching, artwork/NFO assembly) without a real Kodi instance --
not a faithful Kodi emulator, just enough surface for these modules to import
and run against.
"""

import sys
import types
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeVfsFile:
    """Backed by a shared in-memory byte-string store (see FakeXbmcVfs) so a
    write from one FakeVfsFile instance is visible to a later read via
    another -- real xbmcvfs.File instances are independent handles onto the
    same underlying filesystem, and callers rely on that."""

    def __init__(self, store, path, mode):
        self._store = store
        self._path = path
        self._mode = mode
        self._write_buf = None

    def readBytes(self):
        return bytearray(self._store.get(self._path, b''))

    def write(self, data):
        self._store[self._path] = bytes(data)
        return True

    def close(self):
        pass


class FakeXbmcVfs(types.ModuleType):
    """Each instance owns its own isolated in-memory 'filesystem' (a dict
    keyed by path) -- tests construct their own via make_fake_xbmcvfs()
    rather than sharing the process-wide fake, so one test's writes can
    never leak into another's."""

    def __init__(self):
        super().__init__('xbmcvfs')
        self._files = {}
        self._dirs = set()

    def exists(self, path):
        return path in self._files or path in self._dirs or path.rstrip('/') in self._dirs

    def mkdirs(self, path):
        self._dirs.add(path)

    def File(self, path, mode='r'):
        return FakeVfsFile(self._files, path, mode)

    def translatePath(self, path):
        return path


class FakeAddon:
    def __init__(self, settings=None):
        self._settings = settings or {}

    def getAddonInfo(self, key):
        return {'id': 'script.chronicle.scraper', 'version': '0.0.0-test'}.get(key, '')

    def getSetting(self, key):
        return str(self._settings.get(key, ''))

    def getSettingBool(self, key):
        return bool(self._settings.get(key, False))

    def getLocalizedString(self, code):
        return str(code)


def install_kodi_mocks(monkeypatch, settings=None):
    """Installs fresh fake xbmc* modules into sys.modules for the duration of
    one test. Returns the fake xbmcvfs module so a test can also use it
    directly (e.g. to pre-seed a file, or assert what got written)."""
    fake_vfs = FakeXbmcVfs()

    fake_xbmc = types.ModuleType('xbmc')
    fake_xbmc.LOGDEBUG = 0
    fake_xbmc.LOGINFO = 1
    fake_xbmc.LOGWARNING = 2
    fake_xbmc.LOGERROR = 3
    fake_xbmc.log = lambda msg, level=0: None

    class _Actor:
        def __init__(self, name='', role='', order=0):
            self.name, self.role, self.order = name, role, order
    fake_xbmc.Actor = _Actor

    fake_xbmcaddon = types.ModuleType('xbmcaddon')
    fake_xbmcaddon.Addon = lambda *a, **k: FakeAddon(settings)

    fake_xbmcgui = types.ModuleType('xbmcgui')

    class _ListItem:
        def __init__(self, label='', offscreen=False):
            self.label = label
            self._art = {}
            self._fanart = []
            self._vtag = _VideoInfoTag()

        def getVideoInfoTag(self):
            return self._vtag

        def setArt(self, art):
            self._art = art

        def setAvailableFanart(self, fanart_list):
            self._fanart = fanart_list

    class _VideoInfoTag:
        def __init__(self):
            self.available_artwork = []  # list of (url, art_type, kwargs)
            self.seasons = []
            self.cast = []

        def setMediaType(self, *a, **k): pass
        def setTvShowTitle(self, *a, **k): pass
        def setTagLine(self, *a, **k): pass
        def setTvShowStatus(self, *a, **k): pass
        def setDuration(self, *a, **k): pass
        def setEpisodeGuide(self, *a, **k): pass
        def setCast(self, cast): self.cast = cast

        def addSeason(self, number, name):
            self.seasons.append((number, name))

        def addAvailableArtwork(self, url, art_type, **kwargs):
            self.available_artwork.append((url, art_type, kwargs))

    fake_xbmcgui.ListItem = _ListItem

    fake_xbmcplugin = types.ModuleType('xbmcplugin')
    fake_xbmcplugin.setResolvedUrl = lambda **k: None
    fake_xbmcplugin.addDirectoryItem = lambda **k: None
    fake_xbmcplugin.endOfDirectory = lambda *a, **k: None

    modules = {
        'xbmc': fake_xbmc,
        'xbmcvfs': fake_vfs,
        'xbmcaddon': fake_xbmcaddon,
        'xbmcgui': fake_xbmcgui,
        'xbmcplugin': fake_xbmcplugin,
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # Any lib/python module already imported by an earlier test holds a
    # reference to the OLD fake xbmcvfs (module-level `ADDON = xbmcaddon.Addon()`
    # and similar run once at import time) -- force a clean re-import so every
    # test gets modules wired to its own fresh fakes.
    for mod_name in list(sys.modules):
        if mod_name.startswith('lib.') or mod_name.startswith('python.') or mod_name == 'lib':
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    return fake_vfs


import pytest


@pytest.fixture
def kodi(monkeypatch):
    """Installs fresh Kodi module fakes and returns the fake xbmcvfs so a
    test can inspect what got written."""
    return install_kodi_mocks(monkeypatch)


@pytest.fixture
def kodi_with_settings():
    """Like `kodi`, but lets a test supply ADDON.getSetting*() values."""
    def _make(monkeypatch, settings):
        return install_kodi_mocks(monkeypatch, settings)
    return _make
