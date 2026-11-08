"""Bulk refresh: re-reads every title from Chronicle one at a time, waits out scans and playback, and is safe to stop (lib/bulk_refresh.py)."""
import json
import time
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401  (installs the xbmc stubs before the module under test is imported)
import xbmc
import xbmcgui
from lib import bulk_refresh


class _Sim:
    """Kodi and the scraper in miniature: a fake clock that sleep() advances, a library, and a scraper that marks its activity a moment
    after Kodi is asked to refresh a title (unless the title is one the scraper never sees)."""
    def __init__(self, ids=(1, 2, 3), ignored=(), scanning_for=0, error_ids=()):
        self.now, self.activity = 1000.0, 0.0
        self.ids, self.ignored, self.error_ids = list(ids), set(ignored), set(error_ids)
        self.scanning_polls = scanning_for
        self.requests, self.pending, self.slept = [], None, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.pending is not None:                      # the scraper picks the title up and works on it
            self.activity, self.pending = self.now, None

    def rpc(self, payload):
        request = json.loads(payload)
        method, params = request['method'], request.get('params') or {}
        if method in ('VideoLibrary.GetMovies', 'VideoLibrary.GetTVShows'):
            key, field = ('movies', 'movieid') if method.endswith('Movies') else ('tvshows', 'tvshowid')
            return json.dumps({'result': {key: [{field: i, 'label': 'T{0}'.format(i), 'year': 2000 + i} for i in self.ids]}})
        self.requests.append((method, params))
        title = params.get('movieid') or params.get('tvshowid')
        if title in self.error_ids:
            return json.dumps({'error': {'message': 'nope'}})
        if title not in self.ignored:
            self.pending = title
        return json.dumps({'result': 'OK'})

    def scanning(self, condition):
        if condition == 'Library.IsScanning' and self.scanning_polls > 0:
            self.scanning_polls -= 1
            return True
        return False


