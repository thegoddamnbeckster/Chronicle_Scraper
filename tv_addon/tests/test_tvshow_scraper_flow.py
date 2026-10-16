# -*- coding: utf-8 -*-
"""tvshow_scraper: the actions Kodi's TV scanner calls (find, getdetails, getepisodelist, getepisodedetails, getartwork) and the dispatcher."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from python import tvshow_scraper as ts


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.client = mock.MagicMock()
        for obj, name, new in ((ts, 'ChronicleClient', mock.Mock(return_value=self.client)),
                               (ts.activity_tracker, 'mark_active', mock.Mock()),
                               (ts.xbmcplugin, 'setResolvedUrl', mock.Mock()),
                               (ts.xbmcplugin, 'addDirectoryItem', mock.Mock()),
                               (ts.xbmcplugin, 'endOfDirectory', mock.Mock()),
                               (ts, 'remove_local_show_posters', mock.Mock())):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)
        self.items = []
        p = mock.patch.object(ts.xbmcgui, 'ListItem', side_effect=self._make_item)
        p.start()
        self.addCleanup(p.stop)

    def _make_item(self, label='', offscreen=False):
        item = stubs._FakeListItem(label, offscreen)
        self.items.append(item)
        return item


class TestHelpers(_Base):
    def test_get_params(self):
        self.assertEqual(ts.get_params(['5']), {'handle': 5})
        self.assertEqual(ts.get_params(['5', '']), {'handle': 5})
        self.assertEqual(ts.get_params(['5', '?action=find&title=Doctor%20Who&year=2005']),
                         {'handle': 5, 'action': 'find', 'title': 'Doctor Who', 'year': '2005'})

    def test_lookup_string_round_trip_and_bad_input(self):
        self.assertEqual(ts.parse_lookup_string(ts.build_lookup_string(42)), 42)
        for bad in ('not json', None, '[1,2]', '"str"'):
            self.assertIsNone(ts.parse_lookup_string(bad), bad)

    def test_resolve_lookup_id_shapes(self):
        self.assertEqual(ts._resolve_lookup_id({'id': '42'}), 42)
        self.assertEqual(ts._resolve_lookup_id({'url': ts.build_lookup_string(7)}), 7)
        self.client.resolve_show_by_external_id.return_value = {'id': 9}
        self.assertEqual(ts._resolve_lookup_id({'id': 'tt0903747'}), 9)
        self.client.resolve_show_by_external_id.assert_called_once_with('imdb', 'tt0903747')
        self.client.resolve_show_by_external_id.return_value = None
        self.assertIsNone(ts._resolve_lookup_id({'id': 'tt0000000'}))
        self.assertIsNone(ts._resolve_lookup_id({}))
        self.assertIsNone(ts._resolve_lookup_id({'id': 'garbage'}))


class TestFindShow(_Base):
    def test_one_candidate_is_offered_with_a_lookup_string_url(self):
        self.client.search_show.return_value = {'id': 5, 'title': 'Doctor Who', 'year': 2005, 'posterUrl': 'http://c/p.jpg'}
        ts.find_show('Doctor Who', '2005', 3)
        self.client.search_show.assert_called_once_with('Doctor Who', '2005')
        kwargs = ts.xbmcplugin.addDirectoryItem.call_args[1]
        self.assertEqual((kwargs['handle'], json.loads(kwargs['url']), kwargs['isFolder']), (3, {'chronicleId': 5}, True))
        self.assertEqual(self.items[0].label, 'Doctor Who (2005)')
        ts.activity_tracker.mark_active.assert_called_once_with('Doctor Who')

    def test_nothing_found_offers_nothing(self):
        self.client.search_show.return_value = None
        ts.find_show('x', None, 3)
        ts.xbmcplugin.addDirectoryItem.assert_not_called()


class TestGetDetails(_Base):
    DETAILS = {'title': 'Show', 'year': 2020, 'tagline': 'tag', 'status': 'Ended', 'runtimeMinutes': 45, 'userRating': 8,
               'seasons': [{'number': 1, 'name': 'One', 'posterUrl': 'http://c/s1.jpg'}, {'number': None}, {'number': 2}],
               'cast': [{'name': 'Actor', 'role': 'Hero'}], 'ratings': {}, 'artwork': {'poster': [{'url': 'http://c/p.jpg'}]},
               'externalIds': {'tvdb': '5'}}

    def test_everything_is_sent_to_kodi_and_a_resolved_url_is_set(self):
        self.client.get_show_details.return_value = dict(self.DETAILS)
        self.assertTrue(ts.get_details(5, 3))
        vtag = self.items[0].getVideoInfoTag()
        vtag.setMediaType.assert_called_with('tvshow')
        vtag.setTagLine.assert_called_with('tag')
        vtag.setTvShowStatus.assert_called_with('Ended')
        vtag.setDuration.assert_called_with(2700)
        vtag.setUserRating.assert_called_with(8)
        vtag.setEpisodeGuide.assert_called_with(ts.build_lookup_string(5))
        self.assertEqual([c[0][0] for c in vtag.addSeason.call_args_list], [1, 2])     # the season without a number is skipped
        ts.xbmcplugin.setResolvedUrl.assert_called_once()
        ts.remove_local_show_posters.assert_called_once()

    def test_unknown_id_or_unknown_show(self):
        self.assertFalse(ts.get_details(None, 3))
        self.client.get_show_details.return_value = None
        self.assertFalse(ts.get_details(5, 3))
        ts.xbmcplugin.setResolvedUrl.assert_not_called()

    def test_optional_fields_are_left_alone_when_chronicle_has_none(self):
        self.client.get_show_details.return_value = {'title': 'Show'}
        self.assertTrue(ts.get_details(5, 3))
        vtag = self.items[0].getVideoInfoTag()
        vtag.setUserRating.assert_not_called()
        vtag.setTagLine.assert_not_called()


class TestEpisodeList(_Base):
    def test_every_episode_is_offered(self):
        self.client.get_episode_list.return_value = [{'id': 1, 'season': 1, 'episode': 1, 'title': 'Pilot'}, {'id': 2, 'season': 1, 'episode': 2}]
        ts.get_episode_list(ts.build_lookup_string(5), 3)
        self.client.get_episode_list.assert_called_once_with(5)
        self.assertEqual(ts.xbmcplugin.addDirectoryItem.call_count, 2)
        self.assertEqual(self.items[1].label, 'Episode 2')

    def test_bad_guide_string_or_empty_list(self):
        ts.get_episode_list('garbage', 3)
        self.client.get_episode_list.assert_not_called()
        self.client.get_episode_list.return_value = []
        ts.get_episode_list(ts.build_lookup_string(5), 3)
        ts.xbmcplugin.addDirectoryItem.assert_not_called()


class TestEpisodeDetails(_Base):
    def setUp(self):
        super().setUp()
        for obj, name, new in ((ts, 'find_show_location', mock.Mock(return_value=('smb://n/TV/Show/', 7))),
                               (ts.progress_sync, 'lookup_episode_state', mock.Mock(return_value=None))):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)

    DETAILS = {'title': 'Pilot', 'showTitle': 'Show', 'showYear': 2020, 'season': 1, 'episode': 2, 'overview': 'Plot', 'year': 2020,
               'aired': '2020-05-01T00:00:00Z', 'runtimeMinutes': 40, 'userRating': 9, 'thumbUrl': 'http://c/t.jpg',
               'crew': [{'name': 'Dir', 'job': 'Director'}, {'name': 'Wri', 'job': 'Teleplay'}, 'junk', {'name': 'Other', 'job': 'Gaffer'}],
               'externalIds': {'tvdb': '5', 'imdb': 'tt1'}, 'cast': [{'name': 'A', 'role': 'R'}], 'ratings': {}}

    def _run(self, **overrides):
        details = dict(self.DETAILS, **overrides)
        self.client.get_episode_details.return_value = details
        return ts.get_episode_details(ts.build_lookup_string(11), 3)

    def test_fields_reach_kodi(self):
        self.assertTrue(self._run())
        vtag = self.items[0].getVideoInfoTag()
        vtag.setFirstAired.assert_called_with('2020-05-01')
        vtag.setDirectors.assert_called_with(['Dir'])
        vtag.setWriters.assert_called_with(['Wri'])
        vtag.setUniqueIDs.assert_called_with({'imdb': 'tt1', 'tvdb': '5'}, 'imdb')
        vtag.setUserRating.assert_called_with(9)
        ts.find_show_location.assert_called_once_with('Show', 2020)
        self.assertEqual(self.items[0]._art, {'thumb': 'http://c/t.jpg'})

    def test_unknown_episode(self):
        self.assertFalse(ts.get_episode_details('garbage', 3))
        self.client.get_episode_details.return_value = None
        self.assertFalse(ts.get_episode_details(ts.build_lookup_string(11), 3))

    def test_percent_encoded_url_is_decoded(self):
        self.client.get_episode_details.return_value = dict(self.DETAILS)
        import urllib.parse
        self.assertTrue(ts.get_episode_details(urllib.parse.quote(ts.build_lookup_string(11)), 3))

    def test_a_resume_point_is_pushed_into_kodi_when_chronicle_is_newer(self):
        self._run(resumePositionPercent=25.0, resumeUpdatedAt='2020-06-01T00:00:00Z')
        self.items[0].getVideoInfoTag().setResumePoint.assert_called_with(600.0, 2400)

    def test_kodis_newer_resume_is_pulled_into_chronicle(self):
        ts.progress_sync.lookup_episode_state.return_value = {'playcount': 0, 'lastplayed': '2030-01-01 12:00:00',
                                                              'resume': {'position': 500, 'total': 1000}}
        self._run(resumePositionPercent=10.0, resumeUpdatedAt='2020-06-01T00:00:00Z')
        self.client.push_resume.assert_called_once()
        self.assertEqual(self.client.push_resume.call_args[0][:2], (11, 50.0))

    def test_watched_in_chronicle_is_pushed_and_kodis_watch_is_pulled(self):
        self._run(isWatched=True, lastWatchedAt='2020-06-01T00:00:00Z')
        vtag = self.items[0].getVideoInfoTag()
        vtag.setPlaycount.assert_called_with(1)
        ts.progress_sync.lookup_episode_state.return_value = {'playcount': 1, 'lastplayed': '2030-01-01 12:00:00', 'resume': {}}
        self._run(isWatched=True, lastWatchedAt='2020-06-01T00:00:00Z')
        self.client.push_watched.assert_called_once()

    def test_a_chronicle_reset_clears_an_older_kodi_watch(self):
        ts.progress_sync.lookup_episode_state.return_value = {'playcount': 1, 'lastplayed': '2020-01-01 12:00:00', 'resume': {}}
        self._run(isWatched=False, watchResetAt='2020-06-01T00:00:00Z')
        self.items[0].getVideoInfoTag().setPlaycount.assert_called_with(0)

    def test_a_leftover_bookmark_on_a_finished_episode_is_cleared(self):
        ts.progress_sync.lookup_episode_state.return_value = {'playcount': 1, 'lastplayed': '2020-06-01 18:00:00', 'resume': {'position': 500, 'total': 1000}}
        self._run(isWatched=True, lastWatchedAt='2020-06-01T12:00:00Z')
        self.items[0].getVideoInfoTag().setResumePoint.assert_called_with(0, 0)


class TestArtwork(_Base):
    def test_art_and_season_posters(self):
        self.client.get_show_details.return_value = {'title': 'Show', 'artwork': {'poster': [{'url': 'http://c/p.jpg'}]},
                                                     'seasons': [{'number': 1, 'posterUrl': 'http://c/s1.jpg'}, {'number': None, 'posterUrl': 'x'}, {'number': 2}]}
        self.assertTrue(ts.get_artwork(5, 3))
        vtag = self.items[0].getVideoInfoTag()
        self.assertEqual(vtag.addAvailableArtwork.call_args_list[-1][0][:2], ('http://c/s1.jpg', 'poster'))
        ts.xbmcplugin.setResolvedUrl.assert_called_once()

    def test_unknown(self):
        self.assertFalse(ts.get_artwork(None, 3))
        self.client.get_show_details.return_value = None
        self.assertFalse(ts.get_artwork(5, 3))


class TestRun(_Base):
    def _run(self, query):
        with mock.patch.object(ts.sys, 'argv', ['plugin', '3', query]):
            ts.run()

    def test_each_action_is_dispatched_and_the_directory_always_ended(self):
        with mock.patch.object(ts, 'find_show') as find, mock.patch.object(ts, 'get_details') as details, \
                mock.patch.object(ts, 'get_episode_list') as episodes, mock.patch.object(ts, 'get_episode_details') as episode, \
                mock.patch.object(ts, 'get_artwork') as artwork:
            self._run('?action=find&title=Show&year=2020')
            find.assert_called_once_with('Show', '2020', 3)
            self._run('?action=getdetails&url=' + '%7B%22chronicleId%22%3A5%7D')
            details.assert_called_once_with(5, 3)
            self._run('?action=getepisodelist&url=abc')
            episodes.assert_called_once_with('abc', 3)
            self._run('?action=getepisodedetails&url=abc')
            episode.assert_called_once_with('abc', 3)
            self._run('?action=getartwork&id=5')
            artwork.assert_called_once_with(5, 3)
        self.assertEqual(ts.xbmcplugin.endOfDirectory.call_count, 5)

    def test_unknown_or_incomplete_actions_still_end_the_directory(self):
        self._run('?action=bogus')
        self._run('?action=find')      # no title
        self.assertEqual(ts.xbmcplugin.endOfDirectory.call_count, 2)


if __name__ == '__main__':
    unittest.main()
