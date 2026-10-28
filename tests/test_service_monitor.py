# -*- coding: utf-8 -*-
"""service.py's ChronicleMonitor: deferral behind scans, the per-task locks, the cooldown and the result notifications."""
import threading
import time
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

import service


class _Text(str):
    """A localized string whose .format() tolerates any arguments (the real ones are '{0}'/'{1}' templates)."""
    def format(self, *args, **kwargs):
        return str(self) + ''.join(' ' + str(a) for a in args)


class _SyncThread:
    """threading.Thread stand-in that runs the target immediately on start()."""
    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _Base(unittest.TestCase):
    def setUp(self):
        self.addon = mock.MagicMock()
        self.addon.getLocalizedString.side_effect = lambda i: _Text('S{0}'.format(i))
        self.addon.getSettingBool.return_value = True
        self.dialog = mock.MagicMock()
        self.scanning = [False]
        for obj, target, new in ((service, 'ADDON', self.addon), (service.xbmcaddon, 'Addon', mock.Mock(return_value=self.addon))):
            p = mock.patch.object(obj, target, new)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(service.xbmcgui, 'Dialog', return_value=self.dialog)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(service.xbmc, 'getCondVisibility', side_effect=lambda cond: self.scanning[0])
        p.start()
        self.addCleanup(p.stop)
        self.recent = [False]
        p = mock.patch.object(service.activity_tracker, 'is_recently_active', side_effect=lambda seconds: self.recent[0])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(service.threading, 'Thread', _SyncThread)
        p.start()
        self.addCleanup(p.stop)
        self.monitor = service.ChronicleMonitor()


class TestAnchor(unittest.TestCase):
    def test_a_started_pass_restarts_the_interval(self):
        self.assertEqual(service._anchor_after_attempt(1000.0, 7200, True), 1000.0)

    def test_a_deferred_pass_is_due_again_soon_not_a_whole_interval_later(self):
        anchor = service._anchor_after_attempt(1000.0, 7200, False)
        self.assertEqual(1000.0 - anchor + 0, 7200 - service._DEFERRED_RETRY_SECONDS)   # next due in _DEFERRED_RETRY_SECONDS
        short = service._anchor_after_attempt(1000.0, 60, False)
        self.assertEqual(short, 1000.0 - 60 + 60)   # an interval shorter than the retry delay is not stretched


class TestDeferral(_Base):
    def test_idle_library_does_not_defer_and_rearms_the_notice(self):
        self.monitor._defer_notice_shown = True
        self.assertFalse(self.monitor._should_defer_for_active_scan('task'))
        self.assertFalse(self.monitor._defer_notice_shown)

    def test_a_kodi_scan_or_scraper_activity_defers_and_says_so_once_per_busy_period(self):
        self.scanning[0] = True
        self.assertTrue(self.monitor._should_defer_for_active_scan('task'))
        self.assertTrue(self.monitor._should_defer_for_active_scan('task'))
        self.dialog.notification.assert_called_once()
        self.scanning[0], self.recent[0] = False, True
        self.assertTrue(self.monitor._should_defer_for_active_scan('task'))
        self.scanning[0] = self.recent[0] = False
        self.monitor._should_defer_for_active_scan('task')       # idle again: re-armed
        self.recent[0] = True
        self.monitor._should_defer_for_active_scan('task')
        self.assertEqual(self.dialog.notification.call_count, 2)


