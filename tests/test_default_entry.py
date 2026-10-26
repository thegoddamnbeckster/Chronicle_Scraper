# -*- coding: utf-8 -*-
"""default.py (movie add-on): the RunScript(action=...) dispatcher behind the Settings buttons."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

import default


class TestDispatch(unittest.TestCase):
    def _run(self, *argv):
        with mock.patch.object(default.sys, 'argv', ['default.py'] + list(argv)), \
                mock.patch.object(default, '_connect_to_chronicle') as connect, \
                mock.patch.object(default, '_change_chronicle_url') as change, \
                mock.patch.object(default, '_test_connection') as test, \
                mock.patch.object(default, '_sync_watch_ratings_now') as watch, \
                mock.patch.object(default, '_sync_collections_now') as collections, \
                mock.patch.object(default, '_refresh_auth_status') as refresh, \
                mock.patch.object(default, 'ADDON') as addon:
            default.show_menu()
        return {'auth': connect, 'change_url': change, 'test_connection': test, 'sync_watch_ratings': watch,
                'sync_collections': collections, 'refresh': refresh, 'addon': addon}

    def test_each_action_runs_only_its_own_handler_and_does_not_open_settings(self):
        for action in ('auth', 'change_url', 'test_connection', 'sync_watch_ratings', 'sync_collections'):
            calls = self._run('action=' + action)
            for name, mocked in calls.items():
                if name == 'addon':
                    mocked.openSettings.assert_not_called()
                elif name == 'refresh':
                    mocked.assert_not_called()
                else:
                    self.assertEqual(mocked.called, name == action, '{0} with action={1}'.format(name, action))

    def test_no_action_refreshes_the_status_then_opens_settings(self):
        calls = self._run()
        calls['refresh'].assert_called_once()
        calls['addon'].openSettings.assert_called_once()

    def test_an_unknown_action_falls_through_to_settings(self):
        calls = self._run('action=bogus')
        calls['addon'].openSettings.assert_called_once()


class TestManualSyncs(unittest.TestCase):
    def setUp(self):
        self.dialog = mock.MagicMock()
        self.bg = mock.MagicMock()
        self.addon = mock.MagicMock()
        self.addon.getLocalizedString.side_effect = lambda i: {32135: 'Updated {0}', 32136: ' ({0} errors)', 32147: '{0} collections{1}',
                                                              32165: 'Already up to date', 32162: '{0} movies'}.get(i, 's{0}'.format(i))
        for target, new in (('ADDON', self.addon),):
            p = mock.patch.object(default, target, new)
            p.start()
            self.addCleanup(p.stop)
        for name, new in (('Dialog', mock.Mock(return_value=self.dialog)), ('DialogProgressBG', mock.Mock(return_value=self.bg))):
            p = mock.patch.object(default.xbmcgui, name, new)
            p.start()
            self.addCleanup(p.stop)

    def _notification(self):
        return self.dialog.notification.call_args

    def test_watch_sync_with_nothing_changed_still_answers_the_button(self):
        with mock.patch.object(default.watch_rating_sync, 'run', return_value={'errors': 0}), \
                mock.patch.object(default.watch_rating_sync, 'describe_result', return_value=None):
            default._sync_watch_ratings_now()
        self.assertEqual(self._notification()[0][1], 'Already up to date')
        self.bg.close.assert_called_once()

    def test_watch_sync_reports_what_changed_and_warns_on_errors(self):
        with mock.patch.object(default.watch_rating_sync, 'run', return_value={'errors': 2}), \
                mock.patch.object(default.watch_rating_sync, 'describe_result', return_value='Updated 3 movies'):
            default._sync_watch_ratings_now()
        self.assertEqual(self._notification()[0][1], 'Updated 3 movies')
        self.assertEqual(self._notification()[1]['icon'], default.xbmcgui.NOTIFICATION_WARNING)

    def test_progress_is_forwarded_and_the_dialog_closed_even_when_the_pass_raises(self):
        def run(progress_callback=None):
            progress_callback(1, 4, 'Heat (1995)')
            raise RuntimeError('rpc down')
        with mock.patch.object(default.watch_rating_sync, 'run', side_effect=run):
            with self.assertRaises(RuntimeError):
                default._sync_watch_ratings_now()
        self.bg.update.assert_called_once_with(25, message='Heat (1995)')
        self.bg.close.assert_called_once()

    def test_collection_sync_compares_everything_and_reports_what_changed(self):
        with mock.patch.object(default.collection_art_sync, 'run',
                               return_value={'collections': 7, 'changed': 3, 'errors': 0}) as run:
            default._sync_collections_now()
        self.assertTrue(run.call_args[1]['everything'])
        self.assertEqual(self._notification()[0][1], '3 collections')

    def test_collection_sync_warns_about_errors(self):
        with mock.patch.object(default.collection_art_sync, 'run',
                               return_value={'collections': 7, 'changed': 1, 'errors': 2}):
            default._sync_collections_now()
        self.assertEqual(self._notification()[0][1], '1 collections (2 errors)')
        self.assertEqual(self._notification()[1]['icon'], default.xbmcgui.NOTIFICATION_WARNING)

    def test_collection_sync_with_nothing_changed_still_answers_the_button(self):
        with mock.patch.object(default.collection_art_sync, 'run',
                               return_value={'collections': 7, 'changed': 0, 'errors': 0}):
            default._sync_collections_now()
        self.assertEqual(self._notification()[0][1], 'Already up to date')


class TestOnConnected(unittest.TestCase):
    def test_registration_runs_on_a_background_thread(self):
        started = []
        with mock.patch.object(default.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = lambda: started.append(True)
            default._on_connected()
        self.assertEqual(started, [True])
        self.assertIs(thread.call_args[1]['target'], default.device_registration.register)
        self.assertTrue(thread.call_args[1]['daemon'])


if __name__ == '__main__':
    unittest.main()
