# -*- coding: utf-8 -*-
"""default.py (TV add-on): the action dispatcher and the Library Repair dialogs (preview, repair, undo)."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

import default
from lib import library_repair


class _Text(str):
    """A localized string whose .format() tolerates any arguments."""
    def format(self, *args, **kwargs):
        return str(self) + ''.join(' ' + str(a) for a in args)


def _report(episodes=0, groups=0, stale=(), stuck=(), fabricated=0):
    return {'total_episodes': episodes, 'groups': [{'show_name': 'S{0}'.format(i)} for i in range(groups)], 'stale_shows': list(stale),
            'stuck_files': {'file_ids': list(stuck)}, 'fabricated_watched': {'episodes': list(range(fabricated)), 'groups': [{'show_name': 'X'}] * bool(fabricated)}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.dialog = mock.MagicMock()
        self.bg = mock.MagicMock()
        self.addon = mock.MagicMock()
        self.addon.getLocalizedString.side_effect = lambda i: _Text('S{0}'.format(i))
        for target, new in (('ADDON', self.addon),):
            p = mock.patch.object(default, target, new)
            p.start()
            self.addCleanup(p.stop)
        for name, new in (('Dialog', mock.Mock(return_value=self.dialog)), ('DialogProgressBG', mock.Mock(return_value=self.bg))):
            p = mock.patch.object(default.xbmcgui, name, new)
            p.start()
            self.addCleanup(p.stop)

    def _last_message(self):
        return self.dialog.ok.call_args[0][1]


class TestDispatch(_Base):
    def _run(self, *argv):
        names = ('_connect_to_chronicle', '_change_chronicle_url', '_test_connection', '_library_repair_explain', '_library_repair_preview',
                 '_library_repair', '_library_repair_undo', '_refresh_auth_status')
        mocks = {n: mock.patch.object(default, n).start() for n in names}
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(default.sys, 'argv', ['default.py'] + list(argv)):
            default.show_menu()
        return mocks

    def test_each_action_runs_only_its_own_handler(self):
        table = {'auth': '_connect_to_chronicle', 'change_url': '_change_chronicle_url', 'test_connection': '_test_connection',
                 'library_repair_explain': '_library_repair_explain', 'library_repair_preview': '_library_repair_preview',
                 'library_repair': '_library_repair', 'library_repair_undo': '_library_repair_undo'}
        for action, handler in table.items():
            mocks = self._run('action=' + action)
            mock.patch.stopall()
            for name, mocked in mocks.items():
                self.assertEqual(mocked.called, name == handler, '{0} for {1}'.format(name, action))
        self.addon.openSettings.assert_not_called()

    def test_no_action_opens_settings_after_refreshing_the_status(self):
        mocks = self._run()
        mocks['_refresh_auth_status'].assert_called_once()
        self.addon.openSettings.assert_called_once()


class TestProgress(_Base):
    def test_the_indicator_is_shown_at_once_and_advances_but_never_reaches_100(self):
        bg, update = default._progress_bg('Heading')
        self.bg.create.assert_called_once()
        for _ in range(8):
            update('working')
        percents = [c[0][0] for c in self.bg.update.call_args_list]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(max(percents), 90)

    def test_explain_is_just_a_dialog(self):
        default._library_repair_explain()
        self.dialog.ok.assert_called_once()


class TestPreview(_Base):
    def _preview(self, report=None, exc=None):
        with mock.patch.object(library_repair, 'preview', return_value=report, side_effect=exc):
            default._library_repair_preview()

    def test_clean_library(self):
        self._preview(_report())
        self.assertEqual(self._last_message(), 'S32163')
        self.addon.setSetting.assert_called_with('library_repair_last_result', 'S32163')

    def test_counts_and_notes_are_appended(self):
        self._preview(_report(episodes=5, groups=2, stale=['a'], stuck=[1, 2], fabricated=3))
        message = self._last_message()
        for expected in ('S32171 5 2', 'S32174 1', 'S32178 2', 'S32182 3 1'):
            self.assertIn(expected, message)
        self.bg.close.assert_called_once()

    def test_only_a_stale_show_uses_the_neutral_wording(self):
        self._preview(_report(stale=['a']))
        self.assertTrue(self._last_message().startswith('S32176'))

    def test_known_and_unknown_failures_are_reported_and_the_indicator_closed(self):
        err = library_repair.LibraryRepairError('code', 'Kodi is busy')
        self._preview(exc=err)
        self.assertEqual(self._last_message(), 'Kodi is busy')
        self._preview(exc=RuntimeError('boom'))
        self.assertIn('failed unexpectedly', self._last_message())
        self.assertEqual(self.bg.close.call_count, 2)


class TestRepair(_Base):
    def _repair(self, prepare, finish=None, confirmed=True, directories=(), scan_confirmed=False):
        self.dialog.yesno.side_effect = [confirmed, scan_confirmed]
        finish = finish or mock.Mock(return_value={'aborted': False, 'deleted_episodes': 5, 'backup_path': '/backup', 'repaired_stale_shows': [],
                                                   'deleted_stuck_files': 0})
        with mock.patch.object(library_repair, 'prepare_repair', **prepare), mock.patch.object(library_repair, 'finish_repair', finish), \
                mock.patch.object(library_repair, 'own_source_directories', return_value=list(directories)), \
                mock.patch.object(library_repair, 'trigger_scan') as scan:
            default._library_repair()
        return finish, scan

    def test_detection_failures_are_reported(self):
        self._repair({'side_effect': library_repair.LibraryRepairError('code', 'Kodi is busy')})
        self.assertEqual(self._last_message(), 'Kodi is busy')
        self._repair({'side_effect': RuntimeError('boom')})
        self.assertIn('failed unexpectedly', self._last_message())

    def test_nothing_to_repair_finishes_without_executing(self):
        finish, _ = self._repair({'return_value': ('/db', _report())})
        self.assertEqual(finish.call_args[1], {'execute': False})
        self.assertEqual(self._last_message(), 'S32163')

    def test_a_declined_confirmation_changes_nothing(self):
        finish, scan = self._repair({'return_value': ('/db', _report(episodes=3, groups=1))}, confirmed=False)
        self.assertEqual(finish.call_args[1], {'execute': False})
        self.dialog.ok.assert_not_called()
        scan.assert_not_called()

    def test_a_confirmed_repair_reports_the_backup_and_offers_a_scan_of_own_sources(self):
        finish, scan = self._repair({'return_value': ('/db', _report(episodes=5, groups=2))}, directories=['smb://n/TV/'], scan_confirmed=True)
        self.assertEqual(finish.call_args[1]['execute'], True)
        self.assertIn('S32164 5 2 /backup', self._last_message())
        scan.assert_called_once_with(['smb://n/TV/'])

    def test_no_scan_is_offered_without_own_sources(self):
        _, scan = self._repair({'return_value': ('/db', _report(episodes=5, groups=2))}, directories=[])
        scan.assert_not_called()

    def test_extra_results_are_appended(self):
        finish = mock.Mock(return_value={'aborted': False, 'deleted_episodes': 0, 'backup_path': None, 'repaired_stale_shows': [1, 2],
                                         'deleted_stuck_files': 4, 'fabricated_watched': {'cleared': 3, 'skipped': 1}})
        self._repair({'return_value': ('/db', _report(stale=['a']))}, finish=finish)
        message = self._last_message()
        self.assertTrue(message.startswith('S32176'))
        for expected in ('S32175 2', 'S32179 4', 'S32183 3 1'):
            self.assertIn(expected, message)

    def test_a_watched_clear_error_is_shown(self):
        finish = mock.Mock(return_value={'aborted': False, 'deleted_episodes': 1, 'backup_path': '/b', 'repaired_stale_shows': [],
                                         'fabricated_watched_error': 'Could not clear the marks'})
        self._repair({'return_value': ('/db', _report(episodes=1, groups=1))}, finish=finish)
        self.assertIn('Could not clear the marks', self._last_message())

    def test_an_aborted_run_names_the_backup_when_there_is_one(self):
        finish = mock.Mock(return_value={'aborted': True, 'abort_reason': 'locked', 'backup_path': '/b'})
        self._repair({'return_value': ('/db', _report(episodes=1, groups=1))}, finish=finish)
        self.assertIn('S32167 locked /b', self._last_message())
        finish = mock.Mock(return_value={'aborted': True, 'abort_reason': 'locked', 'backup_path': None})
        self._repair({'return_value': ('/db', _report(episodes=1, groups=1))}, finish=finish)
        self.assertIn('locked', self._last_message())

    def test_a_crash_during_the_real_pass_is_reported_and_the_indicator_closed(self):
        finish = mock.Mock(side_effect=RuntimeError('boom'))
        self._repair({'return_value': ('/db', _report(episodes=1, groups=1))}, finish=finish)
        self.assertIn('failed unexpectedly', self._last_message())
        self.bg.close.assert_called()


class TestUndo(_Base):
    def _undo(self, manifest, watched=None, confirmed=True, result=None, exc=None):
        self.dialog.yesno.return_value = confirmed
        with mock.patch.object(library_repair, 'peek_last_manifest', return_value=manifest), \
                mock.patch.object(library_repair, 'peek_fabricated_manifest', return_value=watched), \
                mock.patch.object(library_repair, 'run_undo', return_value=result, side_effect=exc) as run:
            default._library_repair_undo()
        return run

    def test_nothing_to_undo(self):
        run = self._undo(None)
        self.assertIn('nothing to undo', self._last_message())
        run.assert_not_called()

    def test_declined(self):
        run = self._undo({'episode_ids': [1, 2], 'created_at': 0}, confirmed=False)
        run.assert_not_called()
        self.dialog.ok.assert_not_called()

    def test_restores_and_reports_both_kinds(self):
        self._undo({'episode_ids': [1, 2], 'created_at': 0}, watched={'episodes': [1]},
                   result={'aborted': False, 'restored_episodes': 2, 'restored_watched': {'restored': 1}})
        self.assertIn('S32169 2', self._last_message())
        self.assertIn('S32185 1', self._last_message())

    def test_watched_only_manifest(self):
        run = self._undo(None, watched={'episodes': [1, 2]}, result={'aborted': False, 'restored_episodes': 0, 'restored_watched': {'restored': 2}})
        run.assert_called_once()

    def test_aborted_and_failed(self):
        self._undo({'episode_ids': [1], 'created_at': 0}, result={'aborted': True, 'abort_reason': 'locked'})
        self.assertIn('S32170 locked', self._last_message())
        self._undo({'episode_ids': [1], 'created_at': 0}, exc=library_repair.LibraryRepairError('code', 'Kodi is busy'))
        self.assertEqual(self._last_message(), 'Kodi is busy')
        self._undo({'episode_ids': [1], 'created_at': 0}, exc=RuntimeError('boom'))
        self.assertIn('failed unexpectedly', self._last_message())


if __name__ == '__main__':
    unittest.main()
