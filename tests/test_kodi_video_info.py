# -*- coding: utf-8 -*-
"""Tests for lib/kodi_video_info.py's apply_artwork() and the new
add_season_artwork_candidates() -- covers the code-review findings:
  - a None candidates list for any art type must not crash (previously
    unguarded in apply_artwork()'s fanart branch and generic loop, and in
    the season-level loops this module's helper replaces)
  - season-level 'fanart' candidates must never reach addAvailableArtwork()
    (InfoTagVideo has no per-season fanart-list setter as of Kodi 21) --
    they're silently ineffective there, not just untested
"""


def test_apply_artwork_pins_top_candidate_via_set_art(kodi):
    from lib.kodi_video_info import apply_artwork
    import xbmcgui
    item = xbmcgui.ListItem('Test Movie')
    apply_artwork(item, {'poster': [{'url': 'http://x/p1.jpg'}, {'url': 'http://x/p2.jpg'}]})
    assert item._art == {'poster': 'http://x/p1.jpg'}


def test_apply_artwork_offers_all_candidates_as_alternates(kodi):
    from lib.kodi_video_info import apply_artwork
    import xbmcgui
    item = xbmcgui.ListItem('Test Movie')
    apply_artwork(item, {'poster': [{'url': 'http://x/p1.jpg'}, {'url': 'http://x/p2.jpg'}]})
    vtag = item.getVideoInfoTag()
    urls = [u for u, t, k in vtag.available_artwork]
    assert 'http://x/p1.jpg' in urls and 'http://x/p2.jpg' in urls


def test_apply_artwork_fanart_goes_through_set_available_fanart(kodi):
    from lib.kodi_video_info import apply_artwork
    import xbmcgui
    item = xbmcgui.ListItem('Test Movie')
    apply_artwork(item, {'fanart': [{'url': 'http://x/f1.jpg'}]})
    assert item._fanart == [{'image': 'http://x/f1.jpg', 'preview': 'http://x/f1.jpg'}]
    # Must NOT also have gone through the generic addAvailableArtwork() path.
    assert item.getVideoInfoTag().available_artwork == []


def test_apply_artwork_handles_none_candidates_without_crashing(kodi):
    """The exact bug class code review flagged: a type present in the dict
    with value None (not an empty list) must not raise."""
    from lib.kodi_video_info import apply_artwork
    import xbmcgui
    item = xbmcgui.ListItem('Test Movie')
    apply_artwork(item, {'poster': None, 'fanart': None, 'banner': [{'url': 'http://x/b.jpg'}]})
    urls = [u for u, t, k in item.getVideoInfoTag().available_artwork]
    assert urls == ['http://x/b.jpg']


def test_apply_artwork_empty_dict_does_not_crash(kodi):
    from lib.kodi_video_info import apply_artwork
    import xbmcgui
    item = xbmcgui.ListItem('Test Movie')
    apply_artwork(item, {})  # falsy-but-not-None whole dict
    assert item._art == {}


def test_apply_artwork_none_whole_dict_does_not_crash(kodi):
    from lib.kodi_video_info import apply_artwork
    import xbmcgui
    item = xbmcgui.ListItem('Test Movie')
    apply_artwork(item, None)
    assert item._art == {}


def test_add_season_artwork_candidates_offers_poster(kodi):
    from lib.kodi_video_info import add_season_artwork_candidates
    import xbmcgui
    vtag = xbmcgui.ListItem('Show').getVideoInfoTag()
    add_season_artwork_candidates(vtag, {'poster': [{'url': 'http://x/s1.jpg'}]}, 1)
    assert vtag.available_artwork == [('http://x/s1.jpg', 'poster', {'season': 1})]


def test_add_season_artwork_candidates_skips_fanart(kodi):
    """Season-level fanart must never reach addAvailableArtwork() --
    confirmed there's no per-season fanart-list setter, so silently sending
    it through would look like it worked while never actually appearing in
    Kodi's picker."""
    from lib.kodi_video_info import add_season_artwork_candidates
    import xbmcgui
    vtag = xbmcgui.ListItem('Show').getVideoInfoTag()
    add_season_artwork_candidates(vtag, {'fanart': [{'url': 'http://x/f.jpg'}]}, 2)
    assert vtag.available_artwork == []


def test_add_season_artwork_candidates_handles_none_candidates(kodi):
    """The exact crash the code review found: {'poster': None} for a season
    must not raise TypeError."""
    from lib.kodi_video_info import add_season_artwork_candidates
    import xbmcgui
    vtag = xbmcgui.ListItem('Show').getVideoInfoTag()
    add_season_artwork_candidates(vtag, {'poster': None, 'fanart': None}, 3)
    assert vtag.available_artwork == []


def test_add_season_artwork_candidates_handles_none_and_empty_artwork_dict(kodi):
    from lib.kodi_video_info import add_season_artwork_candidates
    import xbmcgui
    vtag = xbmcgui.ListItem('Show').getVideoInfoTag()
    add_season_artwork_candidates(vtag, None, 1)
    add_season_artwork_candidates(vtag, {}, 1)
    assert vtag.available_artwork == []
