# -*- coding: utf-8 -*-
"""
lib/episode_numbers -- season/episode read from a video file name. Kodi's own numbers can be wrong
(mis-scrape, later renumbering); matching by them pairs a file with the wrong Chronicle episode,
so its title/plot/watched state land on the wrong episode. The file name is the independent witness.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import episode_numbers


class TestFromFileName(unittest.TestCase):

    def test_sxxexx_variants(self):
        for name, expected in (
            ('School Spirits (2023) - S01E03 - Dead and Confused.mkv', (1, 3)),
            ('show.s2e10.720p.mkv', (2, 10)),
            ('Show S01.E05.mkv', (1, 5)),
            ('Show S01_E05.mkv', (1, 5)),
            ('smb://nas/tv/Show/Season 1/Show - S01E03E04.mkv', (1, 3)),
        ):
            self.assertEqual(episode_numbers.from_file_name(name), expected, name)

    def test_1x03_form_needs_a_two_digit_episode(self):
        self.assertEqual(episode_numbers.from_file_name('Show 1x03.mkv'), (1, 3))
        self.assertEqual(episode_numbers.from_file_name('Show 1x3.mkv'), (None, None))

    def test_resolutions_and_years_are_not_episode_numbers(self):
        self.assertEqual(episode_numbers.from_file_name('Movie 1920x1080.mkv'), (None, None))
        self.assertEqual(episode_numbers.from_file_name('Show (2023) - Pilot.mkv'), (None, None))

    def test_only_the_file_name_counts_not_the_folder(self):
        self.assertEqual(episode_numbers.from_file_name('smb://nas/tv/Show S01E01/Pilot.mkv'), (None, None))

    def test_empty_input(self):
        self.assertEqual(episode_numbers.from_file_name(None), (None, None))
        self.assertEqual(episode_numbers.from_file_name(''), (None, None))


class TestResolve(unittest.TestCase):

    def test_the_file_name_wins_over_kodis_own_numbers(self):
        ep = {'file': 'smb://x/Show - S01E03.mkv', 'season': 1, 'episode': 5}
        self.assertEqual(episode_numbers.resolve(ep), (1, 3))

    def test_falls_back_to_kodis_numbers_when_the_name_has_none(self):
        ep = {'file': 'smb://x/Pilot.mkv', 'season': 2, 'episode': 4}
        self.assertEqual(episode_numbers.resolve(ep), (2, 4))


if __name__ == '__main__':
    unittest.main()