class TestRun(unittest.TestCase):
    def _run(self, sim, kind='movie', **kwargs):
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=sim.rpc), \
             mock.patch.object(xbmc, 'getCondVisibility', side_effect=sim.scanning), \
             mock.patch.object(bulk_refresh.activity_tracker, 'read_activity', side_effect=lambda: {'timestamp': sim.activity}):
            return bulk_refresh.run(kind, sleep=sim.sleep, clock=sim.clock, **kwargs)

    def test_every_title_is_refreshed_one_at_a_time_with_nfo_ignored(self):
        sim = _Sim()
        result = self._run(sim)
        self.assertEqual(sim.requests, [('VideoLibrary.RefreshMovie', {'movieid': i, 'ignorenfo': True}) for i in (1, 2, 3)])
        self.assertEqual((result['total'], result['refreshed'], result['skipped'], result['errors'], result['cancelled']), (3, 3, 0, 0, False))

    def test_a_show_is_refreshed_with_its_episodes(self):
        sim = _Sim(ids=(7,))
        self._run(sim, kind='tvshow')
        self.assertEqual(sim.requests, [('VideoLibrary.RefreshTVShow', {'tvshowid': 7, 'ignorenfo': True, 'refreshepisodes': True})])

    def test_a_title_the_scraper_never_sees_is_skipped_after_a_bounded_wait(self):
        sim = _Sim(ids=(1, 2), ignored=(1,))
        result = self._run(sim)
        self.assertEqual((result['refreshed'], result['skipped']), (1, 1))
        self.assertLessEqual(sum(sim.slept), bulk_refresh.START_WAIT_SECONDS + 2 * bulk_refresh.KINDS['movie'][4] + 10)

    def test_it_waits_for_a_library_scan_to_finish_before_asking_for_anything(self):
        sim = _Sim(ids=(1,), scanning_for=3)
        self._run(sim)
        self.assertEqual([s for s in sim.slept if s == 5], [5, 5, 5])
        self.assertEqual(len(sim.requests), 1)

    def test_a_refusal_is_counted_and_the_rest_carry_on(self):
        sim = _Sim(ids=(1, 2, 3), error_ids=(2,))
        result = self._run(sim)
        self.assertEqual((result['refreshed'], result['errors']), (2, 1))
        self.assertEqual(len(sim.requests), 3)

    def test_stopping_ends_it_after_the_current_title(self):
        sim = _Sim(ids=(1, 2, 3))
        result = self._run(sim, is_cancelled=lambda: len(sim.requests) >= 1)
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(sim.requests), 1)

    def test_progress_is_reported_for_each_title_as_it_starts(self):
        sim, seen = _Sim(ids=(1, 2)), []
        self._run(sim, progress_callback=lambda i, n, label: seen.append((i, n, label)))
        # the first entry says it is waiting for its turn; it is replaced as soon as the first title starts
        self.assertEqual([entry for entry in seen if entry[0] > 0], [(1, 2, 'T1 (2001)'), (2, 2, 'T2 (2002)')])
        self.assertEqual(seen[0][0], 0)

    def test_a_kodi_that_cannot_list_the_library_is_an_error_not_a_crash(self):
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            result = bulk_refresh.run('movie', sleep=lambda s: None)
        self.assertEqual((result['total'], result['errors']), (0, 1))

    def test_a_wait_for_the_library_scan_is_shown_and_logged_not_silent(self):
        sim, seen = _Sim(ids=(1,), scanning_for=2), []
        with mock.patch.object(bulk_refresh.log, 'info') as info:
            self._run(sim, progress_callback=lambda i, n, label: seen.append((i, label)), messages={'scan': 'WAIT-SCAN'})
        self.assertIn((0, 'WAIT-SCAN'), seen)
        self.assertTrue(any('waiting for the library scan' in str(c) for c in info.call_args_list))

    def test_a_second_refresh_waits_its_turn_behind_the_first_and_then_runs(self):
        # the movie and TV refreshes share one activity signal, so they must not run at once
        owner = bulk_refresh.sync_baton.owner_id('someone-else')
        store = bulk_refresh.sync_baton._STORE
        sim = _Sim(ids=(1,))
        store.set('{0}|{1}|the movie refresh'.format(owner, sim.now + 20))   # held, and gives up the claim 20s from now
        seen = []
        result = self._run(sim, kind='tvshow', progress_callback=lambda i, n, label: seen.append(label), messages={'turn': 'WAIT-TURN'})
        self.assertEqual(seen[0], 'WAIT-TURN')
        self.assertEqual(result['refreshed'], 1)
        self.assertGreaterEqual(sum(sim.slept), 20)       # it did wait for the claim to lapse before asking Kodi for anything
        store.set('')

    def test_the_baton_is_released_when_it_finishes_and_when_it_is_stopped(self):
        store = bulk_refresh.sync_baton._STORE
        self._run(_Sim(ids=(1, 2)))
        self.assertEqual(store.get(), '')
        sim = _Sim(ids=(1, 2, 3))
        self._run(sim, is_cancelled=lambda: len(sim.requests) >= 1)
        self.assertEqual(store.get(), '')

    def test_the_baton_is_let_go_between_chunks_so_a_waiting_sync_can_take_its_turn(self):
        # held throughout, a hours-long refresh would starve the syncs that ask for the baton once and give up after an hour
        store = bulk_refresh.sync_baton._STORE
        sim, free_at_handover = _Sim(ids=(1, 2, 3, 4, 5)), []
        plain_sleep = sim.sleep

        def sleep(seconds):
            if seconds == bulk_refresh.HANDOVER_SECONDS:
                free_at_handover.append(store.get())
            plain_sleep(seconds)
        sim.sleep = sleep
        with mock.patch.object(bulk_refresh, 'TITLES_PER_TURN', 2):
            result = self._run(sim)
        self.assertEqual(free_at_handover, ['', ''])                 # three chunks (2, 2, 1): two handovers, baton free at each
        self.assertEqual(result['refreshed'], 5)

    def test_a_pause_between_titles_beats_the_heartbeat_so_a_dead_run_can_be_told_from_a_live_one(self):
        beats = []
        self._run(_Sim(ids=(1, 2)), heartbeat=lambda: beats.append(1))
        self.assertGreater(len(beats), 2)

    def test_a_show_gets_a_longer_quiet_than_a_movie_because_its_episodes_arrive_with_gaps(self):
        self.assertGreater(bulk_refresh.KINDS['tvshow'][4], bulk_refresh.KINDS['movie'][4])

    def test_a_library_kodi_could_not_list_is_marked_unreadable(self):
        with mock.patch.object(xbmc, 'executeJSONRPC', side_effect=RuntimeError('down')):
            self.assertTrue(bulk_refresh.run('movie', sleep=lambda s: None)['unreadable'])

    def test_it_goes_slowly_while_a_video_plays(self):
        sim = _Sim(ids=(1,))
        with mock.patch.object(bulk_refresh.playback_pacing, 'is_video_playing', return_value=True):
            self._run(sim)
        self.assertIn(bulk_refresh.playback_pacing.PLAYBACK_PACE_SECONDS, sim.slept)


