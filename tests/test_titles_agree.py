# -*- coding: utf-8 -*-
"""
media_id_cache.titles_agree -- the guard a cached Kodi-id -> Chronicle-id mapping must pass before
anything is written. Live (2026-09-26): 32 movies and 6 shows on one Kodi were mapped to a different
item ("Scream" -> "Evil Bong 2", "A Knight of the Seven Kingdoms" -> "Star Trek: Discovery").
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcaddon/xbmcvfs

from lib.media_id_cache import titles_agree


class TestTitlesAgree(unittest.TestCase):

    def test_the_real_cross_mappings_are_rejected(self):
        for kodi, chronicle in (
            ('Scream', "Charles Band's Evil Bong 2: King Bong"),
            ('A Knight of the Seven Kingdoms', 'Star Trek: Discovery'),
            ('Chad Powers', 'A Knight of the Seven Kingdoms'),
            ('Alien: Earth', 'Friends'),
            ('The Toxic Avenger Part II', '2 Lava 2 Lantula!'),
            ('Hitman', 'Bikini Summer'),
        ):
            self.assertFalse(titles_agree(kodi, chronicle), (kodi, chronicle))

    def test_legitimate_naming_differences_are_accepted(self):
        for kodi, chronicle in (
            ("Anne Rice's Mayfair Witches", 'Mayfair Witches'),
            ('Whose Line Is It Anyway? (US)', 'Whose Line Is It Anyway?'),
            ('Captain America: The Winter Soldier', 'Captain America: The Winter Soldier - Defrosted Edition'),
            ('Total Recall (1990)', 'Total Recall'),
        ):
            self.assertTrue(titles_agree(kodi, chronicle), (kodi, chronicle))

    def test_sequels_are_not_the_original(self):
        self.assertFalse(titles_agree('Home Alone', 'Home Alone 2: Lost in New York'))
        self.assertFalse(titles_agree('Rocky', 'Rocky III'))
        self.assertTrue(titles_agree('Blade Runner 2049', 'Blade Runner 2049'))

    def test_no_title_on_either_side_means_nothing_to_judge(self):
        self.assertTrue(titles_agree('', 'Anything'))
        self.assertTrue(titles_agree('Anything', None))


if __name__ == '__main__':
    unittest.main()
