# -*- coding: utf-8 -*-
"""service.run(): what the movie add-on's main loop starts, and when (startup delays, intervals, the install guard, the status line)."""
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

import service
from lib import install_marker


class _Text(str):
    def format(self, *args, **kwargs):
        return str(self) + ''.join(' ' + str(a) for a in args)


class _Base(unittest.TestCase):
    """Runs service.run() for a fixed number of ticks, each advancing the fake clock by TICK seconds."""
    TICK = 100.0

    def setUp(self):
        stubs.reset_vfs()
        self.clock = [10_000.0]
        self.bools = {'sync_watch_ratings_enabled': True, 'sync_on_kodi_start': True, 'sync_collections_enabled': True,
                      'sync_collections_on_kodi_start': True}
        self.ints = {'sync_interval_minutes': 60, 'sync_collections_interval_minutes': 60, 'startup_delay_seconds': 10}
        self.addon = mock.MagicMock()
        self.addon.getSettingBool.side_effect = lambda sid: self.bools.get(sid, False)
        self.addon.getSettingInt.side_effect = lambda sid: self.ints.get(sid, 0)
        self.addon.getLocalizedString.side_effect = lambda i: _Text('S{0}'.format(i))
        self.addon.getAddonInfo.side_effect = lambda k: {'id': 'script.chronicle.scraper.movie', 'version': '3.21.0'}[k]
        self.monitor = mock.MagicMock()
        self.monitor.cleaning = False
        self.monitor.abortRequested.return_value = False
        self.monitor.run_watch_rating_sync.return_value = True
        self.monitor.run_collection_art_sync.return_value = True
        self.scanning = [False]
        self.activity = [None]
        self.bg = mock.MagicMock()
        self.threads = []

        class Thread:
            def __init__(self_, target=None, args=(), kwargs=None, name=None, daemon=None):
                self_.target, self_.args, self_.kwargs, self_.name = target, args, kwargs or {}, name
                outer.threads.append(name)

            def start(self_):
                pass
        outer = self
        for obj, name, new in (
                (service, 'ADDON', self.addon),
                (service.xbmcaddon, 'Addon', mock.Mock(return_value=self.addon)),
                (service, 'ChronicleMonitor', mock.Mock(return_value=self.monitor)),
                (service.threading, 'Thread', Thread),
                (service.time, 'time', mock.Mock(side_effect=lambda: self.clock[0])),
                (service.xbmc, 'getCondVisibility', mock.Mock(side_effect=lambda c: self.scanning[0])),
                (service.xbmcgui, 'DialogProgressBG', mock.Mock(return_value=self.bg)),
                (service.activity_tracker, 'read_activity', mock.Mock(side_effect=lambda: self.activity[0])),
                (service.activity_tracker, 'reset', mock.Mock()),
                (service.scan_progress, 'begin', mock.Mock()),
                (service.scan_progress, 'tick', mock.Mock()),
                (service, 'ChronicleClient', mock.MagicMock())):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, ticks, first_start=False):
        remaining = [ticks]

        def wait(seconds):
            self.clock[0] += self.TICK
            remaining[0] -= 1
            return remaining[0] <= 0
        self.monitor.waitForAbort.side_effect = wait
        with mock.patch.object(install_marker, 'is_first_start_of_version', return_value=first_start):
            service.run()


