# -*- coding: utf-8 -*-
"""Movie full_sync_check.run(): the post-scan pass over every movie Kodi holds."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import full_sync_check as fsc


class TestRpcHelpers(unittest.TestCase):
    def _rpc(self, payload=None, exc=None):
        if exc:
            return mock.patch.object(xbmc, 'executeJSONRPC', side_effect=exc)
        return mock.patch.object(xbmc, 'executeJSONRPC', return_value=json.dumps(payload))

    def test_get_all_movies(self):
        with self._rpc({'result': {'movies': [{'movieid': 1}]}}):
            self.assertEqual(fsc._get_all_movies(), [{'movieid': 1}])
        with self._rpc({'result': {}}):
            self.assertEqual(fsc._get_all_movies(), [])
        with self._rpc({'error': {'message': 'no'}}):
            self.assertEqual(fsc._get_all_movies(), [])
        with self._rpc(exc=RuntimeError('down')):
            self.assertEqual(fsc._get_all_movies(), [])

    def test_set_movie_details_and_refresh(self):
        with self._rpc({'result': 'OK'}) as rpc:
            self.assertTrue(fsc._set_movie_details(5, {'title': 'T'}))
            self.assertEqual(json.loads(rpc.call_args[0][0])['params'], {'title': 'T', 'movieid': 5})
            self.assertTrue(fsc.refresh_movie(5))
            self.assertEqual(json.loads(rpc.call_args[0][0])['params'], {'movieid': 5, 'ignorenfo': True})
        with self._rpc({'error': {}}):
            self.assertFalse(fsc._set_movie_details(5, {}))
            self.assertFalse(fsc.refresh_movie(5))
        with self._rpc(exc=RuntimeError('down')):
            self.assertFalse(fsc._set_movie_details(5, {}))
            self.assertFalse(fsc.refresh_movie(5))

    def test_cast_differs(self):
        kodi = {'cast': [{'name': 'Al Pacino'}, {'name': 'Robert De Niro'}]}
        self.assertFalse(fsc.cast_differs(kodi, {'cast': [{'name': 'robert de niro'}, {'name': 'AL PACINO'}]}))
        self.assertTrue(fsc.cast_differs(kodi, {'cast': [{'name': 'Al Pacino'}]}))
        self.assertFalse(fsc.cast_differs(kodi, {'cast': []}))          # Chronicle has none: never blank Kodi's
        self.assertFalse(fsc.cast_differs(kodi, {}))

    def test_years_in_path(self):
        self.assertEqual(sorted(fsc.years_in_path('smb://n/Movies/Defrosted Edition (2017)/Movie (2014).mkv')), [2014, 2017])
        self.assertEqual(fsc.years_in_path('smb://n/Movies/Heat/Heat.mkv'), [])
        self.assertEqual(fsc.years_in_path(''), [])


class TestRun(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.client.test_connection.return_value = (True, '')
        self.movies = [
            {'movieid': 1, 'title': 'Heat', 'year': 1995, 'file': 'smb://n/Movies/Heat (1995)/Heat (1995).mkv'},
            {'movieid': 2, 'title': 'NoFile', 'year': 2000},
            {'movieid': 3, 'title': 'Unknown', 'year': 2001, 'file': 'smb://n/Movies/Unknown (2001)/Unknown (2001).mkv'},
        ]
        for name, new in (('ChronicleClient', mock.Mock(return_value=self.client)),
                          ('_get_all_movies', mock.Mock(side_effect=lambda: self.movies)),
                          ('diff_movie', mock.Mock(return_value={})),
                          ('needs_cast_refresh', mock.Mock(return_value=False)),
                          ('set_differs', mock.Mock(return_value=False)),
                          ('_set_movie_details', mock.Mock(return_value=True)),
                          ('refresh_movie', mock.Mock())):
            p = mock.patch.object(fsc, name, new)
            setattr(self, name.strip('_'), p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(fsc.movie_art_sync, 'sync_movie_art')
        self.art = p.start()
        self.addCleanup(p.stop)
        self.client.get_movie_details_by_file.side_effect = lambda name, year=None: (
            {'title': 'Heat', 'year': 1995, 'artwork': {}} if name.startswith('Heat') else None)

    def test_an_unreachable_chronicle_aborts_instead_of_looking_clean(self):
        self.client.test_connection.return_value = (False, 'refused')
        result = fsc.run()
        self.assertEqual((result['checked'], result['aborted']), (0, 'Chronicle not reachable: refused'))
        self.client.get_movie_details_by_file.assert_not_called()

    def test_files_are_resolved_by_basename_with_the_files_year(self):
        result = fsc.run()
        self.client.get_movie_details_by_file.assert_any_call('Heat (1995).mkv', year=1995)
        self.assertEqual((result['checked'], result['updated'], result['errors'], result['aborted']), (1, 0, 0, None))

    def test_art_is_synced_from_the_known_location(self):
        fsc.run()
        self.art.assert_called_once()
        self.assertEqual(self.art.call_args[1]['location'], ('smb://n/Movies/Heat (1995)/', 'Heat (1995)'))

    def test_an_art_failure_does_not_stop_the_pass(self):
        self.art.side_effect = OSError('share offline')
        self.assertEqual(fsc.run()['checked'], 1)

    def test_corrections_are_written_and_counted(self):
        self.diff_movie.return_value = {'title': 'Heat'}
        result = fsc.run()
        self.set_movie_details.assert_called_once_with(1, {'title': 'Heat'})
        self.assertEqual(result['updated'], 1)
        self.refresh_movie.assert_not_called()

    def test_a_cast_or_set_difference_triggers_a_rescrape_after_a_successful_write(self):
        self.diff_movie.return_value = {'title': 'Heat'}
        self.set_differs.return_value = True
        fsc.run()
        self.refresh_movie.assert_called_once_with(1)

    def test_a_rejected_write_is_an_error_and_not_re_scraped(self):
        self.diff_movie.return_value = {'title': 'Heat'}
        self.set_differs.return_value = True
        self.set_movie_details.return_value = False
        result = fsc.run()
        self.assertEqual((result['updated'], result['errors']), (0, 1))
        self.refresh_movie.assert_not_called()

    def test_cancellation_and_progress(self):
        progress = mock.Mock()
        flags = iter([False, True, True])
        result = fsc.run(is_cancelled=lambda: next(flags), progress_callback=progress)
        self.assertEqual(progress.call_count, 1)
        self.assertTrue(result['cancelled'])


if __name__ == '__main__':
    unittest.main()
