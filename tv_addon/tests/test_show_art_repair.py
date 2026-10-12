# -*- coding: utf-8 -*-
"""
A wrong poster.jpg left in a show's folder overrides every scraper's art in Kodi and comes back after each refresh
(2026-10-12: Invasion showed a Latvian film's poster). The show's own local poster is deleted when Chronicle has one to
show instead.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

from lib import show_art_repair

FOLDER = 'smb://10.0.0.162/Video7/TV/Invasion (2021)/'
ART = {'poster': [{'url': 'https://simkl.in/posters/x_m.jpg'}], 'fanart': [{'url': 'https://f/x.jpg'}]}


class TestNames(unittest.TestCase):

    def test_poster_names_kodi_reads(self):
        for name in ('poster.jpg', 'Poster.PNG', 'folder.jpg', 'poster.tbn', 'folder.jpeg', 'poster.webp'):
            self.assertTrue(show_art_repair.is_local_poster_name(name), name)

    def test_everything_else_is_left_alone(self):
        for name in ('fanart.jpg', 'fanart1.jpg', 'season01-poster.jpg', 'banner.jpg', 'clearlogo.png', 'theme.mp3',
                     'poster.nfo', 'Invasion.S01E01.mkv', 'poster', '', None):
            self.assertFalse(show_art_repair.is_local_poster_name(name), name)


class TestRemoval(unittest.TestCase):

    def _run(self, files, enabled=True, artwork=ART, folder=FOLDER, delete_ok=True):
        addon = MagicMock()
        addon.getSettingBool.return_value = enabled
        with patch.object(show_art_repair.xbmcaddon, 'Addon', return_value=addon), \
                patch.object(show_art_repair, 'find_show_location', return_value=(folder, 167)) as find, \
                patch.object(show_art_repair, 'listdir_with_timeout', return_value=([], files)) as listing, \
                patch.object(show_art_repair.xbmcvfs, 'delete', create=True, return_value=delete_ok) as delete, \
                patch.object(show_art_repair, '_invalidate_texture') as invalidate:
            removed = show_art_repair.remove_local_show_posters('Invasion', 2021, artwork)
        return removed, find, listing, delete, invalidate

    def test_removes_the_local_poster_and_its_cached_texture(self):
        removed, _, _, delete, invalidate = self._run(['poster.jpg', 'fanart.jpg', 'season01-poster.jpg', 'banner.jpg'])
        self.assertEqual(1, removed)
        delete.assert_called_once_with(FOLDER + 'poster.jpg')
        invalidate.assert_called_once_with(FOLDER + 'poster.jpg')

    def test_removes_every_poster_spelling_present(self):
        removed, _, _, delete, _ = self._run(['poster.jpg', 'folder.png'])
        self.assertEqual(2, removed)
        self.assertEqual(2, delete.call_count)

    def test_folder_without_trailing_slash(self):
        _, _, _, delete, _ = self._run(['poster.jpg'], folder=FOLDER.rstrip('/'))
        delete.assert_called_once_with(FOLDER + 'poster.jpg')

    def test_setting_off_touches_nothing(self):
        removed, find, listing, delete, _ = self._run(['poster.jpg'], enabled=False)
        self.assertEqual(0, removed)
        find.assert_not_called()
        listing.assert_not_called()
        delete.assert_not_called()

    def test_no_chronicle_poster_keeps_the_local_file(self):
        for artwork in (None, {}, {'fanart': [{'url': 'https://f/x.jpg'}]}, {'poster': []}):
            removed, _, _, delete, _ = self._run(['poster.jpg'], artwork=artwork)
            self.assertEqual(0, removed)
            delete.assert_not_called()

    def test_show_folder_not_found_keeps_everything(self):
        removed, _, listing, delete, _ = self._run(['poster.jpg'], folder=None)
        self.assertEqual(0, removed)
        listing.assert_not_called()
        delete.assert_not_called()

    def test_unreadable_folder(self):
        addon = MagicMock()
        addon.getSettingBool.return_value = True
        with patch.object(show_art_repair.xbmcaddon, 'Addon', return_value=addon), \
                patch.object(show_art_repair, 'find_show_location', return_value=(FOLDER, 1)), \
                patch.object(show_art_repair, 'listdir_with_timeout', return_value=(None, None)):
            self.assertEqual(0, show_art_repair.remove_local_show_posters('Invasion', 2021, ART))

    def test_a_failed_delete_is_not_counted_or_invalidated(self):
        removed, _, _, _, invalidate = self._run(['poster.jpg'], delete_ok=False)
        self.assertEqual(0, removed)
        invalidate.assert_not_called()

    def test_errors_never_escape(self):
        with patch.object(show_art_repair.xbmcaddon, 'Addon', side_effect=RuntimeError('boom')):
            self.assertEqual(0, show_art_repair.remove_local_show_posters('Invasion', 2021, ART))


class TestWiring(unittest.TestCase):

    def test_get_details_calls_it_after_pinning_chronicles_art(self):
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python', 'tvshow_scraper.py')
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'get_details')
        calls = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn('remove_local_show_posters', calls)
        self.assertLess(calls.index('apply_artwork'), calls.index('remove_local_show_posters'))


if __name__ == '__main__':
    unittest.main()
