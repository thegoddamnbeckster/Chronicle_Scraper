# -*- coding: utf-8 -*-
"""
Regression tests for get_episode_details() actually setting every field Chronicle's
/tv/episode-details response carries that Kodi's InfoTagVideo has a home for.

Root-caused live (2026-09-14): the episode's air date (Aired, in the response) was never read
at all -- get_episode_details() set title/plot/year/season/episode/cast/directors/writers/
ratings/ids/thumb, but nothing ever called setPremiered() for it, unlike the show- and
movie-level scrapes which already do. Runtime had the same gap in a narrower form: it only
reached InfoTagVideo as setResumePoint()'s own totaltime argument, which only runs when there's
an actual resume position being pushed -- an episode never played on this device got no runtime
set at all, unlike movies/shows which call setDuration() unconditionally.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from python import tvshow_scraper


def _episode_details(**overrides):
    base = {
        'title': 'Part One: Master and Apprentice',
        'overview': 'Ahsoka investigates a threat.',
        'season': 1,
        'episode': 1,
        'year': 2023,
        'aired': '2023-08-22T00:00:00Z',
        'runtimeMinutes': 40,
        'cast': None,
        'crew': None,
        'ratings': {},
        'thumbUrl': 'https://image.tmdb.org/t/p/w500/fake.jpg',
        'externalIds': {'imdb': None, 'tvdb': None, 'tmdb': '2552685', 'trakt': None},
        'showTitle': None,
        'showYear': None,
        'userRating': None,
        'resumePositionPercent': None,
        'resumeUpdatedAt': None,
        'isWatched': False,
        'lastWatchedAt': None,
    }
    base.update(overrides)
    return base


class TestEpisodeDetailsFields(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        tvshow_scraper.xbmcplugin.setResolvedUrl.reset_mock()

    def _run(self, details):
        mock_client = MagicMock()
        mock_client.get_episode_details.return_value = details
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.get_episode_details(
                tvshow_scraper.build_lookup_string(473792), handle=1)
        self.assertTrue(result)
        listitem = tvshow_scraper.xbmcplugin.setResolvedUrl.call_args.kwargs['listitem']
        return listitem.getVideoInfoTag()

    def test_air_date_is_set_from_aired(self):
        vtag = self._run(_episode_details(aired='2023-08-22T00:00:00Z'))
        vtag.setPremiered.assert_called_once_with('2023-08-22')

    def test_air_date_with_no_time_component_still_works(self):
        vtag = self._run(_episode_details(aired='2023-08-22'))
        vtag.setPremiered.assert_called_once_with('2023-08-22')

    def test_missing_aired_never_calls_setpremiered(self):
        vtag = self._run(_episode_details(aired=None))
        vtag.setPremiered.assert_not_called()

    def test_runtime_sets_duration_even_with_no_resume_position(self):
        # The exact gap this test locks in: no resumePositionPercent means
        # progress_sync's own resume-push path never runs, so setDuration() here is the ONLY
        # way this episode's runtime reaches Kodi at all.
        vtag = self._run(_episode_details(runtimeMinutes=40, resumePositionPercent=None))
        vtag.setDuration.assert_called_once_with(40 * 60)

    def test_missing_runtime_never_calls_setduration(self):
        vtag = self._run(_episode_details(runtimeMinutes=None))
        vtag.setDuration.assert_not_called()


if __name__ == '__main__':
    unittest.main()
