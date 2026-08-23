# -*- coding: utf-8 -*-
"""Diagnostic: drive get_details()/find_show()/get_episode_details() end to
end with realistic ChronicleClient responses, since no existing test
actually exercises these full functions (only isolated helpers)."""

import sys
from unittest.mock import MagicMock


def _import_tvshow_scraper():
    sys.path.insert(0, 'python')
    if 'tvshow_scraper' in sys.modules:
        del sys.modules['tvshow_scraper']
    import tvshow_scraper
    return tvshow_scraper


def _use_permissive_listitem(tvshow_scraper, monkeypatch):
    """conftest's fake ListItem/InfoTagVideo only implement the methods
    existing tests happen to call -- swap in MagicMocks so a NEW code path
    isn't rejected by an incomplete test double rather than a real bug in
    tvshow_scraper.py itself."""
    def factory(*a, **k):
        item = MagicMock()
        item.getVideoInfoTag.return_value = MagicMock()
        return item
    monkeypatch.setattr(tvshow_scraper.xbmcgui, 'ListItem', factory)


REALISTIC_SHOW_DETAILS = {
    'title': 'Test Show',
    'overview': 'An overview',
    'tagline': None,
    'year': 2020,
    'premiered': '2020-01-01',
    'mpaa': None,
    'country': None,
    'studio': 'Test Network',
    'status': 'Ended',
    'runtimeMinutes': 30,
    'genres': ['Drama'],
    'cast': [{'name': 'Actor One', 'role': 'Character'}],
    'crew': [],
    'tags': [],
    'ratings': {'tmdb': {'rating': 8.1, 'votes': 100}},
    'trailerUrl': None,
    'externalIds': {'imdb': 'tt1234567', 'tvdb': None, 'tmdb': '12345', 'trakt': None},
    'artwork': {
        'poster': [{'url': 'http://img/poster.jpg', 'source': 'tmdb'}],
        'fanart': [{'url': 'http://img/fanart.jpg', 'source': 'tmdb'}],
    },
    'seasons': [
        {
            'id': 501, 'number': 1, 'name': 'Season 1',
            'posterUrl': 'http://img/s1-poster.jpg',
            'artwork': {'poster': [{'url': 'http://img/s1-poster.jpg', 'source': 'tmdb'}]},
        },
        {
            'id': 502, 'number': 2, 'name': 'Season 2',
            'posterUrl': 'http://img/s2-poster.jpg',
            'artwork': {'poster': [{'url': 'http://img/s2-poster.jpg', 'source': 'tmdb'}]},
        },
    ],
}


def test_get_details_end_to_end_does_not_raise(kodi, monkeypatch):
    tvshow_scraper = _import_tvshow_scraper()
    _use_permissive_listitem(tvshow_scraper, monkeypatch)

    monkeypatch.setattr(
        tvshow_scraper.ChronicleClient, 'get_show_details',
        lambda self, show_id: REALISTIC_SHOW_DETAILS)
    monkeypatch.setattr(
        tvshow_scraper, 'find_show_location',
        lambda title, year: ('/media/tv/Test Show/', 42))
    monkeypatch.setattr(tvshow_scraper.legacy_nfo, 'load_and_clear_stash', lambda key: None)
    monkeypatch.setattr(tvshow_scraper.ChronicleClient, 'contribute_metadata', lambda *a, **k: None)

    result = tvshow_scraper.get_details(999, handle=1)
    assert result is True


def test_find_show_end_to_end_does_not_raise(kodi, monkeypatch):
    tvshow_scraper = _import_tvshow_scraper()
    _use_permissive_listitem(tvshow_scraper, monkeypatch)
    monkeypatch.setattr(
        tvshow_scraper.ChronicleClient, 'search_show',
        lambda self, title, year: {'id': 999, 'title': 'Test Show', 'year': 2020,
                                    'posterUrl': 'http://img/poster.jpg'})
    tvshow_scraper.find_show('Test Show', '2020', handle=1)


def test_get_episode_details_end_to_end_does_not_raise(kodi, monkeypatch):
    tvshow_scraper = _import_tvshow_scraper()
    _use_permissive_listitem(tvshow_scraper, monkeypatch)
    episode_details = {
        'title': 'Pilot', 'overview': 'First episode', 'season': 1, 'episode': 1,
        'year': 2020, 'aired': '2020-01-01', 'runtimeMinutes': 30,
        'cast': [], 'crew': [], 'ratings': {},
        'thumbUrl': 'http://img/ep1-thumb.jpg',
        'externalIds': {}, 'showTitle': 'Test Show', 'showYear': 2020,
        'artwork': {'thumb': [{'url': 'http://img/ep1-thumb.jpg', 'source': 'tmdb'}]},
    }
    monkeypatch.setattr(
        tvshow_scraper.ChronicleClient, 'get_episode_details',
        lambda self, episode_id: episode_details)
    monkeypatch.setattr(
        tvshow_scraper, 'find_show_location',
        lambda title, year: ('/media/tv/Test Show/', 42))
    monkeypatch.setattr(
        tvshow_scraper, 'get_episode',
        lambda tvshowid, season, episode: ('/media/tv/Test Show/S01E01.mkv', None))
    monkeypatch.setattr(tvshow_scraper.legacy_nfo, 'load_and_clear_stash', lambda key: None)
    monkeypatch.setattr(tvshow_scraper.ChronicleClient, 'contribute_metadata', lambda *a, **k: None)

    lookup = tvshow_scraper.build_lookup_string(123)
    result = tvshow_scraper.get_episode_details(lookup, handle=1)
    assert result is True
