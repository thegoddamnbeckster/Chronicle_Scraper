# -*- coding: utf-8 -*-
"""TV service.py: the post-scan check (deferral, cooldown, notifications) and the main loop's startup rules."""
import time
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

import service
from lib import install_marker


class _Text(str):
    """A localized string whose .format() tolerates any arguments."""
    def format(self, *args, **kwargs):
        return str(self) + ''.join(' ' + str(a) for a in args)


class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _Base(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.addon = mock.MagicMock()
        self.addon.getLocalizedString.side_effect = lambda i: _Text('S{0}'.format(i))
        self.addon.getSettingBool.return_value = True
        self.addon.getSettingInt.return_value = 120
        self.addon.getAddonInfo.side_effect = lambda k: {'id': 'script.chronicle.scraper.tv', 'version': '1.18.0'}[k]
        self.dialog = mock.MagicMock()
        self.scanning = [False]
        self.recent = [False]
        for obj, name, new in ((service, 'ADDON', self.addon), (install_marker.xbmcaddon, 'Addon', mock.Mock(return_value=self.addon)),
                               (service.xbmcgui, 'Dialog', mock.Mock(return_value=self.dialog)),
                               (service.xbmc, 'getCondVisibility', mock.Mock(side_effect=lambda cond: self.scanning[0])),
                               (service.activity_tracker, 'is_recently_active', mock.Mock(side_effect=lambda seconds: self.recent[0])),
                               (service.threading, 'Thread', _SyncThread)):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)
        self.monitor = service.ChronicleTVMonitor()


class TestFullSyncCheck(_Base):
    def setUp(self):
        super().setUp()
        self.progress = mock.MagicMock()
        for obj, name, new in ((service.full_sync_check, 'run', mock.Mock(return_value={'checked': 5, 'updated': 2, 'errors': 0, 'cancelled': False, 'aborted': None})),
                               (service.pass_progress, 'PassProgress', mock.Mock(return_value=self.progress))):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)
        self.run = service.full_sync_check.run

    def test_deferral_follows_scans_and_scraper_activity(self):
        self.assertFalse(self.monitor._should_defer_for_active_scan('t'))
        self.scanning[0] = True
        self.assertTrue(self.monitor._should_defer_for_active_scan('t'))
        self.scanning[0], self.recent[0] = False, True
        self.assertTrue(self.monitor._should_defer_for_active_scan('t'))

    def test_only_the_video_library_triggers_it(self):
        self.monitor.onScanFinished('music')
        self.run.assert_not_called()
        self.monitor.onScanFinished('video')
        self.run.assert_called_once()
        self.progress.finish.assert_called_once()

    def test_disabled_by_setting(self):
        self.addon.getSettingBool.return_value = False
        self.monitor.run_full_sync_check('scan finished')
        self.run.assert_not_called()

    def test_cooldown_and_overlap(self):
        self.monitor.run_full_sync_check('scan finished')
        self.monitor.run_full_sync_check('scan finished')
        self.assertEqual(self.run.call_count, 1)
        self.monitor._full_sync_check_last_completed_at = time.time() - service._FULL_SYNC_CHECK_MIN_SECONDS_BETWEEN_RUNS - 1
        self.monitor._full_sync_check_lock.acquire()
        self.monitor.run_full_sync_check('scan finished')
        self.assertEqual(self.run.call_count, 1)

    def test_waits_for_activity_and_gives_up_on_abort_without_starting_the_cooldown(self):
        self.scanning[0] = True
        with mock.patch.object(self.monitor, 'waitForAbort', return_value=True):
            self.monitor.run_full_sync_check('scan finished')
        self.run.assert_not_called()
        self.assertEqual(self.monitor._full_sync_check_last_completed_at, 0.0)
        self.assertTrue(self.monitor._full_sync_check_lock.acquire(False))

    def test_a_crash_releases_the_lock_and_closes_the_progress(self):
        self.run.side_effect = RuntimeError('rpc down')
        self.monitor.run_full_sync_check('scan finished')
        self.progress.finish.assert_called_once()
        self.assertTrue(self.monitor._full_sync_check_lock.acquire(False))

    def test_the_cancel_callback_stops_the_sweep_when_a_scan_starts(self):
        captured = {}

        def run(is_cancelled=None, progress_callback=None):
            captured['cancel'] = is_cancelled
            self.scanning[0] = True
            captured['mid_scan'] = is_cancelled()
            return {'checked': 0, 'updated': 0, 'errors': 0, 'cancelled': True, 'aborted': None}
        self.run.side_effect = run
        self.monitor.run_full_sync_check('scan finished')
        self.assertTrue(captured['mid_scan'])

    def test_result_notifications(self):
        info, warning = service.xbmcgui.NOTIFICATION_INFO, service.xbmcgui.NOTIFICATION_WARNING
        for result, icon in (({'aborted': 'down'}, warning), ({'cancelled': True}, info), ({'checked': 3, 'updated': 1}, info),
                             ({'checked': 3, 'updated': 1, 'errors': 2}, warning)):
            self.monitor._notify_full_sync_check_result(result)
            self.assertEqual(self.dialog.notification.call_args[1]['icon'], icon, result)
        self.dialog.notification.side_effect = RuntimeError('ui gone')
        self.monitor._notify_full_sync_check_result({'checked': 1})   # swallowed


