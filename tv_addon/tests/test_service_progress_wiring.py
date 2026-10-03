# -*- coding: utf-8 -*-
"""
Wiring test for tv_addon/service.py's post-scan "verify every episode" check (added 2026-10-03).

That pass writes corrections into the library, which makes Kodi's screens refresh, and used to do so with
no on-screen explanation. It must feed the shared progress reporter (lib/pass_progress.py, copied into the
TV package from the movie add-on by build.ps1) and end with a notification. Source-level only: the shared
module isn't in tv_addon/lib in the working tree, so service.py can't be imported here.
"""
import ast
import os
import unittest

_SERVICE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'service.py')


class TestTvFullSyncCheckReportsProgress(unittest.TestCase):

    def setUp(self):
        with open(_SERVICE, encoding='utf-8') as f:
            self.source = f.read()
        self.tree = ast.parse(self.source)

    def test_full_sync_check_run_gets_a_progress_callback(self):
        calls = [n for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'run'
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'full_sync_check']
        self.assertTrue(calls)
        for call in calls:
            self.assertIn('progress_callback', {k.arg for k in call.keywords})

    def test_it_uses_the_shared_reporter_and_announces_the_result(self):
        self.assertIn('from lib import pass_progress', self.source)
        self.assertIn('pass_progress.PassProgress(', self.source)
        self.assertIn('_notify_full_sync_check_result(result)', self.source)
        self.assertIn('import xbmcgui', self.source)

    def test_the_strings_it_uses_exist(self):
        po = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'resources', 'language', 'resource.language.en_gb', 'strings.po')
        with open(po, encoding='utf-8') as f:
            text = f.read()
        for string_id in (32186, 32187, 32188, 32189, 32190, 32191):
            self.assertIn('msgctxt "#{0}"'.format(string_id), text)


if __name__ == '__main__':
    unittest.main()
