# -*- coding: utf-8 -*-
"""
watch_rating_sync._resolve_show_id -- look a Kodi show up by its own provider ids before falling back
to the title+year search. That search is resolve-OR-CREATE: Kodi's year for "A Knight of the Seven
Kingdoms" was another show's (2017), so it minted a duplicate empty show and never found the real one
(confirmed live 2026-09-26). "Alien: Earth" was cross-mapped to "Friends" the same way.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib import watch_rating_sync as w


class FakeClient:
    def __init__(self, by_id=None, search=None):
        self.by_id = by_id or {}
        self.search = search
        self.calls = []

    def resolve_show_by_external_id(self, source, external_id):
        self.calls.append(('resolve', source, external_id))
        return self.by_id.get((source, external_id))

    def search_show(self, title, year):
        self.calls.append(('search', title, year))
        return self.search


KNIGHT = {'tvshowid': 254, 'title': 'A Knight of the Seven Kingdoms', 'year': 2017,
          'uniqueid': {'imdb': 'tt27497448', 'tmdb': '224372', 'tvdb': '433631'}}


class TestShowExternalIds(unittest.TestCase):

    def test_pairs_use_chronicles_own_id_formats(self):
        self.assertEqual(w._show_external_ids(KNIGHT), [
            ('tmdb', 'tv:224372'), ('imdb', 'tt27497448'), ('tvdb', '433631')])

    def test_no_uniqueid_means_no_pairs(self):
        self.assertEqual(w._show_external_ids({'tvshowid': 1}), [])


class TestResolveShowId(unittest.TestCase):

    def test_resolves_by_provider_id_and_never_searches_with_kodis_wrong_year(self):
        client = FakeClient(by_id={('tmdb', 'tv:224372'): {'id': 384971}})
        cache = {}

        self.assertEqual(w._resolve_show_id(client, cache, KNIGHT), 384971)
        self.assertEqual(cache['tvshow:254'], 384971)
        self.assertFalse([c for c in client.calls if c[0] == 'search'])

    def test_tries_the_next_id_when_the_first_is_unknown(self):
        client = FakeClient(by_id={('imdb', 'tt27497448'): {'id': 384971}})

        self.assertEqual(w._resolve_show_id(client, {}, KNIGHT), 384971)
        self.assertEqual([c[1] for c in client.calls], ['tmdb', 'imdb'])

    def test_falls_back_to_the_title_search_only_when_no_id_resolves(self):
        client = FakeClient(search={'id': 99})

        self.assertEqual(w._resolve_show_id(client, {}, KNIGHT), 99)
        self.assertIn(('search', 'A Knight of the Seven Kingdoms', 2017), client.calls)

    def test_a_cached_id_is_returned_without_any_lookup(self):
        client = FakeClient()
        self.assertEqual(w._resolve_show_id(client, {'tvshow:254': 7}, KNIGHT), 7)
        self.assertEqual(client.calls, [])


if __name__ == '__main__':
    unittest.main()
