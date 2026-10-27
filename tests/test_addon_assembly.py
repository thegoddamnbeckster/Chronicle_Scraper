# -*- coding: utf-8 -*-
"""Each Chronicle Kodi add-on, assembled the way its build.ps1 assembles it, must be self-contained and complete.

The add-ons share modules from Chronicle_Scraper/shared that each build copies into the add-on's own lib/. Their unit tests see those
modules through a path hook, which would hide a build that forgot to copy one -- exactly the failure that cannot be noticed until the
zip is on a Kodi box. So this test builds each add-on into a scratch folder with only what its build script copies, then, in a fresh
interpreter with only that folder on the path, imports every module, and checks that:

  * every module imports (no missing shared module, no stale import of a deleted one);
  * every getLocalizedString(<id>) the add-on's code can reach has a string in its strings.po;
  * the string ids the shared connection flow reads exist in every add-on that uses it.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_SCRAPER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(_SCRAPER, 'shared')
_REPOS = os.path.dirname(_SCRAPER)

# add-on key -> (add-on folder, shared modules its build copies in (None = all), extra files copied from another folder into lib/)
_ADDONS = {
    'movie': (_SCRAPER, None, {}),
    'tv': (os.path.join(_SCRAPER, 'tv_addon'), None,
           {name: os.path.join(_SCRAPER, 'lib', name) for name in ('kodi_scan_signal.py', 'pass_progress.py', 'movie_art_sync.py', 'install_marker.py')}),
    'scrobbler': (os.path.join(_REPOS, 'Chronicle_Scrobbler'),
                  ('logger.py', 'redact.py', 'http_core.py', 'connection_health.py', 'device_name.py', 'device_auth.py', 'qr_dialog.py', 'connection_ui.py'), {}),
    'rating': (os.path.join(_REPOS, 'Chronicle_Rating'), ('logger.py', 'redact.py', 'http_core.py', 'connection_health.py'), {}),
}

_IMPORT_SCRIPT = r'''
import importlib, os, sys
stubs_path, root = sys.argv[1], sys.argv[2]
src = open(stubs_path, encoding='utf-8').read()
# the stubs' path hook would add Chronicle_Scraper/shared to lib; the assembled add-on must stand on its own
cut = src.rindex('\n_extend_lib_path()')
exec(compile(src[:cut], stubs_path, 'exec'), {'__name__': 'kodi_stubs_isolated'})
# anything the stubs do not define (a Window class a module subclasses, say) becomes an empty class: this test is about imports
import xbmc, xbmcaddon, xbmcgui, xbmcplugin, xbmcvfs
for _mod in (xbmc, xbmcaddon, xbmcgui, xbmcplugin, xbmcvfs):
    _mod.__getattr__ = lambda name: type(name, (object,), {'__init__': lambda self, *a, **k: None})
sys.path.insert(0, root)
failures = []
names = [f[:-3] for f in sorted(os.listdir(root)) if f.endswith('.py')]
names += ['lib.' + f[:-3] for f in sorted(os.listdir(os.path.join(root, 'lib'))) if f.endswith('.py') and f != '__init__.py']
for name in names:
    try:
        importlib.import_module(name)
    except BaseException as exc:
        failures.append('{0}: {1}: {2}'.format(name, type(exc).__name__, exc))
print('\n'.join(failures))
sys.exit(1 if failures else 0)
'''


def _assemble(key, dest):
    folder, shared, extra = _ADDONS[key]
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if name.endswith('.py'):
            shutil.copy(path, dest)
    shutil.copytree(os.path.join(folder, 'lib'), os.path.join(dest, 'lib'), ignore=shutil.ignore_patterns('__pycache__'))
    if os.path.isdir(os.path.join(folder, 'resources')):
        shutil.copytree(os.path.join(folder, 'resources'), os.path.join(dest, 'resources'))
    for name in (shared if shared is not None else [f for f in os.listdir(_SHARED) if f.endswith('.py')]):
        shutil.copy(os.path.join(_SHARED, name), os.path.join(dest, 'lib', name))
    for name, source in extra.items():
        shutil.copy(source, os.path.join(dest, 'lib', name))


def _stubs_for(key):
    folder = _ADDONS[key][0]
    return os.path.join(folder, 'tests', 'kodi_stubs.py')


def _string_ids_defined(root):
    ids = set()
    for dirpath, _dirs, files in os.walk(os.path.join(root, 'resources', 'language')):
        for f in files:
            if f == 'strings.po':
                with open(os.path.join(dirpath, f), encoding='utf-8') as fh:
                    ids.update(int(m) for m in re.findall(r'msgctxt "#(\d+)"', fh.read()))
    return ids


def _string_ids_used(root):
    used = {}
    for dirpath, _dirs, files in os.walk(root):
        if 'resources' in dirpath.split(os.sep):
            continue
        for f in files:
            if f.endswith('.py'):
                with open(os.path.join(dirpath, f), encoding='utf-8') as fh:
                    for m in re.finditer(r'getLocalizedString\(\s*(\d+)\s*\)', fh.read()):
                        used.setdefault(int(m.group(1)), set()).add(f)
    return used


class TestAddonAssembly(unittest.TestCase):
    pass


def _make_tests(key):
    def setUp_dir(self):
        folder = _ADDONS[key][0]
        if not os.path.isdir(folder):
            self.skipTest('{0} is not checked out next to Chronicle_Scraper'.format(folder))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        _assemble(key, self.root)

    def test_every_module_imports_from_the_assembled_addon_alone(self):
        setUp_dir(self)
        proc = subprocess.run([sys.executable, '-I', '-c', _IMPORT_SCRIPT, _stubs_for(key), self.root],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, 'import failures in the assembled {0} add-on:\n{1}{2}'.format(key, proc.stdout, proc.stderr))

    def test_every_localized_string_id_in_use_is_defined(self):
        setUp_dir(self)
        defined, used = _string_ids_defined(self.root), _string_ids_used(self.root)
        missing = {i: sorted(files) for i, files in used.items() if i not in defined}
        self.assertEqual(missing, {}, 'string ids used but missing from strings.po in {0}'.format(key))

    return setUp_dir, test_every_module_imports_from_the_assembled_addon_alone, test_every_localized_string_id_in_use_is_defined


for _key in _ADDONS:
    _, _imports, _strings = _make_tests(_key)
    setattr(TestAddonAssembly, 'test_{0}_modules_import_from_assembled_addon'.format(_key), _imports)
    setattr(TestAddonAssembly, 'test_{0}_localized_strings_defined'.format(_key), _strings)


class TestConnectionFlowStrings(unittest.TestCase):
    def test_every_addon_using_connection_ui_defines_its_string_ids(self):
        import tests.kodi_stubs  # noqa: F401
        from lib import connection_ui
        ids = connection_ui.STRING_IDS
        for key in ('movie', 'tv', 'scrobbler'):
            folder = _ADDONS[key][0]
            if not os.path.isdir(folder):
                continue
            missing = [i for i in ids if i not in _string_ids_defined(folder)]
            self.assertEqual(missing, [], '{0} lacks connection-flow strings'.format(key))


if __name__ == '__main__':
    unittest.main()
