# -*- coding: utf-8 -*-
"""collection_art_sync: sets Kodi's movie-set art from Chronicle's changed collections, writing only what differs."""
import json
import unittest
from unittest import mock
from urllib.parse import quote

import tests.kodi_stubs as stubs

from lib import collection_art_sync as cas


def kodi_value(url):
    """How Kodi reports an art URL it stores."""
    return 'image://{0}/'.format(quote(url, safe=''))


POSTER = 'https://image.tmdb.org/t/p/original/p.jpg'
FANART = 'https://image.tmdb.org/t/p/original/f.jpg'
LOGO = 'https://assets.fanart.tv/l.png'


class TestPureHelpers(unittest.TestCase):
    def test_kodi_art_url_unwraps_what_kodi_stores(self):
        self.assertEqual(cas.kodi_art_url(kodi_value(POSTER)), POSTER)
        self.assertEqual(cas.kodi_art_url(POSTER), POSTER)
        self.assertEqual(cas.kodi_art_url(None), '')

    def test_a_url_with_its_own_escapes_survives_the_round_trip(self):
        url = 'https://x/a%20b.jpg?x=1&y=2'
        self.assertEqual(cas.kodi_art_url(kodi_value(url)), url)

    def test_desired_art_has_only_the_slots_chronicle_has(self):
        self.assertEqual(
            cas.desired_art({'posterUrl': POSTER, 'backdropUrl': FANART, 'logoUrl': LOGO, 'bannerUrl': None}),
            {'poster': POSTER, 'fanart': FANART, 'clearlogo': LOGO})

    def test_nothing_to_write_when_kodi_already_has_chronicles_art(self):
        current = {'poster': kodi_value(POSTER), 'fanart': kodi_value(FANART)}
        self.assertEqual(cas.art_updates(current, {'posterUrl': POSTER, 'backdropUrl': FANART}), {})

    def test_only_the_differing_slots_are_written(self):
        current = {'poster': kodi_value(POSTER), 'fanart': kodi_value('https://old/f.jpg')}
        self.assertEqual(cas.art_updates(current, {'posterUrl': POSTER, 'backdropUrl': FANART, 'logoUrl': LOGO}),
                         {'fanart': FANART, 'clearlogo': LOGO})

    def test_a_file_on_the_share_is_replaced_by_the_url(self):
        current = {'poster': 'image://smb%3a%2f%2fnas%2fMovie%20Collections%2fA%2fposter.jpg/'}
        self.assertEqual(cas.art_updates(current, {'posterUrl': POSTER}), {'poster': POSTER})

    def test_a_slot_chronicle_has_nothing_for_is_left_alone(self):
        self.assertEqual(cas.art_updates({'banner': kodi_value('https://k/b.jpg')}, {'posterUrl': POSTER}),
                         {'poster': POSTER})


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.client = mock.MagicMock()
        self.kodi_sets = {}
        self.rpc = []
        self.rpc_ok = True

        def execute(raw):
            request = json.loads(raw)
            self.rpc.append(request)
            if request['method'] == 'VideoLibrary.GetMovieSets':
                return json.dumps({'result': {'sets': [
                    {'setid': i, 'label': name, 'art': art} for name, (i, art) in self.kodi_sets.items()]}})
            if request['method'] == 'VideoLibrary.SetMovieSetDetails':
                return json.dumps({'result': 'OK' if self.rpc_ok else 'nope'})
            return '{"result": {}}'

        for obj, name, new in ((cas, 'ChronicleClient', mock.Mock(return_value=self.client)),
                               (cas.xbmc, 'executeJSONRPC', mock.Mock(side_effect=execute))):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)

    def writes(self):
        return [r['params'] for r in self.rpc if r['method'] == 'VideoLibrary.SetMovieSetDetails']

    def listing(self, *collections, as_of='2026-10-24T01:00:00Z'):
        self.client.get_changed_collections.return_value = {'asOf': as_of, 'collections': list(collections)}

    def remember(self, *names, since='2026-10-23T00:00:00Z'):
        stubs._FAKE_FILES[cas._STATE_PATH] = json.dumps({'since': since, 'sets': list(names)}).encode()

    def saved(self):
        return json.loads(stubs._FAKE_FILES[cas._STATE_PATH])


