# -*- coding: utf-8 -*-
"""Regression tests for lib/full_sync_check.py's diff_movie() -- the comparison that decides
whether a Kodi movie needs correcting against Chronicle's current data. Root-caused live
(2026-09-22): a "Ghostbusters (2016)" file was scraped with the wrong (1984) movie's data at
some point and never corrected since, because nothing ever compared Kodi's own already-scraped
data against Chronicle's current state. Per-user direction: only ever update a field that
actually differs -- never a blind overwrite -- so these tests focus on exactly that boundary.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc before import below

from lib import full_sync_check
from lib.full_sync_check import cast_differs, diff_movie, needs_cast_refresh, year_from_path


def _kodi_item(**overrides):
    base = {
        'title': 'Ghostbusters', 'year': 1984, 'plot': 'Old plot.', 'tagline': None,
        'mpaa': None, 'premiered': None, 'genre': [], 'director': [],
        'imdbnumber': 'tt0087332', 'uniqueid': {'imdb': 'tt0087332'},
        'art': {'poster': 'https://old.example/poster.jpg'},
    }
    base.update(overrides)
    return base


def _chronicle_details(**overrides):
    base = {
        'title': 'Ghostbusters', 'year': 2016, 'overview': 'New plot.', 'tagline': None,
        'mpaa': None, 'premiered': None, 'genres': [], 'crew': [],
        'externalIds': {'imdb': 'tt1289401', 'tmdb': '43074'},
        'artwork': {'poster': [{'url': 'https://new.example/poster.jpg', 'source': 'chronicle'}]},
    }
    base.update(overrides)
    return base


class TestDiffMovie(unittest.TestCase):

    def test_everything_matching_produces_no_updates(self):
        details = _chronicle_details(title='Ghostbusters', year=1984, overview='Old plot.',
                                       externalIds={'imdb': 'tt0087332', 'tmdb': None},
                                       artwork={'poster': [{'url': 'https://old.example/poster.jpg', 'source': 'chronicle'}]})
        kodi = _kodi_item()

        self.assertEqual(diff_movie(kodi, details), {})

    def test_wrong_year_and_title_detected(self):
        kodi = _kodi_item(title='Ghostbusters', year=1984, imdbnumber='tt0087332',
                           uniqueid={'imdb': 'tt0087332'})
        details = _chronicle_details()  # year=2016, imdb=tt1289401

        updates = diff_movie(kodi, details)

        self.assertEqual(updates['year'], 2016)
        self.assertEqual(updates['imdbnumber'], 'tt1289401')
        self.assertEqual(updates['uniqueid']['imdb'], 'tt1289401')
        self.assertEqual(updates['uniqueid']['tmdb'], '43074')

    def test_matching_title_not_included_in_updates(self):
        # Both sides already say "Ghostbusters" -- title must not appear even though other
        # fields differ, confirming this isn't a blanket overwrite.
        kodi = _kodi_item(title='Ghostbusters')
        details = _chronicle_details(title='Ghostbusters')

        updates = diff_movie(kodi, details)

        self.assertNotIn('title', updates)
        self.assertIn('year', updates)

    def test_chronicle_missing_field_never_blanks_kodi_value(self):
        # Chronicle has no tagline at all -- Kodi's own existing tagline (if any) must be left
        # alone, not cleared.
        kodi = _kodi_item()
        kodi['tagline'] = 'Some existing tagline'
        details = _chronicle_details(tagline=None)

        updates = diff_movie(kodi, details)

        self.assertNotIn('tagline', updates)

    def test_never_touches_art_regardless_of_poster_mismatch(self):
        # Regression test (2026-09-22, the day after this feature's first release): Kodi uses a
        # movie's own local "-poster.jpg" file unconditionally, before ever looking at anything
        # VideoLibrary.SetMovieDetails' own art parameter offers -- confirmed live, the
        # Ghostbusters (2016) fix landed for every other field but the poster kept showing the
        # wrong image regardless, because nothing had rewritten the local file. diff_movie must
        # never produce an 'art' key at all -- see run()'s own unconditional sync_movie_art()
        # call, the mechanism that actually reaches the screen.
        kodi = _kodi_item(art={'poster': 'https://old.example/poster.jpg'})
        details = _chronicle_details(artwork={'poster': [
            {'url': 'https://new.example/poster.jpg', 'source': 'chronicle'},
        ]})

        updates = diff_movie(kodi, details)

        self.assertNotIn('art', updates)

    def test_director_mismatch_detected_from_crew(self):
        kodi = _kodi_item(director=['Ivan Reitman'])
        details = _chronicle_details(crew=[
            {'name': 'Paul Feig', 'job': 'Director'},
            {'name': 'Katie Dippold', 'job': 'Screenplay'},
        ])

        updates = diff_movie(kodi, details)

        self.assertEqual(updates['director'], ['Paul Feig'])

    def test_genre_mismatch_detected(self):
        kodi = _kodi_item(genre=['Horror'])
        details = _chronicle_details(genres=['Action', 'Fantasy', 'Comedy'])

        updates = diff_movie(kodi, details)

        self.assertEqual(updates['genre'], ['Action', 'Fantasy', 'Comedy'])

    def test_genre_same_set_different_order_not_flagged(self):
        # Regression test (2026-09-22): Kodi's own array-typed properties aren't guaranteed to
        # read back in the same order they were written in -- an order-only difference isn't a
        # real one, and treating it as one would push a same-content "correction" every pass.
        kodi = _kodi_item(genre=['Comedy', 'Action', 'Fantasy'])
        details = _chronicle_details(genres=['Action', 'Fantasy', 'Comedy'])

        updates = diff_movie(kodi, details)

        self.assertNotIn('genre', updates)

    def test_director_job_match_is_case_insensitive(self):
        # Regression test (2026-09-22): the normal scrape path (python/scraper.py) matches crew
        # job case-insensitively; this must too, or a provider returning "director"/"DIRECTOR"
        # makes this feature silently never correct a wrong director for that item.
        kodi = _kodi_item(director=['Ivan Reitman'])
        details = _chronicle_details(crew=[{'name': 'Paul Feig', 'job': 'director'}])

        updates = diff_movie(kodi, details)

        self.assertEqual(updates['director'], ['Paul Feig'])

    def test_director_same_set_different_order_not_flagged(self):
        kodi = _kodi_item(director=['Paul Feig', 'Katie Dippold'])
        details = _chronicle_details(crew=[
            {'name': 'Katie Dippold', 'job': 'Director'},
            {'name': 'Paul Feig', 'job': 'Director'},
        ])

        updates = diff_movie(kodi, details)

        self.assertNotIn('director', updates)


class TestRunSyncsArtUnconditionally(unittest.TestCase):
    """run()'s own orchestration: sync_movie_art() must be called for every resolved movie,
    with a location derived straight from Kodi's own file path -- not gated behind diff_movie
    (which deliberately never reports an art difference; see TestDiffMovie's own doc above)."""

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_sync_movie_art_called_with_location_derived_from_kodi_file_path(self):
        movie = {
            'movieid': 42, 'title': 'Ghostbusters', 'year': 2016,
            'file': 'smb://nas/Movies/Ghostbusters (2016)/Ghostbusters (2016).mkv',
        }
        details = {
            'title': 'Ghostbusters', 'year': 2016,
            'artwork': {'poster': [{'url': 'https://new.example/poster.jpg', 'source': 'chronicle'}]},
        }

        with patch('lib.full_sync_check._get_all_movies', return_value=[movie]), \
             patch('lib.full_sync_check.ChronicleClient') as mock_client_cls, \
             patch('lib.full_sync_check.movie_art_sync.sync_movie_art') as mock_sync_art:
            mock_client = mock_client_cls.return_value
            mock_client.test_connection.return_value = (True, '')
            mock_client.get_movie_details_by_file.return_value = details

            full_sync_check.run()

        mock_sync_art.assert_called_once_with(
            'Ghostbusters', 2016, details['artwork'],
            location=('smb://nas/Movies/Ghostbusters (2016)/', 'Ghostbusters (2016)'))

    def test_sync_movie_art_still_called_when_every_other_field_already_matches(self):
        # The whole point: an already-correct movie (diff_movie returns {}, nothing pushed via
        # SetMovieDetails) must still get its art checked -- sync_movie_art's own skip-cache is
        # what keeps that cheap, not a decision made here.
        movie = {
            'movieid': 42, 'title': 'Ghostbusters', 'year': 2016,
            'file': 'smb://nas/Movies/Ghostbusters (2016)/Ghostbusters (2016).mkv',
        }
        details = {'title': 'Ghostbusters', 'year': 2016, 'artwork': {}}

        with patch('lib.full_sync_check._get_all_movies', return_value=[movie]), \
             patch('lib.full_sync_check.ChronicleClient') as mock_client_cls, \
             patch('lib.full_sync_check.movie_art_sync.sync_movie_art') as mock_sync_art:
            mock_client = mock_client_cls.return_value
            mock_client.test_connection.return_value = (True, '')
            mock_client.get_movie_details_by_file.return_value = details

            result = full_sync_check.run()

        mock_sync_art.assert_called_once()
        self.assertEqual(result['updated'], 0, "no SetMovieDetails field differed")

    def test_sync_movie_art_failure_does_not_stop_the_pass(self):
        movie = {
            'movieid': 42, 'title': 'Ghostbusters', 'year': 2016,
            'file': 'smb://nas/Movies/Ghostbusters (2016)/Ghostbusters (2016).mkv',
        }
        details = {'title': 'Ghostbusters', 'year': 2016, 'artwork': {}}

        with patch('lib.full_sync_check._get_all_movies', return_value=[movie]), \
             patch('lib.full_sync_check.ChronicleClient') as mock_client_cls, \
             patch('lib.full_sync_check.movie_art_sync.sync_movie_art', side_effect=RuntimeError('boom')):
            mock_client = mock_client_cls.return_value
            mock_client.test_connection.return_value = (True, '')
            mock_client.get_movie_details_by_file.return_value = details

            result = full_sync_check.run()

        self.assertEqual(result['checked'], 1, "the item is still counted as checked despite the art-sync failure")


class TestYearFromPath(unittest.TestCase):
    """Kodi's own year is only as good as the scrape that set it: "Total Recall (2012).mkv" was
    scraped as the 1990 film, so Kodi's 1990 would have made the by-file year guard reject the
    corrected Chronicle record. The file name's year is the independent witness."""

    def test_file_name_year(self):
        self.assertEqual(year_from_path('smb://n/Movies/Total Recall (2012)/Total Recall (2012).mkv'), 2012)
        self.assertEqual(year_from_path('smb://n/Movies/X/X [1990].mkv'), 1990)

    def test_falls_back_to_the_folder_then_none(self):
        self.assertEqual(year_from_path('smb://n/Movies/Total Recall (2012)/movie.mkv'), 2012)
        self.assertIsNone(year_from_path('smb://n/Movies/Total Recall/Total Recall.mkv'))
        self.assertIsNone(year_from_path(None))


class TestDiffMovieCastAndCompany(unittest.TestCase):

    def test_cast_is_never_in_the_setmoviedetails_params(self):
        # Kodi's SetMovieDetails has NO cast parameter: including one ("Too many parameters") made Kodi
        # reject the WHOLE call, discarding every other correction for that movie.
        kodi = _kodi_item(cast=[{'name': 'Arnold Schwarzenegger', 'role': 'Quaid', 'order': 0}])
        details = _chronicle_details(cast=[{'name': 'Colin Farrell', 'role': 'Quaid'}])
        self.assertNotIn('cast', diff_movie(kodi, details))

    def test_cast_differs_compares_name_sets_ignoring_order_and_case(self):
        self.assertTrue(cast_differs(_kodi_item(cast=[{'name': 'A'}]), {'cast': [{'name': 'B'}]}))
        self.assertFalse(cast_differs(_kodi_item(cast=[{'name': 'B'}, {'name': 'A'}]), {'cast': [{'name': 'a'}, {'name': 'b'}]}))
        self.assertFalse(cast_differs(_kodi_item(cast=[{'name': 'A'}]), {'cast': None}))

    def test_a_refresh_is_wanted_only_when_the_movie_itself_was_corrected_and_the_cast_differs(self):
        kodi = _kodi_item(cast=[{'name': 'A'}])
        details = {'cast': [{'name': 'B'}]}
        self.assertTrue(needs_cast_refresh(kodi, details, {'plot': 'new'}))
        self.assertTrue(needs_cast_refresh(kodi, details, {'year': 2012}))
        self.assertFalse(needs_cast_refresh(kodi, details, {'studio': ['X']}))  # not an identity change
        self.assertFalse(needs_cast_refresh(kodi, {'cast': [{'name': 'A'}]}, {'plot': 'new'}))  # cast already right

    def test_studio_and_country_are_written_as_lists_only_when_they_differ(self):
        kodi = _kodi_item(studio=['Carolco Pictures'], country=['United States of America'])
        details = _chronicle_details(studio='Columbia Pictures', country='United States of America')
        updates = diff_movie(kodi, details)
        self.assertEqual(updates['studio'], ['Columbia Pictures'])
        self.assertNotIn('country', updates)

    def test_nothing_chronicle_has_is_never_blanked(self):
        kodi = _kodi_item(cast=[{'name': 'A'}], studio=['S'], country=['C'])
        updates = diff_movie(kodi, _chronicle_details(cast=None, studio=None, country=None))
        for key in ('cast', 'studio', 'country'):
            self.assertNotIn(key, updates)


class TestImpossibleYear(unittest.TestCase):
    """Kodi reported 65535 as the year of a movie (its stored -1): the sync must replace it, from Chronicle
    when Chronicle has a year, else from the year in the file's own name."""

    def test_an_impossible_kodi_year_is_replaced_by_the_file_names_year_when_chronicle_has_none(self):
        kodi = _kodi_item(year=65535, file='smb://n/Movies/Captain America (2014)/Captain America (2014).mkv')
        self.assertEqual(diff_movie(kodi, _chronicle_details(year=None))['year'], 2014)

    def test_chronicles_year_wins_when_it_has_one(self):
        kodi = _kodi_item(year=65535, file='smb://n/Movies/X (2014)/X (2014).mkv')
        self.assertEqual(diff_movie(kodi, _chronicle_details(year=2016))['year'], 2016)

    def test_a_plausible_kodi_year_is_left_alone_when_chronicle_has_none(self):
        kodi = _kodi_item(year=1984, file='smb://n/Movies/X (2014)/X (2014).mkv')
        self.assertNotIn('year', diff_movie(kodi, _chronicle_details(year=None)))


if __name__ == '__main__':
    unittest.main()
