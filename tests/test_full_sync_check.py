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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc before import below

from lib.full_sync_check import diff_movie


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

    def test_poster_mismatch_uses_first_chronicle_candidate(self):
        kodi = _kodi_item(art={'poster': 'https://old.example/poster.jpg'})
        details = _chronicle_details(artwork={'poster': [
            {'url': 'https://new.example/poster.jpg', 'source': 'chronicle'},
            {'url': 'https://other.example/poster.jpg', 'source': 'tmdb'},
        ]})

        updates = diff_movie(kodi, details)

        self.assertEqual(updates['art']['poster'], 'https://new.example/poster.jpg')

    def test_matching_poster_not_included(self):
        kodi = _kodi_item(art={'poster': 'https://same.example/poster.jpg', 'fanart': 'https://same.example/fanart.jpg'})
        details = _chronicle_details(artwork={'poster': [
            {'url': 'https://same.example/poster.jpg', 'source': 'chronicle'},
        ]})

        updates = diff_movie(kodi, details)

        self.assertNotIn('art', updates)

    def test_poster_update_preserves_other_art_slots(self):
        kodi = _kodi_item(art={'poster': 'https://old.example/poster.jpg', 'fanart': 'https://keep.example/fanart.jpg'})
        details = _chronicle_details()

        updates = diff_movie(kodi, details)

        self.assertEqual(updates['art']['fanart'], 'https://keep.example/fanart.jpg')

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


if __name__ == '__main__':
    unittest.main()
