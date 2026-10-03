# -*- coding: utf-8 -*-
"""
Tests for lib/pass_progress.py -- the corner progress every background pass shows (added 2026-10-03).

The post-scan "verify every movie/episode" checks and the watch/ratings sync write into the library,
which makes Kodi's screens refresh. They used to do that with no on-screen explanation at all (the
post-scan checks) or a bare percentage (the sync), which is alarming for someone who doesn't know
what is going on.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

from lib.pass_progress import PassProgress


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _progress(clock=None, dialog=None, **kwargs):
    dialog = dialog or MagicMock()
    log = MagicMock()
    progress = PassProgress('Verifying movies', 'Starting -- the screen may refresh', log,
                            dialog_factory=lambda: dialog, clock=clock or _Clock(), **kwargs)
    return progress, dialog, log


class TestStart(unittest.TestCase):

    def test_the_dialog_opens_immediately_with_the_heading_and_the_flicker_warning(self):
        progress, dialog, log = _progress()

        progress.start()

        dialog.create.assert_called_once_with('Verifying movies', 'Starting -- the screen may refresh')
        log.info.assert_called_once_with('Verifying movies: starting')

    def test_update_without_start_still_opens_the_dialog(self):
        progress, dialog, _log = _progress()

        progress.update(0, 10, 'First Movie (2000)')

        dialog.create.assert_called_once()


class TestUpdate(unittest.TestCase):

    def test_shows_position_total_label_and_percent(self):
        progress, dialog, _log = _progress()
        progress.start()

        progress.update(411, 1227, 'Red Planet (2000)')

        dialog.update.assert_called_once_with(33, message='412 of 1,227  --  Red Planet (2000)')

    def test_the_first_update_always_shows_then_ui_updates_are_throttled(self):
        clock = _Clock()
        progress, dialog, _log = _progress(clock=clock, ui_interval_seconds=1.0)
        progress.start()

        progress.update(0, 100, 'A')
        clock.advance(0.2)
        progress.update(1, 100, 'B')        # too soon: not drawn
        clock.advance(0.2)
        progress.update(2, 100, 'C')        # still too soon
        clock.advance(1.0)
        progress.update(3, 100, 'D')        # drawn

        shown = [c.kwargs['message'] for c in dialog.update.call_args_list]
        self.assertEqual(shown, ['1 of 100  --  A', '4 of 100  --  D'])

    def test_a_long_title_is_truncated(self):
        progress, dialog, _log = _progress()
        progress.start()

        progress.update(0, 5, 'X' * 200)

        message = dialog.update.call_args.kwargs['message']
        self.assertLessEqual(len(message), len('1 of 5  --  ') + 60)
        self.assertTrue(message.endswith('…'))

    def test_an_unknown_total_does_not_divide_by_zero(self):
        progress, dialog, _log = _progress()
        progress.start()

        progress.update(7, 0, 'Whatever')

        dialog.update.assert_called_once_with(0, message='7 done  --  Whatever')

    def test_percent_never_exceeds_100(self):
        progress, dialog, _log = _progress()
        progress.start()

        progress.update(150, 100, 'Over')

        self.assertEqual(dialog.update.call_args.args[0], 100)


class TestLogging(unittest.TestCase):

    def test_logs_a_progress_line_every_interval_not_every_item(self):
        clock = _Clock()
        progress, _dialog, log = _progress(clock=clock, log_interval_seconds=60.0)
        progress.start()                    # logs "starting"
        log.info.reset_mock()

        for i in range(5):
            clock.advance(10)
            progress.update(i, 100, 'Title {0}'.format(i))
        self.assertEqual(log.info.call_count, 0)     # 50s in: nothing yet

        clock.advance(15)
        progress.update(5, 100, 'Title 5')
        log.info.assert_called_once()
        line = log.info.call_args.args[0]
        self.assertIn('6 of 100 (5%)', line)
        self.assertIn('Title 5', line)
        self.assertIn('65s elapsed', line)


class TestFinishAndFailure(unittest.TestCase):

    def test_finish_closes_the_dialog(self):
        progress, dialog, _log = _progress()
        progress.start()

        progress.finish()

        dialog.close.assert_called_once()

    def test_finish_is_safe_when_nothing_ever_started(self):
        progress, dialog, _log = _progress()

        progress.finish()                   # must not raise

        dialog.close.assert_not_called()

    def test_finish_twice_closes_once(self):
        progress, dialog, _log = _progress()
        progress.start()

        progress.finish()
        progress.finish()

        dialog.close.assert_called_once()

    def test_an_update_after_finish_does_not_reopen_the_dialog(self):
        progress, dialog, _log = _progress()
        progress.start()
        progress.finish()

        progress.update(5, 10, 'Late')

        dialog.create.assert_called_once()        # only the original open
        dialog.close.assert_called_once()

    def test_a_broken_dialog_never_breaks_the_pass_and_is_not_retried(self):
        dialog = MagicMock()
        dialog.create.side_effect = RuntimeError('no GUI')
        progress, _dialog, log = _progress(dialog=dialog)

        progress.start()                    # must not raise
        progress.update(0, 10, 'A')
        progress.update(1, 10, 'B')
        progress.finish()

        self.assertEqual(dialog.create.call_count, 1)
        log.warning.assert_called_once()
        self.assertIn('continuing without it', log.warning.call_args.args[0])

    def test_a_dialog_that_fails_midway_is_closed_not_left_on_screen(self):
        dialog = MagicMock()
        dialog.update.side_effect = RuntimeError('boom')
        progress, _dialog, _log = _progress(dialog=dialog)
        progress.start()

        progress.update(0, 10, 'A')

        dialog.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
