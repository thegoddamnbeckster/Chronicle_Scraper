# -*- coding: utf-8 -*-
"""
The periodic pass now syncs a movie's TEXT and ART along with its watched state and rating, in the same
visit. Before, only the post-scan check compared text, so a movie whose Chronicle data was corrected
after that check ran ("Total Recall (2012).mkv" still showing the 1990 film's plot and cast) stayed wrong
on the device until a later library scan happened to re-run it.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib import watch_rating_sync as w


class FakeClient:
    def __init__(self, details):
        self.details = details

    def search_movie(self, title, year, filename=None):
        return {'id': 896810}

    def get_movie_details(self, media_item_id):
        return self.details

    def report_kodi_id(self, *a):
        pass

    def push_resume(self, *a):
        pass

    def push_watched(self, *a):
        pass


KODI_MOVIE = {
    'movieid': 10890, 'title': 'Total Recall', 'year': 1990,
    'file': 'smb://nas/Movies/Total Recall (2012)/Total Recall (2012).mkv',
    'plot': "1990's plot.", 'cast': [{'name': 'Arnold Schwarzenegger'}], 'genre': [], 'director': [],
    'tagline': None, 'mpaa': None, 'premiered': None, 'imdbnumber': None, 'uniqueid': {}, 'art': {},
    'playcount': 0, 'lastplayed': '', 'resume': {'position': 0, 'total': 0}, 'userrating': 0,
}

DETAILS_2012 = {
    'title': 'Total Recall', 'year': 2012, 'overview': "2012's plot.",
    'cast': [{'name': 'Colin Farrell', 'role': 'Quaid'}], 'artwork': {'poster': [{'url': 'https://t/p.jpg'}]},
}


class TestMoviePass(unittest.TestCase):

    def test_state_listing_carries_the_text_fields_too(self):
        for prop in ('plot', 'cast', 'genre', 'art', 'uniqueid', 'file', 'playcount', 'lastplayed'):
            self.assertIn(prop, w._MOVIE_STATE_PROPERTIES)

    def test_a_visit_corrects_text_and_cast_and_syncs_art_in_one_go(self):
        applied = []
        with patch.object(w, '_set_movie_details', lambda mid, u: applied.append((mid, u))), \
             patch.object(w.movie_art_sync, 'sync_movie_art') as art, \
             patch.object(w, 'refresh_movie') as refresh:
            w._sync_one_movie(FakeClient(DETAILS_2012), {}, dict(KODI_MOVIE))

        self.assertEqual(len(applied), 1)
        _, updates = applied[0]
        self.assertEqual(updates['plot'], "2012's plot.")
        self.assertEqual(updates['year'], 2012)
        self.assertNotIn('cast', updates)  # Kodi rejects a whole SetMovieDetails call that carries cast
        refresh.assert_called_once_with(10890)  # cast is corrected by a re-scrape instead
        art.assert_called_once()
        self.assertEqual(art.call_args.kwargs['location'], ('smb://nas/Movies/Total Recall (2012)/',
                                                            'Total Recall (2012)'))

    def test_a_movie_that_already_matches_writes_nothing(self):
        movie = dict(KODI_MOVIE, year=2012, plot="2012's plot.", cast=[{'name': 'Colin Farrell'}])
        applied = []
        with patch.object(w, '_set_movie_details', lambda mid, u: applied.append(u)), \
             patch.object(w.movie_art_sync, 'sync_movie_art'):
            w._sync_one_movie(FakeClient(DETAILS_2012), {}, movie)

        self.assertEqual(applied, [])


class TestSetMembership(unittest.TestCase):

    def test_set_differs_only_when_chronicle_has_a_collection_and_kodis_set_is_different(self):
        from lib.full_sync_check import set_differs
        coll = {'collection': {'name': 'The Matrix Collection'}}
        self.assertTrue(set_differs({'set': ''}, coll))
        self.assertTrue(set_differs({'set': 'Something Else'}, coll))
        self.assertFalse(set_differs({'set': 'The Matrix Collection'}, coll))
        self.assertFalse(set_differs({'set': 'Whatever'}, {'collection': None}))  # never pulled out of a set

    def test_a_movie_missing_its_set_is_re_scraped_even_when_nothing_else_differs(self):
        movie = dict(KODI_MOVIE, year=2012, plot="2012's plot.", cast=[{'name': 'Colin Farrell'}], set='')
        details = dict(DETAILS_2012, collection={'name': 'Total Recall Collection'})
        with patch.object(w, '_set_movie_details'), patch.object(w.movie_art_sync, 'sync_movie_art'), \
             patch.object(w, 'refresh_movie') as refresh:
            w._sync_one_movie(FakeClient(details), {}, movie)

        refresh.assert_called_once_with(10890)


if __name__ == '__main__':
    unittest.main()
