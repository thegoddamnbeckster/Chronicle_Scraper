# -*- coding: utf-8 -*-
"""recent_play: the record of what this Kodi played, and the rule that keeps a bookmark made by a partial play."""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import recent_play

FILE = 'smb://nas/Movies/Heat (1995)/Heat (1995).mkv'
PLAYED = datetime(2026, 10, 25, 16, 30, 0)
PLAYED_KEY = '2026-10-25 16:30:00'


def _item(position=3000, total=8400, file=FILE):
    return {'file': file, 'resume': {'position': position, 'total': total}}


def _utc(local):
    """Chronicle-style ISO for a naive local datetime on this machine."""
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.mktime(local.timetuple())))


class TestStore(unittest.TestCase):
    def setUp(self):
        tests.kodi_stubs.reset_vfs()
        self.addCleanup(tests.kodi_stubs.reset_vfs)

    def test_a_recorded_play_is_read_back_under_any_spelling_of_the_path(self):
        recent_play.record(FILE, now=PLAYED)
        self.assertEqual(recent_play.last_played(FILE), PLAYED)
        self.assertEqual(recent_play.last_played('SMB://nas/Movies/Heat%20(1995)/Heat%20(1995).mkv'), PLAYED)
        self.assertIsNone(recent_play.last_played('smb://nas/Movies/Other.mkv'))

    def test_a_second_play_replaces_the_first(self):
        recent_play.record(FILE, now=PLAYED)
        recent_play.record(FILE, now=PLAYED + timedelta(hours=1))
        self.assertEqual(recent_play.last_played(FILE), PLAYED + timedelta(hours=1))

    def test_plays_older_than_the_keep_window_are_dropped(self):
        recent_play.record('smb://nas/Old.mkv', now=PLAYED - timedelta(days=400))
        recent_play.record(FILE, now=PLAYED)
        self.assertIsNone(recent_play.last_played('smb://nas/Old.mkv'))
        self.assertEqual(recent_play.last_played(FILE), PLAYED)

    def test_a_missing_or_unreadable_store_means_nothing_was_played(self):
        self.assertEqual(recent_play.load(), {})
        recent_play.record(FILE, now=PLAYED)
        with mock.patch.object(recent_play.json, 'loads', side_effect=ValueError('corrupt')):
            self.assertEqual(recent_play.load(), {})

    def test_an_empty_path_is_ignored(self):
        recent_play.record('', now=PLAYED)
        self.assertEqual(recent_play.load(), {})


class TestFreshBookmark(unittest.TestCase):
    PLAYS = {recent_play.norm(FILE): PLAYED_KEY}

    def _fresh(self, item=None, stamps=(), plays=None):
        return recent_play.fresh_bookmark(item or _item(), list(stamps), self.PLAYS if plays is None else plays)

    def test_a_bookmark_from_a_play_after_chronicles_finish_is_fresh_and_reports_its_real_percent(self):
        percent, at = self._fresh(stamps=[_utc(PLAYED - timedelta(days=30))])
        self.assertAlmostEqual(percent, 3000 * 100.0 / 8400)
        self.assertTrue(at.startswith('2026-10-25T16:30:00'))

    def test_no_chronicle_time_at_all_still_counts_a_recorded_play_as_fresh(self):
        self.assertIsNotNone(self._fresh(stamps=[None, None]))

    def test_a_play_Chronicle_has_since_finished_elsewhere_is_a_leftover(self):
        self.assertIsNone(self._fresh(stamps=[_utc(PLAYED + timedelta(hours=2))]))

    def test_chronicles_own_resume_time_for_the_same_play_seconds_later_does_not_hide_it(self):
        self.assertIsNotNone(self._fresh(stamps=[_utc(PLAYED + timedelta(seconds=40))]))

    def test_no_recorded_play_means_a_leftover(self):
        self.assertIsNone(self._fresh(plays={}))

    def test_no_bookmark_means_nothing_to_keep(self):
        self.assertIsNone(self._fresh(item=_item(position=0)))
        self.assertIsNone(self._fresh(item=_item(total=0)))
        self.assertIsNone(self._fresh(item={'file': FILE}))
        self.assertIsNone(recent_play.fresh_bookmark(None, [], self.PLAYS))

    def test_an_item_with_no_file_is_never_fresh(self):
        self.assertIsNone(self._fresh(item=_item(file=None)))

    def test_stamps_ignore_a_finish_time_after_a_reset(self):
        self.assertEqual(recent_play.chronicle_stamps({'isWatched': False, 'lastWatchedAt': 'X', 'resumeUpdatedAt': 'Y'}), [None, 'Y'])
        self.assertEqual(recent_play.chronicle_stamps({'isWatched': True, 'lastWatchedAt': 'X'}), ['X', None])