class TestStartupSchedule(_Base):
    def test_nothing_runs_before_the_startup_delay(self):
        self._run(ticks=1)
        self.monitor.run_watch_rating_sync.assert_not_called()
        self.monitor.run_collection_art_sync.assert_not_called()
        self.monitor.run_startup_refresh.assert_not_called()

    def test_after_the_delay_each_startup_task_runs_once_with_the_startup_label(self):
        self._run(ticks=3)
        self.monitor.run_watch_rating_sync.assert_called_once_with('startup')
        self.monitor.run_collection_art_sync.assert_called_once_with('startup')
        self.monitor.run_startup_refresh.assert_called_once()

    def test_the_startup_delay_setting_holds_every_startup_task_back(self):
        self.ints['startup_delay_seconds'] = 1000
        self._run(ticks=5)                                # 500 s: still inside the delay
        self.monitor.run_watch_rating_sync.assert_not_called()
        self.monitor.run_collection_art_sync.assert_not_called()
        self.monitor.run_startup_refresh.assert_not_called()

    def test_a_delay_of_zero_starts_straight_away(self):
        self.ints['startup_delay_seconds'] = 0
        self._run(ticks=1)
        self.monitor.run_startup_refresh.assert_called_once()
        self.monitor.run_watch_rating_sync.assert_called_once_with('startup')

    def test_turning_off_sync_on_kodi_start_skips_the_startup_pass_only(self):
        self.bools['sync_on_kodi_start'] = False
        self._run(ticks=3)
        self.monitor.run_watch_rating_sync.assert_not_called()
        self.monitor.run_collection_art_sync.assert_called_once()

    def test_a_disabled_master_switch_never_syncs(self):
        self.bools['sync_watch_ratings_enabled'] = False
        self.bools['sync_collections_enabled'] = False
        self._run(ticks=10)
        self.monitor.run_watch_rating_sync.assert_not_called()
        self.monitor.run_collection_art_sync.assert_not_called()

    def test_the_periodic_pass_runs_every_interval_after_the_startup_pass(self):
        self.ints['sync_interval_minutes'] = 30           # the 30-minute floor: 1800 s
        self._run(ticks=40)                               # 4000 s
        labels = [c[0][0] for c in self.monitor.run_watch_rating_sync.call_args_list]
        self.assertEqual(labels[0], 'startup')
        self.assertGreaterEqual(labels.count('scheduled'), 1)

    def test_an_interval_below_the_floor_is_raised_to_30_minutes(self):
        self.ints['sync_interval_minutes'] = 1
        self._run(ticks=10)                               # 1000 s: the startup pass only
        self.assertEqual([c[0][0] for c in self.monitor.run_watch_rating_sync.call_args_list], ['startup'])

    def test_a_deferred_pass_is_tried_again_soon_instead_of_a_whole_interval_later(self):
        self.monitor.run_watch_rating_sync.return_value = False       # always deferred behind a scan
        self._run(ticks=30)
        self.assertGreater(self.monitor.run_watch_rating_sync.call_count, 2)

    def test_the_first_start_of_a_version_holds_back_the_startup_refresh(self):
        self._run(ticks=3, first_start=True)
        self.monitor.run_startup_refresh.assert_not_called()
        self.monitor.run_watch_rating_sync.assert_called_once_with('startup')      # the syncs are unaffected

    def test_the_add_on_no_longer_has_anything_that_starts_a_library_scan(self):
        self.assertFalse(hasattr(service.kodi_scan_signal, 'check_and_scan'))
        self.assertFalse(hasattr(service.kodi_scan_signal, 'run_startup_scan_followup'))


class TestRegistrationAndHeartbeat(_Base):
    def test_the_device_registers_once_at_start_and_not_again(self):
        self._run(ticks=300)                              # well past the old 6-hour re-registration
        self.assertEqual(self.threads.count('chronicle-device-register'), 1)

    def test_scanning_sends_the_heartbeat_and_starts_progress_logging(self):
        self.scanning[0] = True
        self._run(ticks=2)
        self.assertIn('chronicle-scan-active-heartbeat', self.threads)
        self.assertIn('chronicle-scan-progress', self.threads)
        service.scan_progress.begin.assert_called_once()

    def test_the_end_of_a_scan_logs_a_final_progress_line(self):
        ticks = {'n': 0}

        def scanning(cond):
            ticks['n'] += 1
            return ticks['n'] == 1
        with mock.patch.object(service.xbmc, 'getCondVisibility', side_effect=scanning):
            self._run(ticks=3)
        self.assertIn('chronicle-scan-progress-final', self.threads)


class TestCornerStatus(_Base):
    def test_shown_while_busy_and_closed_when_idle(self):
        self.scanning[0] = True
        self._run(ticks=1)
        self.bg.create.assert_called_once()

    def test_closed_again_and_the_activity_count_reset_once_idle(self):
        calls = {'n': 0}

        def scanning(cond):
            calls['n'] += 1
            return calls['n'] <= 2          # busy for the first pass (two checks per pass), idle after
        with mock.patch.object(service.xbmc, 'getCondVisibility', side_effect=scanning):
            self._run(ticks=3)
        self.bg.close.assert_called()
        service.activity_tracker.reset.assert_called()

    def test_scraper_activity_alone_shows_the_status_with_its_label(self):
        self.activity[0] = {'timestamp': self.clock[0], 'count': 7, 'last_label': 'Heat (1995)'}
        self._run(ticks=1)
        self.assertIn('Heat (1995)', self.bg.create.call_args[0][1])


if __name__ == '__main__':
    unittest.main()
