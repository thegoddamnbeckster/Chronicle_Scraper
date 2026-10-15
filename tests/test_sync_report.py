# -*- coding: utf-8 -*-
"""
The notification after a watch/rating pass (and the post-scan check) reports only what CHANGED -- never
how many items were merely visited -- and says nothing at all when nothing changed and nothing failed.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib import watch_rating_sync as w
from tests.test_movie_pass import FakeClient, KODI_MOVIE, DETAILS_2012

STRINGS = {
    32135: 'Updated {0}',
    32136: ' -- {0} error(s), see kodi.log',
    32162: '{0} movie(s)',
    32163: '{0} show(s)',
    32164: '{0} episode(s)',
    32166: 'Watch and rating sync hit {0} error(s), see kodi.log',
}


def get_string(string_id):
    return STRINGS[string_id]


def result(**overrides):
    base = {'movies': 120, 'shows': 40, 'episodes': 800, 'errors': 0, 'cancelled': False,
            'movies_changed': 0, 'shows_changed': 0, 'episodes_changed': 0}
    base.update(overrides)
    return base


class TestDescribeResult(unittest.TestCase):

    def test_a_pass_that_changed_nothing_says_nothing(self):
        self.assertIsNone(w.describe_result(result(), get_string))

    def test_only_what_changed_is_listed_and_the_visited_totals_never_appear(self):
        message = w.describe_result(result(movies_changed=3, episodes_changed=12), get_string)
        self.assertEqual(message, 'Updated 3 movie(s), 12 episode(s)')
        self.assertNotIn('120', message)
        self.assertNotIn('800', message)
        self.assertNotIn('show', message)

    def test_errors_are_appended_to_a_report_of_changes(self):
        message = w.describe_result(result(shows_changed=1, errors=2), get_string)
        self.assertEqual(message, 'Updated 1 show(s) -- 2 error(s), see kodi.log')

    def test_errors_alone_are_still_reported(self):
        self.assertEqual(w.describe_result(result(errors=4), get_string),
                         'Watch and rating sync hit 4 error(s), see kodi.log')


class TestChangeCounting(unittest.TestCase):

    def test_a_movie_that_needed_nothing_is_not_counted(self):
        in_sync = dict(KODI_MOVIE, year=2012, plot=DETAILS_2012['overview'],
                       cast=[{'name': 'Colin Farrell'}])
        with patch.object(w, '_set_movie_details') as set_details, \
             patch.object(w.movie_art_sync, 'sync_movie_art', return_value=False), \
             patch.object(w, 'refresh_movie'):
            changed = w._sync_one_movie(FakeClient(DETAILS_2012), {}, in_sync)

        self.assertFalse(changed)
        set_details.assert_not_called()

    def test_a_movie_whose_text_was_corrected_is_counted(self):
        with patch.object(w, '_set_movie_details'), \
             patch.object(w.movie_art_sync, 'sync_movie_art', return_value=False), \
             patch.object(w, 'refresh_movie'):
            self.assertTrue(w._sync_one_movie(FakeClient(DETAILS_2012), {}, dict(KODI_MOVIE)))

    def test_a_movie_whose_only_change_was_its_art_is_counted(self):
        in_sync = dict(KODI_MOVIE, year=2012, plot=DETAILS_2012['overview'],
                       cast=[{'name': 'Colin Farrell'}])
        with patch.object(w, '_set_movie_details'), \
             patch.object(w.movie_art_sync, 'sync_movie_art', return_value=True), \
             patch.object(w, 'refresh_movie'):
            self.assertTrue(w._sync_one_movie(FakeClient(DETAILS_2012), {}, in_sync))

    def test_run_totals_changed_separately_from_visited(self):
        movies = [dict(KODI_MOVIE, movieid=i) for i in (1, 2, 3)]
        outcomes = iter([True, False, True])
        with patch.object(w, 'ChronicleClient'), patch.object(w.media_id_cache, 'load', return_value={}), \
             patch.object(w.media_id_cache, 'save'), \
             patch.object(w, '_get_all_movies', return_value=movies), \
             patch.object(w, '_get_all_tvshows', return_value=[{'tvshowid': 7, 'title': 'S', 'year': 2020}]), \
             patch.object(w, '_sync_one_movie', side_effect=lambda *a: next(outcomes)), \
             patch.object(w, '_sync_one_show', return_value=(10, True, 4)), \
             patch.object(w.xbmc, 'sleep'):
            counts = w.run()

        self.assertEqual((counts['movies'], counts['shows'], counts['episodes']), (3, 1, 10))
        self.assertEqual((counts['movies_changed'], counts['shows_changed'], counts['episodes_changed']), (2, 1, 4))


if __name__ == '__main__':
    unittest.main()