class TestWatchRatingSync(_Base):
    RESULT = {'movies': 1, 'shows': 1, 'episodes': 2, 'errors': 0, 'cancelled': False,
              'movies_changed': 1, 'shows_changed': 0, 'episodes_changed': 0}

    def setUp(self):
        super().setUp()
        self.progress = mock.MagicMock()
        for target, new in (('run', mock.Mock(return_value=dict(self.RESULT))), ('describe_result', mock.Mock(return_value='Updated 1'))):
            p = mock.patch.object(service.watch_rating_sync, target, new)
            setattr(self, target, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(service.pass_progress, 'PassProgress', return_value=self.progress)
        p.start()
        self.addCleanup(p.stop)

    def test_deferred_behind_a_scan_returns_false_and_runs_nothing(self):
        self.scanning[0] = True
        self.assertFalse(self.monitor.run_watch_rating_sync('scheduled'))
        self.run.assert_not_called()

    def test_runs_notifies_what_changed_and_releases_the_lock(self):
        self.assertTrue(self.monitor.run_watch_rating_sync('scheduled'))
        self.run.assert_called_once()
        self.assertEqual(self.dialog.notification.call_args[0][1], 'Updated 1')
        self.progress.start.assert_called_once()
        self.progress.finish.assert_called_once()
        self.assertTrue(self.monitor._watch_rating_lock.acquire(False))     # released

    def test_an_error_pass_warns(self):
        self.run.return_value = dict(self.RESULT, errors=1)
        self.monitor.run_watch_rating_sync('scheduled')
        self.assertEqual(self.dialog.notification.call_args[1]['icon'], service.xbmcgui.NOTIFICATION_WARNING)

    def test_silent_when_nothing_changed_or_cancelled(self):
        self.describe_result.return_value = None
        self.monitor.run_watch_rating_sync('scheduled')
        self.dialog.notification.assert_not_called()
        self.run.return_value = dict(self.RESULT, cancelled=True)
        self.monitor.run_watch_rating_sync('scheduled')
        self.dialog.notification.assert_not_called()

    def test_a_crashing_pass_still_closes_the_progress_and_releases_the_lock(self):
        self.run.side_effect = RuntimeError('rpc down')
        self.assertTrue(self.monitor.run_watch_rating_sync('scheduled'))
        self.progress.finish.assert_called_once()
        self.assertTrue(self.monitor._watch_rating_lock.acquire(False))

    def test_an_overlapping_pass_is_skipped_but_counts_as_covered(self):
        self.monitor._watch_rating_lock.acquire()
        self.assertTrue(self.monitor.run_watch_rating_sync('scheduled'))
        self.run.assert_not_called()


class TestCollectionArtSync(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(service.collection_art_sync, 'run', return_value={'collections': 3, 'changed': 3, 'errors': 0, 'cancelled': False})
        self.run = p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(service.xbmcgui, 'DialogProgressBG')
        self.bg = q.start().return_value
        self.addCleanup(q.stop)

    def test_defers_runs_and_reports(self):
        self.scanning[0] = True
        self.assertFalse(self.monitor.run_collection_art_sync('scheduled'))
        self.run.assert_not_called()
        self.scanning[0] = False
        self.dialog.notification.reset_mock()
        self.assertTrue(self.monitor.run_collection_art_sync('scheduled'))
        self.run.assert_called_once()
        self.dialog.notification.assert_called_once()

    def test_progress_opens_lazily_and_closes(self):
        def run(is_cancelled=None, progress_callback=None):
            progress_callback(1, 4, 'Heat Collection')
            return {'collections': 1, 'changed': 1, 'errors': 0, 'cancelled': False}
        self.run.side_effect = run
        self.monitor.run_collection_art_sync('scheduled')
        self.bg.update.assert_called_once_with(25, message='Heat Collection')
        self.bg.close.assert_called_once()

    def test_a_pass_that_changed_nothing_is_silent(self):
        self.run.return_value = {'collections': 3, 'changed': 0, 'errors': 0, 'cancelled': False}
        self.monitor.run_collection_art_sync('scheduled')
        self.dialog.notification.assert_not_called()

    def test_errors_warn(self):
        self.run.return_value = {'collections': 3, 'changed': 0, 'errors': 2, 'cancelled': False}
        self.monitor.run_collection_art_sync('scheduled')
        self.assertEqual(self.dialog.notification.call_args[1]['icon'], service.xbmcgui.NOTIFICATION_WARNING)

    def test_a_cancelled_pass_is_silent_and_the_lock_is_released(self):
        self.run.return_value = {'collections': 1, 'changed': 1, 'errors': 0, 'cancelled': True}
        self.monitor.run_collection_art_sync('scheduled')
        self.dialog.notification.assert_not_called()
        self.assertTrue(self.monitor._collection_art_lock.acquire(False))


class TestPostScanPass(_Base):
    WATCH = {'movies': 1, 'shows': 0, 'episodes': 0, 'errors': 0, 'cancelled': False,
             'movies_changed': 0, 'shows_changed': 0, 'episodes_changed': 0}
    COLLECTIONS = {'collections': 0, 'changed': 0, 'errors': 0, 'cancelled': False}

    def setUp(self):
        super().setUp()
        self.order = []
        self.progress = mock.MagicMock()
        self.watch = mock.Mock(side_effect=lambda **kw: self.order.append('watch') or dict(self.WATCH))
        self.collections = mock.Mock(side_effect=lambda **kw: self.order.append('collections') or dict(self.COLLECTIONS))
        for obj, name, new in ((service.watch_rating_sync, 'run', self.watch),
                               (service.collection_art_sync, 'run', self.collections),
                               (service.pass_progress, 'PassProgress', mock.Mock(return_value=self.progress)),
                               (service.xbmcgui, 'DialogProgressBG', mock.MagicMock())):
            p = mock.patch.object(obj, name, new)
            p.start()
            self.addCleanup(p.stop)

    def test_disabled_by_setting(self):
        self.addon.getSettingBool.return_value = False
        self.monitor.run_post_scan_pass('scan finished')
        self.watch.assert_not_called()
        self.collections.assert_not_called()

    def test_scan_finished_runs_it_for_the_video_library_only(self):
        self.monitor.onScanFinished('music')
        self.watch.assert_not_called()
        self.monitor.onScanFinished('video')
        self.watch.assert_called_once()

    def test_the_watch_sync_runs_first_and_then_the_collection_art_sync(self):
        self.monitor.onScanFinished('video')
        self.assertEqual(self.order, ['watch', 'collections'])

    def test_a_pass_just_completed_blocks_the_cascade_of_scan_finished_events(self):
        self.monitor.run_post_scan_pass('scan finished')
        self.monitor.run_post_scan_pass('scan finished')
        self.assertEqual(self.watch.call_count, 1)
        self.monitor._post_scan_last_completed_at = time.time() - service._POST_SCAN_MIN_SECONDS_BETWEEN_RUNS - 1
        self.monitor.run_post_scan_pass('scan finished')
        self.assertEqual(self.watch.call_count, 2)

    def test_a_running_pass_blocks_an_overlapping_one(self):
        self.monitor._post_scan_lock.acquire()
        self.monitor.run_post_scan_pass('scan finished')
        self.watch.assert_not_called()

    def test_it_waits_for_other_activity_and_gives_up_if_aborted(self):
        self.scanning[0] = True
        with mock.patch.object(self.monitor, 'waitForAbort', return_value=True):
            self.monitor.run_post_scan_pass('scan finished')
        self.watch.assert_not_called()
        self.collections.assert_not_called()
        self.assertTrue(self.monitor._post_scan_lock.acquire(False))
        self.assertEqual(self.monitor._post_scan_last_completed_at, 0.0)   # no work done: the cooldown is not started

    def test_a_crash_releases_the_lock_and_does_not_start_the_cooldown(self):
        self.watch.side_effect = RuntimeError('rpc down')
        self.monitor.run_post_scan_pass('scan finished')
        self.assertTrue(self.monitor._post_scan_lock.acquire(False))
        self.assertEqual(self.monitor._post_scan_last_completed_at, 0.0)

    def test_a_periodic_watch_pass_already_running_is_not_run_twice_but_the_collections_still_are(self):
        self.monitor._watch_rating_lock.acquire()
        self.monitor.run_post_scan_pass('scan finished')
        self.watch.assert_not_called()
        self.collections.assert_called_once()

    def test_the_collection_sync_waits_for_the_scraper_the_first_pass_kept_busy(self):
        # The watch pass can ask Kodi to re-scrape an item, which keeps the scraper busy for a while.
        self.watch.side_effect = lambda **kw: self.recent.__setitem__(0, True) or dict(self.WATCH)
        with mock.patch.object(self.monitor, 'waitForAbort', return_value=True):
            self.monitor.run_post_scan_pass('scan finished')
        self.watch.assert_called_once()
        self.collections.assert_not_called()
        self.assertTrue(self.monitor._post_scan_lock.acquire(False))

    def test_both_passes_stop_when_a_new_scan_starts_mid_way(self):
        self.monitor.run_post_scan_pass('scan finished')
        for run in (self.watch, self.collections):
            is_cancelled = run.call_args[1]['is_cancelled']
            self.scanning[0] = False
            self.assertFalse(is_cancelled())
            self.scanning[0] = True
            self.assertTrue(is_cancelled())

    def test_locks_are_released_after_the_pass(self):
        self.monitor.run_post_scan_pass('scan finished')
        self.assertTrue(self.monitor._post_scan_lock.acquire(False))
        self.assertTrue(self.monitor._watch_rating_lock.acquire(False))
        self.assertTrue(self.monitor._collection_art_lock.acquire(False))


class TestStartupRefresh(_Base):
    def test_passes_the_movie_kind_and_the_abort_check(self):
        with mock.patch.object(service.kodi_scan_signal, 'run_startup_refresh') as refresh:
            self.monitor.run_startup_refresh()
        self.assertEqual(refresh.call_args[0], (['movie'],))
        self.assertEqual(refresh.call_args[1]['is_aborted'], self.monitor.abortRequested)

    def test_the_service_has_no_scan_triggers_left(self):
        for name in ('run_scan_signal_check', 'run_startup_scan_followup', 'run_full_sync_check'):
            self.assertFalse(hasattr(self.monitor, name), name)

    def test_clean_state_follows_the_video_library_only(self):
        self.monitor.onCleanStarted('music')
        self.assertFalse(self.monitor.cleaning)
        self.monitor.onCleanStarted('video')
        self.assertTrue(self.monitor.cleaning)
        self.monitor.onCleanFinished('video')
        self.assertFalse(self.monitor.cleaning)


if __name__ == '__main__':
    unittest.main()