class TestSettingsButton(unittest.TestCase):
    TEXT = {'title': 1, 'confirm': 2, 'stop': 3, 'done': 4, 'stopped': 5, 'waiting_scan': 6, 'waiting_turn': 7, 'failed': 8}

    def setUp(self):
        self.props = {}
        window = mock.MagicMock()
        window.getProperty.side_effect = lambda k: self.props.get(k, '')
        window.setProperty.side_effect = lambda k, v: self.props.__setitem__(k, v)
        self.dialog = mock.MagicMock()
        self.addon = mock.MagicMock()
        self.addon.getLocalizedString.side_effect = lambda i: {1: 'Title', 2: 'Sure?', 3: 'Stop it?', 4: '{0}/{1} {2} {3}', 5: 'Stopped {0}/{1}', 6: 'scan wait', 7: 'turn wait', 8: 'could not read'}[i]
        for obj, name, new in ((xbmcgui, 'Window', mock.Mock(return_value=window)), (xbmcgui, 'Dialog', mock.Mock(return_value=self.dialog)),
                               (xbmcgui, 'DialogProgressBG', mock.Mock(return_value=mock.MagicMock()))):
            p = mock.patch.object(obj, name, new, create=True)
            p.start()
            self.addCleanup(p.stop)

    def _press(self, result=None):
        done = result or {'total': 5, 'refreshed': 4, 'skipped': 1, 'errors': 0, 'cancelled': False}
        with mock.patch.object(bulk_refresh, 'run', return_value=done) as run:
            bulk_refresh.run_from_settings(self.addon, 'movie', self.TEXT)
        return run

    def test_declining_the_confirmation_starts_nothing(self):
        self.dialog.yesno.return_value = False
        self._press().assert_not_called()

    def test_confirming_runs_it_clears_the_running_flag_and_reports(self):
        self.dialog.yesno.return_value = True
        self._press().assert_called_once()
        self.assertEqual(self.props.get('chronicle.bulkrefresh.movie'), '')
        self.assertEqual(self.dialog.notification.call_args[0][1], '4/5 1 0')

    def test_pressing_it_again_while_running_offers_to_stop_instead_of_starting_a_second(self):
        self.props['chronicle.bulkrefresh.movie'] = 'running|{0}'.format(time.time())
        self.dialog.yesno.return_value = True
        self._press().assert_not_called()
        self.assertTrue(self.props['chronicle.bulkrefresh.movie'].startswith('stop|'))

    def test_a_flag_left_by_a_killed_script_is_ignored_so_the_button_still_starts_a_run(self):
        # an add-on update stops its scripts without letting them clean up: the flag must not block the button until Kodi restarts
        self.props['chronicle.bulkrefresh.movie'] = 'running|{0}'.format(time.time() - bulk_refresh.STALE_SECONDS - 5)
        self.dialog.yesno.return_value = True
        self._press().assert_called_once()

    def test_a_listing_failure_says_so_instead_of_reporting_a_finished_run(self):
        self.dialog.yesno.return_value = True
        self._press({'total': 0, 'refreshed': 0, 'skipped': 0, 'errors': 1, 'cancelled': False, 'unreadable': True})
        self.assertEqual(self.dialog.notification.call_args[0][1], 'could not read')

    def test_a_stopped_run_says_how_far_it_got(self):
        self.dialog.yesno.return_value = True
        self._press({'total': 5, 'refreshed': 2, 'skipped': 0, 'errors': 0, 'cancelled': True})
        self.assertEqual(self.dialog.notification.call_args[0][1], 'Stopped 2/5')


if __name__ == '__main__':
    unittest.main()