class TestScanSignalAndFollowup(_Base):
    def test_one_check_at_a_time_and_errors_release_the_lock(self):
        with mock.patch.object(service.kodi_scan_signal, 'check_and_scan') as check:
            self.monitor.run_scan_signal_check()
            self.assertEqual(check.call_count, 1)
            self.monitor._scan_signal_lock.acquire()
            self.monitor.run_scan_signal_check()
            self.assertEqual(check.call_count, 1)
            self.monitor._scan_signal_lock.release()
        with mock.patch.object(service.kodi_scan_signal, 'check_and_scan', side_effect=RuntimeError('x')):
            self.monitor.run_scan_signal_check()
        self.assertTrue(self.monitor._scan_signal_lock.acquire(False))

    def test_followup_asks_for_episode_and_show_refreshes(self):
        with mock.patch.object(service.kodi_scan_signal, 'run_startup_scan_followup') as followup:
            self.monitor.run_startup_scan_followup(5.0)
        self.assertEqual(followup.call_args[0], (5.0, ['episode', 'tvshow']))
        with mock.patch.object(service.kodi_scan_signal, 'run_startup_scan_followup', side_effect=RuntimeError('x')):
            self.monitor.run_startup_scan_followup(5.0)


class TestMainLoop(_Base):
    """run() drives the monitor; the clock is faked so the 60 s / 10 s startup delays pass in one tick."""

    def setUp(self):
        super().setUp()
        self.addon.getSettingBool.side_effect = lambda sid: sid == 'scan_signal_enabled'
        self.monitor_instance = mock.MagicMock()
        self.monitor_instance.abortRequested.return_value = False
        self.clock = [1000.0]
        for obj, name, new in ((service, 'ChronicleTVMonitor', mock.Mock(return_value=self.monitor_instance)),
                               (service.activity_tracker, 'read_activity', mock.Mock(return_value=None)),
                               (service, 'ChronicleClient', mock.MagicMock()),
                               (service.time, 'time', mock.Mock(side_effect=lambda: self.clock[0]))):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, first_start):
        with mock.patch.object(install_marker, 'is_first_start_of_version', return_value=first_start):
            ticks = iter([False, True])

            def wait(seconds):
                self.clock[0] += 120
                return next(ticks)
            self.monitor_instance.waitForAbort.side_effect = wait
            service.run()

    def test_a_normal_start_checks_for_new_content_once_the_delay_has_passed(self):
        self._run(first_start=False)
        self.monitor_instance.run_startup_scan_followup.assert_called_once()
        self.assertTrue(self.monitor_instance.run_scan_signal_check.called)

    def test_the_first_start_of_a_new_version_scans_nothing(self):
        self._run(first_start=True)
        self.monitor_instance.run_startup_scan_followup.assert_not_called()
        # the recurring check (scan_signal_enabled) is also held back for its first interval
        self.monitor_instance.run_scan_signal_check.assert_not_called()

    def test_the_heartbeat_tells_chronicle_a_scan_is_running(self):
        self.scanning[0] = True
        self._run(first_start=True)
        service.ChronicleClient.return_value.report_scan_active.assert_called()


class TestInstallMarkerPerAddon(unittest.TestCase):
    def test_each_addon_keeps_its_own_marker(self):
        stubs.reset_vfs()
        with mock.patch.object(install_marker.xbmcaddon, 'Addon') as addon_cls:
            addon_cls.return_value.getAddonInfo.return_value = 'script.chronicle.scraper.tv'
            self.assertTrue(install_marker.is_first_start_of_version('1.0'))
            self.assertFalse(install_marker.is_first_start_of_version('1.0'))
            addon_cls.return_value.getAddonInfo.return_value = 'script.chronicle.scraper.movie'
            self.assertTrue(install_marker.is_first_start_of_version('1.0'))   # the movie add-on's own first start
        self.assertIn('special://profile/addon_data/script.chronicle.scraper.tv/installed_version.txt', stubs._FAKE_FILES)
        self.assertIn('special://profile/addon_data/script.chronicle.scraper.movie/installed_version.txt', stubs._FAKE_FILES)


if __name__ == '__main__':
    unittest.main()
