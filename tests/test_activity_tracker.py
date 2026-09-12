# -*- coding: utf-8 -*-
"""
Regression tests for lib/activity_tracker.py's is_recently_active() -- the shared "is either
addon's scraper doing something right now" signal that collection_art_sync.py and
watch_rating_sync.py's own periodic triggers defer to (see is_recently_active's own doc for the
full reasoning: this mirrors IKodiDeviceService.IsScanActiveAsync's server-side pause, but for
avoiding contention between the two addons' own local background tasks and an active scrape).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import activity_tracker


class TestIsRecentlyActive(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_nothing_ever_recorded_returns_false(self):
        self.assertFalse(activity_tracker.is_recently_active(idle_timeout_seconds=30))

    def test_activity_within_the_window_returns_true(self):
        activity_tracker.mark_active('Some Movie (2020)')

        self.assertTrue(activity_tracker.is_recently_active(idle_timeout_seconds=30))

    def test_activity_past_the_window_returns_false(self):
        activity_tracker.mark_active('Some Movie (2020)')
        data = activity_tracker.read_activity()

        # now is injectable specifically so this doesn't need a real 30-second sleep to prove
        # the window actually expires -- see is_recently_active's own doc.
        far_future = data['timestamp'] + 31
        self.assertFalse(activity_tracker.is_recently_active(idle_timeout_seconds=30, now=far_future))

    def test_activity_exactly_at_the_boundary_returns_false(self):
        activity_tracker.mark_active('Some Movie (2020)')
        data = activity_tracker.read_activity()

        at_boundary = data['timestamp'] + 30
        self.assertFalse(activity_tracker.is_recently_active(idle_timeout_seconds=30, now=at_boundary))

    def test_reflects_activity_recorded_by_the_other_addon(self):
        # mark_active() has no notion of which addon called it -- both write to the same shared
        # special://temp/ path (see the module's own doc for why). This test just documents that
        # a caller cannot and need not distinguish; any recent call from either addon counts.
        activity_tracker.mark_active('A TV Episode (from the TV addon)')

        self.assertTrue(activity_tracker.is_recently_active(idle_timeout_seconds=30))


if __name__ == '__main__':
    unittest.main()