class TestIsPlaying(unittest.TestCase):
    def tearDown(self):
        xbmc.Player.playing_file = None

    def test_only_the_file_actually_playing_counts(self):
        xbmc.Player.playing_file = FILE
        self.assertTrue(recent_play.is_playing(FILE))
        self.assertTrue(recent_play.is_playing('SMB://nas/Movies/Heat%20(1995)/Heat%20(1995).mkv'))
        self.assertFalse(recent_play.is_playing('smb://nas/Movies/Other.mkv'))
        self.assertFalse(recent_play.is_playing(None))

    def test_nothing_playing_or_a_failing_player_is_not_playing(self):
        self.assertFalse(recent_play.is_playing(FILE))
        with mock.patch.object(xbmc, 'Player', side_effect=RuntimeError('boom')):
            self.assertFalse(recent_play.is_playing(FILE))


class TestProtectsProgress(unittest.TestCase):
    DETAILS = {'title': 'Heat'}

    def setUp(self):
        tests.kodi_stubs.reset_vfs()
        self.addCleanup(tests.kodi_stubs.reset_vfs)
        self.addCleanup(setattr, xbmc.Player, 'playing_file', None)
        self.push = mock.Mock()

    def test_a_playing_file_is_protected_and_nothing_is_reported(self):
        xbmc.Player.playing_file = FILE
        self.assertTrue(recent_play.protects_progress(_item(), self.DETAILS, self.push))
        self.push.assert_not_called()

    def test_a_fresh_bookmark_is_protected_and_its_real_percent_is_reported(self):
        recent_play.record(FILE, now=datetime.now())
        self.assertTrue(recent_play.protects_progress(_item(), self.DETAILS, self.push))
        self.assertAlmostEqual(self.push.call_args[0][0], 3000 * 100.0 / 8400)

    def test_an_ordinary_item_is_not_protected(self):
        self.assertFalse(recent_play.protects_progress(_item(), self.DETAILS, self.push))
        self.assertFalse(recent_play.protects_progress(None, self.DETAILS, self.push))
        self.push.assert_not_called()


class TestRecorder(unittest.TestCase):
    def setUp(self):
        tests.kodi_stubs.reset_vfs()
        self.addCleanup(setattr, xbmc.Player, 'playing_file', None)
        self.addCleanup(tests.kodi_stubs.reset_vfs)
        self.recorder = recent_play.Recorder()

    def test_start_records_the_playing_file_and_stop_records_it_again(self):
        xbmc.Player.playing_file = FILE
        self.recorder.onAVStarted()
        started = recent_play.last_played(FILE)
        self.assertIsNotNone(started)
        xbmc.Player.playing_file = None          # after a stop Kodi no longer names the file
        with mock.patch.object(recent_play, 'datetime') as clock:
            clock.now.return_value = started + timedelta(minutes=45)
            self.recorder.onPlayBackStopped()
        self.assertEqual(recent_play.last_played(FILE), started + timedelta(minutes=45))

    def test_ended_and_error_also_record_and_forget_the_file(self):
        for callback in ('onPlayBackEnded', 'onPlayBackError'):
            xbmc.Player.playing_file = FILE
            self.recorder.onAVStarted()
            tests.kodi_stubs.reset_vfs()
            getattr(self.recorder, callback)()
            self.assertIsNotNone(recent_play.last_played(FILE), callback)

    def test_a_film_already_playing_when_the_service_starts_is_picked_up(self):
        xbmc.Player.playing_file = FILE
        recent_play.Recorder()
        self.assertIsNotNone(recent_play.last_played(FILE))

    def test_a_stop_with_nothing_started_records_nothing(self):
        self.recorder.onPlayBackStopped()
        self.assertEqual(recent_play.load(), {})