class TestRun(_Base):
    def test_first_run_asks_for_everything_writes_the_difference_and_remembers_the_time(self):
        self.kodi_sets = {'Alien Collection': (7, {'poster': kodi_value(POSTER)})}
        self.listing({'name': 'Alien Collection', 'posterUrl': POSTER, 'backdropUrl': FANART})
        progress = mock.Mock()

        result = cas.run(progress_callback=progress)

        self.client.get_changed_collections.assert_called_once_with(since=None)
        self.assertEqual(self.writes(), [{'setid': 7, 'art': {'fanart': FANART}}])
        self.assertEqual(result, {'collections': 1, 'changed': 1, 'errors': 0, 'cancelled': False})
        progress.assert_called_once_with(0, 1, 'Alien Collection')
        self.assertEqual(self.saved(), {'since': '2026-10-24T01:00:00Z', 'sets': ['Alien Collection']})

    def test_later_runs_send_their_own_time_and_an_empty_answer_touches_nothing(self):
        self.kodi_sets = {'Alien Collection': (7, {})}
        self.listing(as_of='2026-10-24T05:00:00Z')
        self.remember('Alien Collection', since='2026-10-24T01:00:00Z')
        progress = mock.Mock()

        result = cas.run(progress_callback=progress)

        self.client.get_changed_collections.assert_called_once_with(since='2026-10-24T01:00:00Z')
        self.assertEqual(self.writes(), [])
        progress.assert_not_called()          # nothing on screen when there is nothing to do
        self.assertEqual(result, {'collections': 0, 'changed': 0, 'errors': 0, 'cancelled': False})
        self.assertEqual(self.saved()['since'], '2026-10-24T05:00:00Z')      # the copy of the time moves on

    def test_a_set_kodi_has_not_had_before_asks_for_everything(self):
        self.kodi_sets = {'Alien Collection': (7, {}), 'New Collection': (8, {})}
        self.listing()
        self.remember('Alien Collection')

        cas.run()

        self.client.get_changed_collections.assert_called_once_with(since=None)

    def test_a_manual_sync_asks_for_everything(self):
        self.kodi_sets = {'A': (1, {})}
        self.listing()
        self.remember('A')

        cas.run(everything=True)

        self.client.get_changed_collections.assert_called_once_with(since=None)

    def test_a_collection_already_in_step_is_not_written(self):
        self.kodi_sets = {'A': (1, {'poster': kodi_value(POSTER)})}
        self.listing({'name': 'A', 'posterUrl': POSTER})

        result = cas.run()

        self.assertEqual(self.writes(), [])
        self.assertEqual((result['collections'], result['changed']), (1, 0))

    def test_a_collection_kodi_has_no_set_for_is_skipped(self):
        self.kodi_sets = {'A': (1, {})}
        self.listing({'name': 'Not In Kodi', 'posterUrl': POSTER})

        result = cas.run()

        self.assertEqual(self.writes(), [])
        self.assertEqual(result['errors'], 0)

    def test_the_sets_handled_are_remembered_for_next_time(self):
        self.kodi_sets = {'B': (2, {}), 'A': (1, {})}
        self.listing()

        cas.run()

        self.assertEqual(self.saved()['sets'], ['A', 'B'])

    def test_a_failed_write_is_an_error_and_the_time_is_not_remembered(self):
        self.kodi_sets = {'A': (1, {})}
        self.listing({'name': 'A', 'posterUrl': POSTER})
        self.rpc_ok = False

        result = cas.run()

        self.assertEqual((result['changed'], result['errors']), (0, 1))
        self.assertNotIn(cas._STATE_PATH, stubs._FAKE_FILES)

    def test_chronicle_not_answering_is_an_error_and_remembers_nothing(self):
        self.kodi_sets = {'A': (1, {})}
        self.client.get_changed_collections.return_value = None

        result = cas.run()

        self.assertEqual(result['errors'], 1)
        self.assertEqual(self.writes(), [])
        self.assertNotIn(cas._STATE_PATH, stubs._FAKE_FILES)

    def test_kodi_not_answering_does_nothing_at_all(self):
        cas.xbmc.executeJSONRPC.side_effect = RuntimeError('rpc down')

        result = cas.run()

        self.assertEqual(result['errors'], 1)
        self.client.get_changed_collections.assert_not_called()

    def test_cancelling_stops_between_collections_and_remembers_nothing(self):
        self.kodi_sets = {'A': (1, {}), 'B': (2, {})}
        self.listing({'name': 'A', 'posterUrl': POSTER}, {'name': 'B', 'posterUrl': POSTER})
        flags = iter([False, True])

        result = cas.run(is_cancelled=lambda: next(flags))

        self.assertTrue(result['cancelled'])
        self.assertEqual(len(self.writes()), 1)
        self.assertNotIn(cas._STATE_PATH, stubs._FAKE_FILES)

    def test_unreadable_state_is_treated_as_no_state(self):
        self.kodi_sets = {'A': (1, {})}
        self.listing()
        stubs._FAKE_FILES[cas._STATE_PATH] = b'{not json'

        cas.run()

        self.client.get_changed_collections.assert_called_once_with(since=None)

    def test_an_older_state_file_holding_only_the_names_still_asks_for_everything_once(self):
        self.kodi_sets = {'A': (1, {})}
        self.listing()
        stubs._FAKE_FILES[cas._STATE_PATH] = json.dumps(['A']).encode()

        cas.run()

        self.client.get_changed_collections.assert_called_once_with(since=None)
        self.assertEqual(self.saved()['since'], '2026-10-24T01:00:00Z')


class TestDescribeResult(unittest.TestCase):
    strings = {32147: 'Updated {0} collection(s){1}', 32136: ' ({0} errors)'}

    def test_silent_when_nothing_changed(self):
        self.assertIsNone(cas.describe_result({'changed': 0, 'errors': 0}, self.strings.get))

    def test_says_what_changed_and_any_errors(self):
        self.assertEqual(cas.describe_result({'changed': 3, 'errors': 0}, self.strings.get),
                         'Updated 3 collection(s)')
        self.assertEqual(cas.describe_result({'changed': 1, 'errors': 2}, self.strings.get),
                         'Updated 1 collection(s) (2 errors)')


if __name__ == '__main__':
    unittest.main()
