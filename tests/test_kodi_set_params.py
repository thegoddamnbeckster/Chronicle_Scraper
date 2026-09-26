# -*- coding: utf-8 -*-
"""
Every key a diff_* function can put in a VideoLibrary.Set*Details call must be a parameter Kodi really
has. Kodi rejects the WHOLE call for one unknown parameter, discarding every other correction in it --
which is exactly how a movie's "cast" (v3.17.11) and a show's "year" silently stopped corrections from
reaching the device. The parameter lists below are Kodi's own (JSONRPC.Introspect, Kodi 21, 2026-09-26).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib import full_sync_check as fsc
from lib import watch_rating_sync as w

SET_MOVIE = set('''movieid title playcount runtime director studio year plot genre rating mpaa imdbnumber votes
lastplayed originaltitle trailer tagline plotoutline writer country top250 sorttitle set showlink thumbnail
fanart tag art resume userrating ratings dateadded premiered uniqueid'''.split())
SET_EPISODE = set('''episodeid title playcount runtime director plot rating votes lastplayed writer firstaired
productioncode season episode originaltitle thumbnail fanart art resume userrating ratings dateadded
uniqueid'''.split())
SET_TVSHOW = set('''tvshowid title playcount studio plot genre rating mpaa imdbnumber premiered votes lastplayed
originaltitle sorttitle episodeguide thumbnail fanart tag art userrating ratings dateadded runtime status
uniqueid'''.split())


def _rich_movie_details():
    return {
        'title': 'T', 'year': 2012, 'overview': 'P', 'tagline': 'X', 'mpaa': 'R', 'premiered': '2012-08-03',
        'genres': ['Action'], 'crew': [{'name': 'D', 'job': 'Director'}], 'studio': 'S', 'country': 'US',
        'cast': [{'name': 'A', 'role': 'r', 'thumbUrl': 'u'}],
        'externalIds': {'imdb': 'tt1', 'tmdb': '2'},
    }


class TestOnlyRealKodiParameters(unittest.TestCase):

    def test_movie_updates_use_only_setmoviedetails_parameters(self):
        updates = fsc.diff_movie({'cast': [], 'genre': [], 'director': [], 'uniqueid': {}, 'art': {}}, _rich_movie_details())
        self.assertTrue(updates)
        self.assertLessEqual(set(updates), SET_MOVIE, set(updates) - SET_MOVIE)

    def test_episode_updates_use_only_setepisodedetails_parameters(self):
        details = {'title': 'T', 'overview': 'P', 'aired': '2026-01-18T00:00:00', 'season': 1, 'episode': 1,
                   'thumbUrl': 'u', 'userRating': 8}
        updates = w.diff_episode_text({'art': {}}, details)
        self.assertTrue(updates)
        self.assertLessEqual(set(updates), SET_EPISODE, set(updates) - SET_EPISODE)

    def test_show_updates_use_only_settvshowdetails_parameters(self):
        details = {'title': 'T', 'year': 2026, 'overview': 'P', 'premiered': '2026-01-18T00:00:00', 'mpaa': 'TV-MA',
                   'genres': ['Drama'], 'studio': 'HBO'}
        updates = w.diff_show_text({}, details)
        self.assertTrue(updates)
        self.assertLessEqual(set(updates), SET_TVSHOW, set(updates) - SET_TVSHOW)

    def test_state_updates_use_only_real_parameters(self):
        # playcount/lastplayed/resume/userrating -- the keys _build_state_updates can emit.
        for key in ('userrating', 'playcount', 'lastplayed', 'resume'):
            self.assertIn(key, SET_MOVIE)
            self.assertIn(key, SET_EPISODE)


if __name__ == '__main__':
    unittest.main()
