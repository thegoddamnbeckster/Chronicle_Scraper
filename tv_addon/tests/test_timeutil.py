# -*- coding: utf-8 -*-
"""
Time zones between the device and Chronicle (2026-10-11).

Chronicle stores UTC; Kodi's lastplayed is naive LOCAL time. The add-on compared and copied the two as if they were one
clock, leaving the device's UTC offset (six hours in Mountain time) in every "who watched it more recently" decision,
so F9 (finished on one Shield, half-way bookmark left on the other) stayed 'in progress' for weeks. The device now
declares its zone, sends every timestamp with its offset, and converts what Chronicle sends by instant.

The zone is faked by patching time.localtime / time.mktime, so the tests are independent of the machine's own zone
(and of Windows lacking time.tzset). No test hard-codes a zone into the code under test.
"""
import calendar
import os
import sys
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

from lib import progress_sync, timeutil, chronicle_client


def _us_mountain(epoch):
    """-06:00 from 2026-03-08 09:00Z to 2026-11-01 08:00Z, else -07:00 (the 2026 US rules), as a fake local zone."""
    return -6 * 3600 if calendar.timegm((2026, 3, 8, 9, 0, 0)) <= epoch < calendar.timegm((2026, 11, 1, 8, 0, 0)) else -7 * 3600


@contextmanager
def zone(offset_fn):
    """Make the process's 'local' zone offset_fn(epoch) seconds from UTC."""
    def localtime(epoch=None):
        epoch = time.time() if epoch is None else epoch
        return time.gmtime(epoch + offset_fn(epoch))

    def mktime(tt):
        naive = calendar.timegm(tuple(tt[:6]))
        guess = naive - offset_fn(naive)
        return float(naive - offset_fn(guess))

    with patch.object(timeutil.time, 'localtime', localtime), patch.object(timeutil.time, 'mktime', mktime):
        yield


def fixed(hours):
    return lambda epoch: int(hours * 3600)


