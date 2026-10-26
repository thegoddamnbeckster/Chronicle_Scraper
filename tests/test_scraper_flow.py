# -*- coding: utf-8 -*-
"""python/scraper.py: Kodi's movie scraper actions (find, getdetails, getartwork), quick scan, and the dispatcher."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from python import scraper as sc


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.client = mock.MagicMock()
        self.addon = mock.MagicMock()
        self.addon.getSettingBool.return_value = True      # quick scan on (the default)
        self.items = []
        for obj, name, new in ((sc, 'ChronicleClient', mock.Mock(return_value=self.client)), (sc, 'ADDON', self.addon),
                               (sc.activity_tracker, 'mark_active', mock.Mock()),
                               (sc.xbmcplugin, 'setResolvedUrl', mock.Mock()), (sc.xbmcplugin, 'addDirectoryItem', mock.Mock()),
                               (sc.xbmcplugin, 'endOfDirectory', mock.Mock()),
                               (sc, 'find_movie_location', mock.Mock(return_value=(None, None, None, False, None))),
                               (sc, 'sync_movie_art', mock.Mock()),
                               (sc.xbmcgui, 'ListItem', mock.Mock(side_effect=self._make_item))):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)

    def _make_item(self, label='', offscreen=False):
        item = stubs._FakeListItem(label, offscreen)
        self.items.append(item)
        return item


class TestHelpers(_Base):
    def test_get_params(self):
        self.assertEqual(sc.get_params(['5']), {'handle': 5})
        self.assertEqual(sc.get_params(['5', '?action=find&title=Heat&year=1995']), {'handle': 5, 'action': 'find', 'title': 'Heat', 'year': '1995'})

    def test_lookup_string(self):
        self.assertEqual(sc.parse_lookup_string(sc.build_lookup_string(3)), 3)
        for bad in ('x', None, '[]'):
            self.assertIsNone(sc.parse_lookup_string(bad))

    def test_resolve_lookup_id_and_year(self):
        self.assertEqual(sc._resolve_lookup_id({'id': '4'}), 4)
        self.assertEqual(sc._resolve_lookup_id({'url': sc.build_lookup_string(6)}), 6)
        self.assertIsNone(sc._resolve_lookup_id({}))
        self.assertEqual((sc._parse_year('1995'), sc._parse_year(None), sc._parse_year('x')), (1995, None, None))

    def test_quick_scan_defaults_to_on_when_the_setting_cannot_be_read(self):
        self.addon.getSettingBool.side_effect = RuntimeError('settings broken')
        self.assertTrue(sc.quick_scan_enabled())
        self.addon.getSettingBool.side_effect = None
        self.addon.getSettingBool.return_value = False
        self.assertFalse(sc.quick_scan_enabled())


class TestFind(_Base):
    def test_the_files_basename_is_sent_so_a_known_file_wins_over_title_matching(self):
        sc.find_movie_location.return_value = ('smb://n/Alien - Derelict/', 'Alien - Derelict', 'Alien - Derelict.mkv', True, None)
        self.client.search_movie.return_value = {'id': 5, 'title': 'Derelict', 'year': 2017, 'posterUrl': 'http://c/p.jpg'}
        sc.search_for_movie('Alien - Derelict', 2017, 3)
        self.client.search_movie.assert_called_once_with('Alien - Derelict', 2017, filename='Alien - Derelict.mkv')
        kwargs = sc.xbmcplugin.addDirectoryItem.call_args[1]
        self.assertEqual(json.loads(kwargs['url']), {'chronicleId': 5})
        self.assertEqual(self.items[0].label, 'Derelict (2017)')
        sc.find_movie_location.assert_called_once_with('Alien - Derelict', 2017, library_retries=1)

    def test_a_failing_location_precheck_still_searches_by_title(self):
        sc.find_movie_location.side_effect = RuntimeError('share offline')
        self.client.search_movie.return_value = {'id': 5}
        sc.search_for_movie('Heat', 1995, 3)
        self.client.search_movie.assert_called_once_with('Heat', 1995, filename=None)
        sc.xbmcplugin.addDirectoryItem.assert_called_once()

    def test_no_candidate_offers_nothing(self):
        self.client.search_movie.return_value = None
        sc.search_for_movie('Heat', 1995, 3)
        sc.xbmcplugin.addDirectoryItem.assert_not_called()


class TestGetDetails(_Base):
    DETAILS = {'title': 'Heat', 'year': 1995, 'tagline': 'tag', 'runtimeMinutes': 170, 'knownFileName': None,
               'crew': [{'name': 'Mann', 'job': 'Director'}, {'name': 'W', 'job': 'Screenplay'}, 'junk'],
               'collection': {'name': 'Heat Collection', 'overview': 'o', 'posterUrl': 'http://c/sp.jpg', 'backdropUrl': 'http://c/sb.jpg'},
               'ratings': {}, 'artwork': {'poster': [{'url': 'http://c/p.jpg'}]}, 'userRating': 9,
               'cast': [{'name': 'Al', 'role': 'Vincent'}], 'trailerUrl': 'https://youtu.be/abcdef123'}

    def _run(self, quick=True, **overrides):
        self.addon.getSettingBool.return_value = quick
        self.client.get_movie_details.return_value = dict(self.DETAILS, **overrides)
        return sc.get_details(5, 3)

    def test_fields_collection_trailer_and_cast_reach_kodi(self):
        self.assertTrue(self._run())
        vtag = self.items[0].getVideoInfoTag()
        vtag.setSet.assert_called_with('Heat Collection')
        vtag.setDirectors.assert_called_with(['Mann'])
        vtag.setWriters.assert_called_with(['W'])
        vtag.setDuration.assert_called_with(10200)
        vtag.setUserRating.assert_called_with(9)
        vtag.setTrailer.assert_called_once()
        self.assertEqual(vtag.setCast.call_args[0][0][0].name, 'Al')
        sc.xbmcplugin.setResolvedUrl.assert_called_once()

    def test_quick_scan_defers_art_files_and_watch_sync(self):
        with mock.patch.object(sc.progress_sync, 'lookup_movie_state') as lookup:
            self._run(quick=True)
        lookup.assert_not_called()
        sc.sync_movie_art.assert_not_called()
        sc.find_movie_location.assert_called_once()
        self.assertEqual(sc.find_movie_location.call_args[1]['library_retries'], 1)

    def test_a_full_scan_syncs_art_and_reconciles_watch_state(self):
        with mock.patch.object(sc.progress_sync, 'lookup_movie_state', return_value=None):
            self._run(quick=False, isWatched=True, lastWatchedAt='2020-06-01T00:00:00Z', resumePositionPercent=None)
        sc.sync_movie_art.assert_called_once()
        self.items[0].getVideoInfoTag().setPlaycount.assert_called_with(1)

    def test_a_resume_point_is_pushed_and_kodis_newer_state_pulled(self):
        with mock.patch.object(sc.progress_sync, 'lookup_movie_state', return_value=None):
            self._run(quick=False, resumePositionPercent=25.0, resumeUpdatedAt='2020-06-01T00:00:00Z')
        self.items[0].getVideoInfoTag().setResumePoint.assert_called_with(2550.0, 10200)
        kodi = {'playcount': 0, 'lastplayed': '2030-01-01 12:00:00', 'resume': {'position': 500, 'total': 1000}}
        with mock.patch.object(sc.progress_sync, 'lookup_movie_state', return_value=kodi):
            self._run(quick=False, resumePositionPercent=10.0, resumeUpdatedAt='2020-06-01T00:00:00Z')
        self.client.push_resume.assert_called_once()
        self.assertEqual(self.client.push_resume.call_args[0][:2], (5, 50.0))

    def test_a_chronicle_reset_clears_and_a_kodi_watch_is_pulled(self):
        kodi = {'playcount': 1, 'lastplayed': '2020-01-01 12:00:00', 'resume': {}}
        with mock.patch.object(sc.progress_sync, 'lookup_movie_state', return_value=kodi):
            self._run(quick=False, isWatched=False, watchResetAt='2020-06-01T00:00:00Z')
            self.items[0].getVideoInfoTag().setPlaycount.assert_called_with(0)
            kodi['lastplayed'] = '2030-01-01 12:00:00'
            self._run(quick=False, isWatched=False)
        self.client.push_watched.assert_called_once()

    def test_a_leftover_bookmark_on_a_finished_movie_is_cleared(self):
        kodi = {'playcount': 1, 'lastplayed': '2020-06-01 18:00:00', 'resume': {'position': 500, 'total': 1000}}
        with mock.patch.object(sc.progress_sync, 'lookup_movie_state', return_value=kodi):
            self._run(quick=False, isWatched=True, lastWatchedAt='2020-06-01T12:00:00Z')
        self.items[0].getVideoInfoTag().setResumePoint.assert_called_with(0, 0)

    def test_a_file_found_by_title_is_reported_back_and_the_kodi_id_is_reported(self):
        sc.find_movie_location.return_value = ('smb://n/Heat/', 'Heat', 'Heat.mkv', True, 77)
        self._run()
        self.client.report_resolved_file.assert_called_once_with(5, 'Heat.mkv')
        self.client.report_kodi_id.assert_called_once_with(5, 'movie', 77)

    def test_a_known_file_is_not_reported_again(self):
        sc.find_movie_location.return_value = ('smb://n/Heat/', 'Heat', 'Heat.mkv', False, None)
        self._run(knownFileName='Heat.mkv')
        self.client.report_resolved_file.assert_not_called()
        self.client.report_kodi_id.assert_not_called()

    def test_a_missing_year_falls_back_to_the_year_in_the_file_name(self):
        sc.find_movie_location.return_value = ('smb://n/Fan Edit/', 'Fan Edit (2014)', 'Fan Edit (2014).mkv', False, None)
        self._run(year=None)
        self.items[0].getVideoInfoTag().setYear.assert_called_with(2014)

    def test_unknown_item(self):
        self.assertFalse(sc.get_details(None, 3))
        self.client.get_movie_details.return_value = None
        self.assertFalse(sc.get_details(5, 3))
        sc.xbmcplugin.setResolvedUrl.assert_not_called()


class TestGetArtwork(_Base):
    def test_art_is_offered_and_written(self):
        self.client.get_movie_details.return_value = {'title': 'Heat', 'year': 1995, 'artwork': {'poster': [{'url': 'http://c/p.jpg'}]},
                                                      'collection': {'name': 'C', 'posterUrl': 'http://c/sp.jpg', 'backdropUrl': 'http://c/sb.jpg'}}
        self.assertTrue(sc.get_artwork(5, 3))
        sc.sync_movie_art.assert_called_once()
        sc.xbmcplugin.setResolvedUrl.assert_called_once()

    def test_unknown(self):
        self.assertFalse(sc.get_artwork(None, 3))
        self.client.get_movie_details.return_value = None
        self.assertFalse(sc.get_artwork(5, 3))


class TestRun(_Base):
    def _run(self, query):
        with mock.patch.object(sc.sys, 'argv', ['plugin', '3', query]):
            sc.run()

    def test_find_always_ends_the_directory(self):
        with mock.patch.object(sc, 'search_for_movie') as find:
            self._run('?action=find&title=Heat&year=1995')
        find.assert_called_once_with('Heat', 1995, 3)
        sc.xbmcplugin.endOfDirectory.assert_called_once_with(3)

    def test_a_successful_getdetails_does_not_end_the_directory_but_a_failed_one_does(self):
        with mock.patch.object(sc, 'get_details', return_value=True) as details:
            self._run('?action=getdetails&url=%7B%22chronicleId%22%3A5%7D')
        details.assert_called_once_with(5, 3)
        sc.xbmcplugin.endOfDirectory.assert_not_called()
        with mock.patch.object(sc, 'get_details', return_value=False):
            self._run('?action=getdetails&url=%7B%22chronicleId%22%3A5%7D')
        sc.xbmcplugin.endOfDirectory.assert_called_once_with(3)

    def test_getartwork_and_unknown_actions(self):
        with mock.patch.object(sc, 'get_artwork', return_value=True) as artwork:
            self._run('?action=getartwork&id=5')
        artwork.assert_called_once_with(5, 3)
        sc.xbmcplugin.endOfDirectory.assert_not_called()
        self._run('?action=bogus')
        sc.xbmcplugin.endOfDirectory.assert_called_once_with(3)


if __name__ == '__main__':
    unittest.main()
