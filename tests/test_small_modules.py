# -*- coding: utf-8 -*-
"""device_registration, kodi_video_info, install_marker and status_display."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs
import xbmc

from lib import device_registration, install_marker, kodi_video_info, status_display


class TestDeviceRegistration(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        p = mock.patch.object(device_registration, 'ChronicleClient', mock.Mock(return_value=self.client))
        p.start()
        self.addCleanup(p.stop)

    def test_registers_by_name_only(self):
        with mock.patch.object(xbmc, 'getInfoLabel', return_value='Downstairs Shield'):
            device_registration.register()
        self.client.register_device.assert_called_once_with('Downstairs Shield')

    def test_a_blank_name_is_registered_as_kodi(self):
        with mock.patch.object(xbmc, 'getInfoLabel', return_value=''):
            device_registration.register()
        self.client.register_device.assert_called_once_with('Kodi')

    def test_nothing_about_the_device_address_or_login_is_read(self):
        with mock.patch.object(xbmc, 'getInfoLabel', return_value='Shield'), \
                mock.patch.object(xbmc, 'executeJSONRPC') as rpc:
            device_registration.register()
        rpc.assert_not_called()
        self.assertFalse(hasattr(device_registration, 'socket'))


class TestVideoInfo(unittest.TestCase):
    def test_youtube_trailer_uri(self):
        expected = 'plugin://plugin.video.youtube/?action=play_video&videoid=abc12345'
        self.assertEqual(kodi_video_info.youtube_trailer_uri('https://www.youtube.com/watch?v=abc12345'), expected)
        self.assertEqual(kodi_video_info.youtube_trailer_uri('https://youtu.be/abc12345'), expected)
        for bad in ('', None, 'https://vimeo.com/1', 'https://youtube.com/watch?v=x'):
            self.assertIsNone(kodi_video_info.youtube_trailer_uri(bad), bad)

    def test_common_info_sets_only_what_is_present(self):
        vtag = mock.MagicMock()
        kodi_video_info.apply_common_video_info(vtag, {'title': 'Heat', 'premiered': '1995-12-15T00:00:00Z', 'country': 'US',
                                                       'genres': ['Crime'], 'externalIds': {'tmdb': '949', 'trakt': '', 'imdb': None}})
        vtag.setTitle.assert_called_once_with('Heat')
        vtag.setPremiered.assert_called_once_with('1995-12-15')
        vtag.setCountries.assert_called_once_with(['US'])
        vtag.setGenres.assert_called_once_with(['Crime'])
        vtag.setUniqueIDs.assert_called_once_with({'tmdb': '949'}, 'tmdb')
        vtag.setPlot.assert_not_called()
        vtag.setStudios.assert_not_called()

    def test_imdb_is_the_default_unique_id_when_present(self):
        vtag = mock.MagicMock()
        kodi_video_info.apply_common_video_info(vtag, {'externalIds': {'tvdb': '5', 'imdb': 'tt1'}})
        self.assertEqual(vtag.setUniqueIDs.call_args[0][1], 'imdb')

    def test_empty_details_set_nothing(self):
        vtag = mock.MagicMock()
        kodi_video_info.apply_common_video_info(vtag, {})
        self.assertEqual(vtag.mock_calls, [])

    def test_only_the_first_rating_is_the_default_and_empty_ones_are_skipped(self):
        vtag = mock.MagicMock()
        kodi_video_info.apply_ratings(vtag, {'imdb': {'rating': 0}, 'tmdb': {'rating': 7.5, 'votes': 10}, 'trakt': {'rating': 8}})
        calls = vtag.setRating.call_args_list
        self.assertEqual([(c[0][0], c[1]['type'], c[1]['isdefault'], c[1]['votes']) for c in calls],
                         [(7.5, 'tmdb', True, 10), (8, 'trakt', False, 0)])
        kodi_video_info.apply_ratings(vtag, None)

    def test_artwork_pins_the_first_candidate_and_offers_all(self):
        item = stubs._FakeListItem()
        item.setAvailableFanart = mock.MagicMock()
        artwork = {'poster': [{'url': 'p1'}, {'url': 'p2'}], 'fanart': [{'url': 'f1'}], 'clearlogo': []}
        kodi_video_info.apply_artwork(item, artwork)
        self.assertEqual(item._art, {'poster': 'p1', 'fanart': 'f1'})
        item.setAvailableFanart.assert_called_once_with([{'image': 'f1', 'preview': 'f1'}])
        added = [(c[0][0], c[0][1]) for c in item.getVideoInfoTag().addAvailableArtwork.call_args_list]
        self.assertEqual(added, [('p1', 'poster'), ('p2', 'poster')])

    def test_no_artwork_leaves_existing_art_alone(self):
        item = stubs._FakeListItem()
        kodi_video_info.apply_artwork(item, {})
        kodi_video_info.apply_artwork(item, {'poster': []})
        self.assertEqual(item._art, {})


class TestInstallMarker(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()

    def test_first_start_of_a_version_only_once(self):
        self.assertTrue(install_marker.is_first_start_of_version('1.0'))
        self.assertFalse(install_marker.is_first_start_of_version('1.0'))
        self.assertTrue(install_marker.is_first_start_of_version('1.1'))   # an upgrade is fresh again
        self.assertFalse(install_marker.is_first_start_of_version('1.1'))

    def test_marker_failure_never_suppresses(self):
        with mock.patch.object(install_marker.xbmcvfs, 'exists', side_effect=OSError('vfs down')):
            self.assertFalse(install_marker.is_first_start_of_version('1.0'))


class TestStatusDisplay(unittest.TestCase):
    def test_priority_cleaning_then_scraper_then_scanning_then_nothing(self):
        t = status_display.status_text
        self.assertEqual(t(True, True, True, 'scraper', 'scan', 'clean'), 'clean')
        self.assertEqual(t(True, False, True, 'scraper', 'scan', 'clean'), 'scraper')
        self.assertEqual(t(True, False, False, 'scraper', 'scan', 'clean'), 'scan')
        self.assertIsNone(t(False, False, False, 'scraper', 'scan', 'clean'))


if __name__ == '__main__':
    unittest.main()
