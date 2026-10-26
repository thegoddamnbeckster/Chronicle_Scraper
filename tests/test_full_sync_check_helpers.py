# -*- coding: utf-8 -*-
"""Movie full_sync_check helpers: refresh_movie, cast_differs, years_in_path."""
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

    def test_refresh_movie(self):
        with self._rpc({'result': 'OK'}) as rpc:
            self.assertTrue(fsc.refresh_movie(5))
            self.assertEqual(json.loads(rpc.call_args[0][0])['params'], {'movieid': 5, 'ignorenfo': True})
        with self._rpc({'error': {}}):
            self.assertFalse(fsc.refresh_movie(5))
        with self._rpc(exc=RuntimeError('down')):
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


if __name__ == '__main__':
    unittest.main()
