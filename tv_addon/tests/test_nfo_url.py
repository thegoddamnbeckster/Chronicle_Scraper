# -*- coding: utf-8 -*-
"""
Regression tests for tvshow_scraper.py's nfo_url() -- Kodi's "NfoUrl" action.

Root-caused live (2026-09-12): this action was never implemented at all (a known,
documented gap), so it always fell through to "unhandled or missing action" for every show a
prior tool (e.g. tinyMediaManager) had already organized, or every show/episode Chronicle's own
write_nfo feature had already written a sidecar for. A failed SHOW-level NfoUrl call is
harmless (confirmed live: Kodi falls back to its own normal find()-based flow), but a failed
EPISODE-level call is not: confirmed live against a real household show (stuck at exactly 5 of
38 episodes no matter how many times it was rescanned) that Kodi abandons the REST of that
show's episode scan entirely the moment the first episode's NfoUrl call comes back empty,
rather than falling back to a normal getepisodelist/getepisodedetails pass. See nfo_url's own
doc and _nfo_url_episode's own doc for the full story.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc/xbmcgui/xbmcplugin

from python import tvshow_scraper

_TVSHOW_NFO_WITH_IDS = '''<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>Ahsoka</title>
  <year>2023</year>
  <uniqueid type="imdb" default="true">tt13622776</uniqueid>
  <uniqueid type="tmdb">114461</uniqueid>
  <uniqueid type="tvdb">393187</uniqueid>
</tvshow>'''

_TVSHOW_NFO_NO_IDS = '''<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>Some Legacy Show</title>
  <year>2019</year>
</tvshow>'''

_EPISODE_NFO = '''<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>Part One: Master and Apprentice</title>
  <season>1</season>
  <episode>1</episode>
  <uniqueid type="tmdb">2552685</uniqueid>
</episodedetails>'''

_EPISODE_NFO_NO_IDS = '''<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>Some Legacy Episode</title>
  <season>2</season>
  <episode>3</episode>
</episodedetails>'''


class TestNfoUrl(unittest.TestCase):

    def setUp(self):
        tvshow_scraper.xbmcplugin.addDirectoryItem.reset_mock()

    def test_none_nfo_returns_false_and_makes_no_calls(self):
        with patch('python.tvshow_scraper.ChronicleClient') as mock_client_cls:
            result = tvshow_scraper.nfo_url(None, handle=1)

        self.assertFalse(result)
        mock_client_cls.assert_not_called()
        tvshow_scraper.xbmcplugin.addDirectoryItem.assert_not_called()

    def test_episode_resolves_via_tmdb_id_when_present(self):
        mock_client = MagicMock()
        mock_client.resolve_episode_by_external_id.return_value = {'id': 473792, 'title': 'Part One'}
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_EPISODE_NFO, handle=9)

        self.assertTrue(result)
        mock_client.resolve_episode_by_external_id.assert_called_once_with('tmdb', '2552685')

        call = tvshow_scraper.xbmcplugin.addDirectoryItem.call_args
        self.assertEqual(call.kwargs['handle'], 9)
        self.assertEqual(call.kwargs['url'], tvshow_scraper.build_lookup_string(473792))
        self.assertTrue(call.kwargs['isFolder'])

    def test_episode_falls_back_to_imdb_then_tvdb_when_tmdb_lookup_fails(self):
        mock_client = MagicMock()
        mock_client.resolve_episode_by_external_id.return_value = None
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            tvshow_scraper.nfo_url(_EPISODE_NFO, handle=1)

        # Only tmdb is present on this NFO, so only tmdb should ever be tried -- proves the
        # fallback loop doesn't call resolve_episode_by_external_id with a source the NFO
        # never actually carried.
        mock_client.resolve_episode_by_external_id.assert_called_once_with('tmdb', '2552685')

    def test_episode_with_no_external_ids_returns_false_with_no_calls(self):
        # Unlike a show, an episode has no title+year search fallback available -- see
        # _nfo_url_episode's own doc for why.
        with patch('python.tvshow_scraper.ChronicleClient') as mock_client_cls:
            result = tvshow_scraper.nfo_url(_EPISODE_NFO_NO_IDS, handle=1)

        self.assertFalse(result)
        mock_client_cls.assert_not_called()
        tvshow_scraper.xbmcplugin.addDirectoryItem.assert_not_called()

    def test_episode_resolve_failure_returns_false(self):
        mock_client = MagicMock()
        mock_client.resolve_episode_by_external_id.return_value = None
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_EPISODE_NFO, handle=1)

        self.assertFalse(result)
        tvshow_scraper.xbmcplugin.addDirectoryItem.assert_not_called()

    def test_unparseable_content_returns_false(self):
        with patch('python.tvshow_scraper.ChronicleClient') as mock_client_cls:
            result = tvshow_scraper.nfo_url('not xml at all', handle=1)

        self.assertFalse(result)
        mock_client_cls.assert_not_called()

    def test_resolves_via_imdb_id_when_present(self):
        mock_client = MagicMock()
        mock_client.resolve_show_by_external_id.return_value = {'id': 490248, 'title': 'Ahsoka'}
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_TVSHOW_NFO_WITH_IDS, handle=7)

        self.assertTrue(result)
        mock_client.resolve_show_by_external_id.assert_called_once_with('imdb', 'tt13622776')
        mock_client.search_show.assert_not_called()

        call = tvshow_scraper.xbmcplugin.addDirectoryItem.call_args
        self.assertEqual(call.kwargs['handle'], 7)
        self.assertEqual(call.kwargs['url'], tvshow_scraper.build_lookup_string(490248))
        self.assertTrue(call.kwargs['isFolder'])

    def test_falls_back_to_tmdb_then_tvdb_when_imdb_lookup_fails(self):
        mock_client = MagicMock()
        mock_client.resolve_show_by_external_id.side_effect = [None, {'id': 501, 'title': 'Ahsoka'}]
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_TVSHOW_NFO_WITH_IDS, handle=1)

        self.assertTrue(result)
        self.assertEqual(mock_client.resolve_show_by_external_id.call_args_list, [
            unittest.mock.call('imdb', 'tt13622776'),
            unittest.mock.call('tmdb', '114461'),
        ])

    def test_falls_back_to_search_show_when_no_external_id_resolves(self):
        mock_client = MagicMock()
        mock_client.resolve_show_by_external_id.return_value = None
        mock_client.search_show.return_value = {'id': 502, 'title': 'Ahsoka'}
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_TVSHOW_NFO_WITH_IDS, handle=1)

        self.assertTrue(result)
        mock_client.search_show.assert_called_once_with('Ahsoka', 2023)

    def test_no_external_ids_at_all_goes_straight_to_search_show(self):
        mock_client = MagicMock()
        mock_client.search_show.return_value = {'id': 503, 'title': 'Some Legacy Show'}
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_TVSHOW_NFO_NO_IDS, handle=1)

        self.assertTrue(result)
        mock_client.resolve_show_by_external_id.assert_not_called()
        mock_client.search_show.assert_called_once_with('Some Legacy Show', 2019)

    def test_everything_fails_returns_false(self):
        mock_client = MagicMock()
        mock_client.resolve_show_by_external_id.return_value = None
        mock_client.search_show.return_value = None
        with patch('python.tvshow_scraper.ChronicleClient', return_value=mock_client):
            result = tvshow_scraper.nfo_url(_TVSHOW_NFO_WITH_IDS, handle=1)

        self.assertFalse(result)
        tvshow_scraper.xbmcplugin.addDirectoryItem.assert_not_called()


if __name__ == '__main__':
    unittest.main()
