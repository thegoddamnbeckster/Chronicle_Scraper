# -*- coding: utf-8 -*-
"""Tests for python/tvshow_scraper.py's _season_artwork() -- the graceful-
degradation fix for an addon running ahead of its Chronicle server (which
may only send a season's bare posterUrl, not yet the richer Artwork dict)."""

import sys


def _import_tvshow_scraper():
    sys.path.insert(0, 'python')
    import tvshow_scraper
    return tvshow_scraper


def test_season_artwork_prefers_full_artwork_dict_when_present(kodi):
    tvshow_scraper = _import_tvshow_scraper()
    season = {
        'number': 1,
        'posterUrl': 'http://old/fallback-poster.jpg',
        'artwork': {'poster': [{'url': 'http://new/real-poster.jpg'}]},
    }
    result = tvshow_scraper._season_artwork(season)
    assert result == {'poster': [{'url': 'http://new/real-poster.jpg'}]}


def test_season_artwork_falls_back_to_bare_poster_url(kodi):
    """The exact code-review finding: an old server (pre-v0.8.1) sends only
    posterUrl, no artwork dict at all -- this must not silently produce
    nothing."""
    tvshow_scraper = _import_tvshow_scraper()
    season = {'number': 5, 'posterUrl': 'http://old/fallback-poster.jpg'}
    result = tvshow_scraper._season_artwork(season)
    assert result == {'poster': [{'url': 'http://old/fallback-poster.jpg', 'source': None}]}


def test_season_artwork_falls_back_when_artwork_dict_is_empty(kodi):
    tvshow_scraper = _import_tvshow_scraper()
    season = {'number': 2, 'posterUrl': 'http://old/p.jpg', 'artwork': {}}
    result = tvshow_scraper._season_artwork(season)
    assert result == {'poster': [{'url': 'http://old/p.jpg', 'source': None}]}


def test_season_artwork_returns_none_when_neither_present(kodi):
    tvshow_scraper = _import_tvshow_scraper()
    season = {'number': 3}
    assert tvshow_scraper._season_artwork(season) is None
