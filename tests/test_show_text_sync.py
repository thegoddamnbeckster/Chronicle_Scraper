# -*- coding: utf-8 -*-
"""
watch_rating_sync.diff_show_text -- the show-level counterpart of full_sync_check's movie/episode
diffs: a show scraped once and never corrected kept its old text forever. Only a field Chronicle has
a value for AND that differs is ever written; nothing is blanked and order-only list differences
are not differences.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib.watch_rating_sync import diff_show_text


def _kodi(**over):
    base = {'plot': 'Old plot.', 'premiered': '2023-03-09', 'mpaa': 'TV-MA',
            'genre': ['Mystery', 'Drama'], 'studio': ['Paramount+']}
    base.update(over)
    return base


class TestDiffShowText(unittest.TestCase):

    def test_everything_matching_is_no_update(self):
        details = {'overview': 'Old plot.', 'premiered': '2023-03-09T00:00:00', 'mpaa': 'TV-MA',
                   'genres': ['Drama', 'Mystery'], 'studio': 'Paramount+'}
        self.assertEqual(diff_show_text(_kodi(), details), {})

    def test_only_the_differing_fields_are_returned(self):
        updates = diff_show_text(_kodi(), {'overview': 'New plot.', 'mpaa': 'TV-MA', 'genres': ['Mystery', 'Drama']})
        self.assertEqual(updates, {'plot': 'New plot.'})

    def test_a_field_chronicle_has_nothing_for_is_never_blanked(self):
        self.assertEqual(diff_show_text(_kodi(), {'overview': None, 'genres': [], 'studio': None}), {})

    def test_genre_order_alone_is_not_a_difference(self):
        self.assertEqual(diff_show_text(_kodi(genre=['Drama', 'Mystery']), {'genres': ['Mystery', 'Drama']}), {})

    def test_premiered_is_compared_as_a_date(self):
        self.assertEqual(diff_show_text(_kodi(), {'premiered': '2024-01-05T00:00:00'}), {'premiered': '2024-01-05'})

    def test_a_wrong_year_is_corrected(self):
        # Kodi's year for "A Knight of the Seven Kingdoms" was another show's (2017), which also made the
        # sync's title+year lookup mint a duplicate empty show.
        self.assertEqual(diff_show_text(_kodi(year=2017), {'year': 2026}), {'year': 2026})
        self.assertEqual(diff_show_text(_kodi(year=2026), {'year': 2026}), {})

    def test_studio_is_written_as_a_list(self):
        self.assertEqual(diff_show_text(_kodi(), {'studio': 'Netflix'}), {'studio': ['Netflix']})


if __name__ == '__main__':
    unittest.main()
