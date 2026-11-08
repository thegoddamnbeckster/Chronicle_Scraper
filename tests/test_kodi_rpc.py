"""kodi_rpc: the one place the add-ons send JSON-RPC to Kodi and ask it to re-scrape a library item."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401  (installs the xbmc stubs before the module under test is imported)
import xbmc
from lib import kodi_rpc


class TestSend(unittest.TestCase):
    def test_a_request_is_sent_as_json_and_the_answer_decoded(self):
        with mock.patch.object(xbmc, 'executeJSONRPC', return_value='{"result": "OK"}') as rpc:
            self.assertEqual(kodi_rpc.send({'jsonrpc': '2.0', 'id': 1, 'method': 'JSONRPC.Ping'}), {'result': 'OK'})
        self.assertEqual(json.loads(rpc.call_args[0][0])['method'], 'JSONRPC.Ping')

    def test_call_builds_the_request_from_a_method_and_params(self):
        with mock.patch.object(xbmc, 'executeJSONRPC', return_value='{"result": {}}') as rpc:
            kodi_rpc.call('Settings.GetSettingValue', {'setting': 'locale.timezone'})
            kodi_rpc.call('JSONRPC.Ping')
        first, second = (json.loads(c[0][0]) for c in rpc.call_args_list)
        self.assertEqual((first['jsonrpc'], first['method'], first['params']), ('2.0', 'Settings.GetSettingValue', {'setting': 'locale.timezone'}))
        self.assertNotIn('params', second)


class TestRefreshItem(unittest.TestCase):
    def _refresh(self, answer, *args, **kwargs):
        with mock.patch.object(xbmc, 'executeJSONRPC', **answer) as rpc:
            ok = kodi_rpc.refresh_item(*args, **kwargs)
        return ok, [json.loads(c[0][0]) for c in rpc.call_args_list]

    def test_each_kind_uses_its_own_method_and_id_parameter(self):
        for kind, method, param in (('movie', 'VideoLibrary.RefreshMovie', 'movieid'), ('episode', 'VideoLibrary.RefreshEpisode', 'episodeid'),
                                    ('tvshow', 'VideoLibrary.RefreshTVShow', 'tvshowid')):
            ok, (request,) = self._refresh({'return_value': '{"result": "OK"}'}, kind, 7)
            self.assertTrue(ok)
            self.assertEqual((request['method'], request['params']), (method, {param: 7}))

    def test_ignoring_the_nfo_and_refreshing_a_shows_episodes_are_opt_in(self):
        _, (movie,) = self._refresh({'return_value': '{"result": "OK"}'}, 'movie', 1, ignore_nfo=True, with_episodes=True)
        self.assertEqual(movie['params'], {'movieid': 1, 'ignorenfo': True})          # episodes only apply to a show
        _, (show,) = self._refresh({'return_value': '{"result": "OK"}'}, 'tvshow', 2, ignore_nfo=True, with_episodes=True)
        self.assertEqual(show['params'], {'tvshowid': 2, 'ignorenfo': True, 'refreshepisodes': True})

    def test_a_refusal_or_a_failure_is_logged_and_reported_as_false_not_raised(self):
        with mock.patch.object(kodi_rpc.log, 'warning') as warning:
            self.assertFalse(self._refresh({'return_value': '{"error": {"code": -32602}}'}, 'movie', 9, who='me')[0])
            self.assertFalse(self._refresh({'side_effect': RuntimeError('down')}, 'movie', 9, who='me')[0])
        self.assertEqual(warning.call_count, 2)
        self.assertIn('me: VideoLibrary.RefreshMovie(movieid=9)', warning.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
