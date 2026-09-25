# -*- coding: utf-8 -*-
"""
lib/episode_numbers -- matching a Kodi episode to Chronicle's for the same FILE. The cases are the
real ones found on a live library (2026-09-26): multi-episode files that Kodi lists as two entries,
and a show whose files are numbered differently from Chronicle's list, which put the wrong title,
plot and thumb on the file.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import episode_numbers


def _ep(id_, season, episode, title):
    return {'id': id_, 'season': season, 'episode': episode, 'title': title}


def _kodi(file, season, episode):
    return {'file': 'smb://nas/tv/Show/' + file, 'season': season, 'episode': episode}


class TestParse(unittest.TestCase):

    def test_sxxexx_variants(self):
        for name, expected in (
            ('School Spirits (2023) - S01E03 - Dead and Confused.mkv', (1, [3])),
            ('show.s2e10.720p.mkv', (2, [10])),
            ('Show S01.E05.mkv', (1, [5])),
            ('Show S01_E05.mkv', (1, [5])),
        ):
            season, episodes, _ = episode_numbers.parse(name)
            self.assertEqual((season, episodes), expected, name)

    def test_multi_episode_files_yield_every_episode(self):
        self.assertEqual(episode_numbers.parse('Enterprise - S01E01-E02 - Broken Bow.mkv')[:2], (1, [1, 2]))
        self.assertEqual(episode_numbers.parse('Show - S01E03E04.mkv')[:2], (1, [3, 4]))

    def test_title_is_what_follows_the_numbering(self):
        self.assertEqual(episode_numbers.parse('Voyager - S01E03 - Parallax.mkv')[2], 'Parallax')
        self.assertEqual(episode_numbers.parse('Voyager - S01E02 - Caretaker (2).mkv')[2], 'Caretaker')
        self.assertIsNone(episode_numbers.parse('Show S01E01.mkv')[2])
        self.assertEqual(episode_numbers.parse('Show S01E01 [1080p] Pilot.mkv')[2], 'Pilot')

    def test_1x03_form_needs_a_two_digit_episode(self):
        self.assertEqual(episode_numbers.parse('Show 1x03.mkv')[:2], (1, [3]))
        self.assertEqual(episode_numbers.parse('Show 1x3.mkv')[:2], (None, []))

    def test_resolutions_years_and_folders_are_not_episode_numbers(self):
        self.assertEqual(episode_numbers.parse('Movie 1920x1080.mkv')[:2], (None, []))
        self.assertEqual(episode_numbers.parse('Show (2023) - Pilot.mkv')[:2], (None, []))
        self.assertEqual(episode_numbers.parse('smb://nas/tv/Show S01E01/Pilot.mkv')[:2], (None, []))
        self.assertEqual(episode_numbers.parse(None)[:2], (None, []))


class TestNormalizeTitle(unittest.TestCase):

    def test_two_part_markers_and_punctuation_compare_equal(self):
        for t in ('Caretaker', 'Caretaker (1)', 'Caretaker, Part I', 'caretaker!', 'Caretaker Part 2'):
            self.assertEqual(episode_numbers.normalize_title(t), 'caretaker', t)


class TestResolve(unittest.TestCase):

    def test_single_episode_file_uses_the_file_names_numbers(self):
        self.assertEqual(episode_numbers.resolve(_kodi('Show - S01E03.mkv', 1, 5)), (1, 3))

    def test_multi_episode_file_keeps_each_entrys_own_number(self):
        f = 'Enterprise - S01E01-E02 - Broken Bow.mkv'
        self.assertEqual(episode_numbers.resolve(_kodi(f, 1, 1)), (1, 1))
        self.assertEqual(episode_numbers.resolve(_kodi(f, 1, 2)), (1, 2))

    def test_falls_back_to_kodis_numbers_when_the_name_has_none(self):
        self.assertEqual(episode_numbers.resolve(_kodi('Pilot.mkv', 2, 4)), (2, 4))


class TestMatch(unittest.TestCase):

    # Chronicle counts Voyager's pilot as one episode; the files count it as E01 + E02.
    VOYAGER = [_ep(1, 1, 1, 'Caretaker'), _ep(2, 1, 2, 'Parallax'), _ep(3, 1, 3, 'Time and Again')]

    def test_title_beats_a_number_that_disagrees_between_the_files_and_chronicle(self):
        # The live bug: this file is "Parallax", but its number (3) is Chronicle's 'Time and Again'.
        kodi = _kodi('Voyager - S01E03 - Parallax.mkv', 1, 3)
        self.assertEqual(episode_numbers.match(kodi, self.VOYAGER)['id'], 2)

    def test_a_matching_title_and_number_is_simply_that_episode(self):
        kodi = _kodi('Voyager - S01E02 - Parallax.mkv', 1, 2)
        self.assertEqual(episode_numbers.match(kodi, self.VOYAGER)['id'], 2)

    def test_title_that_matches_nothing_falls_back_to_the_number(self):
        kodi = _kodi('Voyager - S01E03 - Some Release Tag.mkv', 1, 3)
        self.assertEqual(episode_numbers.match(kodi, self.VOYAGER)['id'], 3)

    def test_ambiguous_title_falls_back_to_the_number_never_a_guess(self):
        eps = [_ep(1, 1, 1, 'Broken Bow (1)'), _ep(2, 1, 2, 'Broken Bow (2)')]
        kodi = _kodi('Enterprise - S01E02 - Broken Bow.mkv', 1, 2)
        self.assertEqual(episode_numbers.match(kodi, eps)['id'], 2)

    def test_multi_episode_file_matches_each_entry_by_its_own_number(self):
        eps = [_ep(1, 1, 1, 'Broken Bow'), _ep(2, 1, 2, 'Broken Bow, Part II')]
        f = 'Enterprise - S01E01-E02 - Broken Bow.mkv'
        self.assertEqual(episode_numbers.match(_kodi(f, 1, 1), eps)['id'], 1)
        self.assertEqual(episode_numbers.match(_kodi(f, 1, 2), eps)['id'], 2)

    def test_title_match_stays_inside_the_files_season(self):
        eps = [_ep(1, 1, 1, 'Pilot'), _ep(2, 2, 1, 'Pilot')]
        kodi = _kodi('Show - S02E01 - Pilot.mkv', 2, 1)
        self.assertEqual(episode_numbers.match(kodi, eps)['id'], 2)

    def test_nothing_matching_at_all_is_none(self):
        self.assertIsNone(episode_numbers.match(_kodi('Show - S09E09 - Nope.mkv', 9, 9), self.VOYAGER))


if __name__ == '__main__':
    unittest.main()