class TestKodiLocalToIso(unittest.TestCase):

    def test_carries_the_devices_offset_at_that_moment(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03T19:34:43-06:00', timeutil.kodi_local_to_iso('2026-09-03 19:34:43'))   # summer
            self.assertEqual('2026-01-15T18:00:00-07:00', timeutil.kodi_local_to_iso('2026-01-15 18:00:00'))   # winter

    def test_another_zone_gets_its_own_offset(self):
        with zone(fixed(5.5)):
            self.assertEqual('2026-09-04T07:04:43+05:30', timeutil.kodi_local_to_iso('2026-09-04 07:04:43'))
        with zone(fixed(0)):
            self.assertEqual('2026-09-04T01:34:43+00:00', timeutil.kodi_local_to_iso('2026-09-04 01:34:43'))

    def test_empty_and_unparseable(self):
        self.assertIsNone(timeutil.kodi_local_to_iso(''))
        self.assertIsNone(timeutil.kodi_local_to_iso(None))
        self.assertEqual('garbage', timeutil.kodi_local_to_iso('garbage'))

    def test_progress_sync_uses_it(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03T19:34:43-06:00', progress_sync.kodi_lastplayed_to_iso('2026-09-03 19:34:43'))


class TestChronicleIsoToLocal(unittest.TestCase):

    def test_utc_z_becomes_local(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03 19:34:43', timeutil.chronicle_iso_to_kodi('2026-09-04T01:34:43Z'))

    def test_value_in_the_devices_own_zone_is_unchanged(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03 19:34:43', timeutil.chronicle_iso_to_kodi('2026-09-03T19:34:43-06:00'))

    def test_value_in_another_zone_is_converted_by_instant(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03 19:34:43', timeutil.chronicle_iso_to_kodi('2026-09-04T02:34:43+01:00'))

    def test_fractional_seconds_and_compact_offset(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03 19:34:43', timeutil.chronicle_iso_to_kodi('2026-09-04T01:34:43.1234567Z'))
            self.assertEqual('2026-09-03 19:34:43', timeutil.chronicle_iso_to_kodi('2026-09-03T19:34:43-0600'))

    def test_naive_value_from_an_older_server_is_utc(self):
        with zone(_us_mountain):
            self.assertEqual('2026-09-03 19:34:43', timeutil.chronicle_iso_to_kodi('2026-09-04T01:34:43'))

    def test_the_historic_offset_not_the_current_one_applies(self):
        with zone(_us_mountain):
            self.assertEqual('2026-01-15 18:00:00', timeutil.chronicle_iso_to_kodi('2026-01-16T01:00:00Z'))

    def test_unparseable(self):
        self.assertIsNone(timeutil.chronicle_iso_to_local('nonsense'))
        self.assertIsNone(timeutil.chronicle_iso_to_local(''))
        self.assertEqual('', timeutil.chronicle_iso_to_kodi(None))

    def test_round_trip(self):
        with zone(_us_mountain):
            iso = timeutil.kodi_local_to_iso('2026-05-05 12:00:00')
            self.assertEqual('2026-05-05 12:00:00', timeutil.chronicle_iso_to_kodi(iso))


class TestDeviceTimezone(unittest.TestCase):

    @staticmethod
    def _without_tz():
        return {k: v for k, v in os.environ.items() if k != 'TZ'}

    def test_iana_name_from_the_environment(self):
        with patch.dict(os.environ, {'TZ': 'America/Denver'}):
            self.assertEqual('America/Denver', timeutil.device_timezone())

    def test_leading_colon_posix_form(self):
        with patch.dict(os.environ, {'TZ': ':Europe/London'}):
            self.assertEqual('Europe/London', timeutil.device_timezone())

    def test_kodis_own_setting_is_used_when_the_environment_has_none(self):
        with patch.dict(os.environ, self._without_tz(), clear=True), \
                patch.object(timeutil, '_kodi_setting_timezone', return_value='Australia/Sydney'):
            self.assertEqual('Australia/Sydney', timeutil.device_timezone())

    def test_falls_back_to_the_current_offset(self):
        with patch.dict(os.environ, self._without_tz(), clear=True), \
                patch.object(timeutil, '_kodi_setting_timezone', return_value=''), zone(_us_mountain):
            value = timeutil.device_timezone()
        self.assertRegex(value, r'^[+-]\d\d:\d\d$')

    def test_an_abbreviation_is_not_mistaken_for_a_zone_id(self):
        with patch.dict(os.environ, dict(self._without_tz(), TZ='MST7MDT'), clear=True), \
                patch.object(timeutil, '_kodi_setting_timezone', return_value=''):
            self.assertRegex(timeutil.device_timezone(), r'^[+-]\d\d:\d\d$|^Z$')

    def test_the_kodi_setting_is_asked_once_per_process(self):
        calls = []

        def fake(_request):
            calls.append(1)
            return '{"result": {"value": "Europe/Paris"}}'
        with patch.object(timeutil, '_setting_cache', None), patch.object(timeutil.xbmc if hasattr(timeutil, 'xbmc') else sys.modules['xbmc'], 'executeJSONRPC', fake):
            self.assertEqual('Europe/Paris', timeutil._kodi_setting_timezone())
            self.assertEqual('Europe/Paris', timeutil._kodi_setting_timezone())
        self.assertEqual(1, len(calls))

    def test_every_chronicle_request_declares_the_zone(self):
        client = chronicle_client.ChronicleClient.__new__(chronicle_client.ChronicleClient)
        client._api_key = 'k'
        with patch.object(timeutil, 'device_timezone', return_value='America/Denver'):
            req = client._build_request('http://x/api/v1/scrobble', data=b'{}', method='POST')
        self.assertEqual('America/Denver', req.get_header(timeutil.TIMEZONE_HEADER.capitalize()))


class TestComparisonsAreInTheSameClock(unittest.TestCase):
    """F9 with its real values: Chronicle holds the finish as an instant; Kodi's lastplayed is local time."""

    F9_INSTANT = '2026-09-04T01:34:43Z'          # 19:34:43 Mountain
    F9_KODI_LOCAL = '2026-09-03 19:34:43'

    def test_same_moment_is_not_newer(self):
        with zone(_us_mountain):
            self.assertFalse(progress_sync._kodi_lastplayed_is_newer(self.F9_KODI_LOCAL, self.F9_INSTANT))

    def test_same_moment_in_the_devices_own_zone_is_not_newer(self):
        with zone(_us_mountain):
            self.assertFalse(progress_sync._kodi_lastplayed_is_newer(self.F9_KODI_LOCAL, '2026-09-03T19:34:43-06:00'))

    def test_a_later_play_is_newer(self):
        with zone(_us_mountain):
            self.assertTrue(progress_sync._kodi_lastplayed_is_newer('2026-09-03 19:34:44', self.F9_INSTANT))

    def test_watched_direction_pushes_when_chronicle_is_the_same_moment(self):
        kodi = {'playcount': 1, 'lastplayed': self.F9_KODI_LOCAL, 'resume': {'position': 3278.0, 'total': 8995.0}}
        with zone(_us_mountain):
            direction, _ = progress_sync.resolve_watched_direction(True, self.F9_INSTANT, kodi)
        self.assertEqual('push', direction)

    def test_works_in_a_zone_east_of_utc_too(self):
        with zone(fixed(9)):
            self.assertFalse(progress_sync._kodi_lastplayed_is_newer('2026-09-04 10:34:43', self.F9_INSTANT))
            self.assertTrue(progress_sync._kodi_lastplayed_is_newer('2026-09-04 10:34:44', self.F9_INSTANT))

    def test_apply_watched_push_writes_local_time_into_kodi(self):
        vtag = MagicMock()
        with zone(_us_mountain):
            progress_sync.apply_watched_push(vtag, self.F9_INSTANT)
        vtag.setLastPlayed.assert_called_once_with(self.F9_KODI_LOCAL)

    def test_reset_stamp_in_any_format(self):
        kodi = {'playcount': 1, 'lastplayed': '2026-09-03 19:00:00', 'resume': {}}
        with zone(_us_mountain):
            for stamp in ('2026-09-04T01:34:43Z', '2026-09-03T19:34:43-06:00', '2026-09-04T01:34:43'):
                self.assertEqual('reset', progress_sync.resolve_watched_direction(
                    False, None, kodi, chronicle_reset_at=stamp)[0], stamp)


if __name__ == '__main__':
    unittest.main()
