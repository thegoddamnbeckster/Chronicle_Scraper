# -*- coding: utf-8 -*-
"""
Smoke tests for python/scraper.py's get_details()/get_artwork() -- the movie addon's core
find/getdetails scrape path had no direct test coverage before this file (only
activity_tracker.py, which it happens to use, was tested). Locks in that the scrape path (art
sync, rating/resume/watched reconciliation, ListItem population, setResolvedUrl) runs end to end
with no exception.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from python import scraper


_MOVIE_DETAILS = {
    'title': 'Dune: Part Two',
    'year': 2024,
    'tagline': 'Long live the fighters.',
    'runtimeMinutes': 166,
    'crew': [{'name': 'Denis Villeneuve', 'job': 'Director'}],
    'collection': None,
    'ratings': {},
    'artwork': {'poster': [{'url': 'https://example.com/poster.jpg'}]},
    'userRating': None,
    'resumePositionPercent': None,
    'resumeUpdatedAt': None,
    'isWatched': False,
    'lastWatchedAt': None,
    'cast': None,
    'knownFileName': None,
}


class TestGetDetailsSmoke(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()
        scraper.xbmcplugin.setResolvedUrl.reset_mock()

    def _run_get_details(self):
        mock_client = MagicMock()
        mock_client.get_movie_details.return_value = dict(_MOVIE_DETAILS)
        # The full (non-quick) path: quick scan skips the art/watch work this test locks in.
        scraper.ADDON.getSettingBool = MagicMock(side_effect=lambda key: key != 'quick_scan')
        with patch('python.scraper.ChronicleClient', return_value=mock_client), \
             patch('python.scraper.find_movie_location',
                   return_value=('/movies/Dune Part Two (2024)/', 'Dune Part Two (2024)',
                                 'Dune Part Two (2024).mkv', False, None)), \
             patch('python.scraper.sync_movie_art') as mock_sync_art:
            result = scraper.get_details(media_item_id=555, handle=1)
        return result, mock_sync_art

    def test_get_details_completes_and_resolves_the_listitem(self):
        result, mock_sync_art = self._run_get_details()

        self.assertTrue(result)
        scraper.xbmcplugin.setResolvedUrl.assert_called_once()
        mock_sync_art.assert_called_once()
        # getVideoInfoTag() returns an unconstrained MagicMock (see kodi_stubs._FakeListItem) --
        # it doesn't round-trip real state, so the meaningful assertion is that setTitle() was
        # actually invoked with Chronicle's title, not that a later getter echoes it back.
        listitem = scraper.xbmcplugin.setResolvedUrl.call_args.kwargs.get('listitem') \
            or scraper.xbmcplugin.setResolvedUrl.call_args.args[-1]
        vtag = listitem.getVideoInfoTag()
        vtag.setTitle.assert_called_once_with('Dune: Part Two')

    def test_get_details_returns_false_for_unresolvable_media_item_id(self):
        result, _ = (scraper.get_details(media_item_id=None, handle=1), None)
        self.assertFalse(result)
        scraper.xbmcplugin.setResolvedUrl.assert_not_called()


class TestGetDetailsKeepsProgress(unittest.TestCase):
    """The reconcile that rides along with a scrape must not overwrite progress made here (2026-10-25)."""
    FILE = '/movies/Dune Part Two (2024)/Dune Part Two (2024).mkv'

    def setUp(self):
        kodi_stubs.reset_vfs()
        scraper.xbmcplugin.setResolvedUrl.reset_mock()
        self.addCleanup(setattr, scraper.xbmc.Player, 'playing_file', None)
        self.client = MagicMock()

    def _run(self, kodi_state, **details):
        self.client.get_movie_details.return_value = dict(_MOVIE_DETAILS, **details)
        scraper.ADDON.getSettingBool = MagicMock(side_effect=lambda key: key != 'quick_scan')
        with patch('python.scraper.ChronicleClient', return_value=self.client),              patch('python.scraper.progress_sync.lookup_movie_state', return_value=kodi_state),              patch('python.scraper.find_movie_location',
                   return_value=('/movies/Dune Part Two (2024)/', 'Dune Part Two (2024)',
                                 'Dune Part Two (2024).mkv', False, None)),              patch('python.scraper.sync_movie_art'):
            scraper.get_details(media_item_id=555, handle=1)
        listitem = scraper.xbmcplugin.setResolvedUrl.call_args.kwargs.get('listitem')             or scraper.xbmcplugin.setResolvedUrl.call_args.args[-1]
        return listitem.getVideoInfoTag()

    def test_a_bookmark_made_here_after_chronicle_finished_is_kept_and_reported_not_wiped(self):
        state = {'playcount': 1, 'lastplayed': '2020-01-01 12:00:00', 'file': self.FILE,
                 'resume': {'position': 3000, 'total': 6000}}
        with patch('python.scraper.recent_play.load',
                   return_value={scraper.recent_play.norm(self.FILE): '2026-10-25 16:30:00'}):
            vtag = self._run(state, isWatched=True, lastWatchedAt='2020-06-01T12:00:00Z')
        vtag.setResumePoint.assert_not_called()
        vtag.setPlaycount.assert_not_called()
        self.assertEqual(self.client.push_resume.call_args[0][:2], (555, 50.0))

    def test_a_movie_that_is_playing_is_left_alone(self):
        state = {'playcount': 0, 'lastplayed': '', 'file': self.FILE, 'resume': {}}
        scraper.xbmc.Player.playing_file = self.FILE
        vtag = self._run(state, isWatched=True, lastWatchedAt='2020-06-01T12:00:00Z')
        vtag.setPlaycount.assert_not_called()
        vtag.setResumePoint.assert_not_called()
        self.client.push_resume.assert_not_called()
        self.client.push_watched.assert_not_called()

    def test_an_ordinary_finished_movie_is_still_marked_watched(self):
        state = {'playcount': 0, 'lastplayed': '', 'file': self.FILE, 'resume': {}}
        vtag = self._run(state, isWatched=True, lastWatchedAt='2020-06-01T12:00:00Z')
        vtag.setPlaycount.assert_called_with(1)


class TestGetArtworkSmoke(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_get_artwork_completes_without_error(self):
        mock_client = MagicMock()
        mock_client.get_movie_details.return_value = dict(_MOVIE_DETAILS)
        with patch('python.scraper.ChronicleClient', return_value=mock_client), \
             patch('python.scraper.sync_movie_art') as mock_sync_art:
            result = scraper.get_artwork(media_item_id=555, handle=1)
        self.assertTrue(result)
        mock_sync_art.assert_called_once()


if __name__ == '__main__':
    unittest.main()
