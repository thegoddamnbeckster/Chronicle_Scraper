# -*- coding: utf-8 -*-
"""
Regression test for a live bug (2026-09-13): lib/collection_sync.py's _repair_stale_set_art()
used to clear a dead art reference by sending an empty string (''), but Kodi's
VideoLibrary.SetMovieSetDetails schema rejects an empty string for any art slot outright
("Received value does not match any of the union type definitions") -- and since `art` is one
combined object per call, that single bad value silently dropped every OTHER art update batched
in the same call too, for any set that had even one dead reference to clear. Confirmed live via
direct JSON-RPC reproduction against a real device: '' fails, null succeeds. Fixed by sending
None (serializes to JSON null) instead of ''.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcvfs

from lib import collection_sync


class TestRepairStaleSetArtNeverSendsEmptyString(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_dead_reference_with_no_local_replacement_clears_via_null_not_empty_string(self):
        # The set has one registered art type (poster) pointing at a local file that no
        # longer exists, and no same-named replacement file sits in the current folder --
        # the exact "clear it outright" branch that used to send ''.
        find_set_response = json.dumps({'result': {'sets': [
            {'setid': 42, 'label': 'Ghostbusters Collection',
             'art': {'poster': 'image://smb%3a%2f%2fhost%2fold%2fposter.jpg/'}},
        ]}})
        captured_requests = []

        def _fake_json_rpc(request_json):
            request = json.loads(request_json)
            captured_requests.append(request)
            if request['method'] == 'VideoLibrary.GetMovieSets':
                return find_set_response
            if request['method'] == 'VideoLibrary.SetMovieSetDetails':
                return json.dumps({'result': 'OK'})
            return json.dumps({'result': {}})

        with patch('lib.collection_sync.xbmc.executeJSONRPC', side_effect=_fake_json_rpc), \
             patch('lib.collection_sync.xbmcvfs.exists', return_value=False):
            collection_sync._repair_stale_set_art('Ghostbusters Collection', '/movie_sets/Ghostbusters Collection/')

        set_calls = [r for r in captured_requests if r['method'] == 'VideoLibrary.SetMovieSetDetails']
        self.assertEqual(len(set_calls), 1)
        art_payload = set_calls[0]['params']['art']
        self.assertIn('poster', art_payload)
        self.assertIsNone(art_payload['poster'], "a cleared slot must be JSON null, never ''")
        self.assertNotEqual(art_payload['poster'], '',
                             "an empty string here makes Kodi reject the ENTIRE SetMovieSetDetails "
                             "call, silently dropping every other art update batched with it")


if __name__ == '__main__':
    unittest.main()
