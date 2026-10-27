# -*- coding: utf-8 -*-
"""sync_baton: the add-ons' background syncs take turns instead of running at once."""
import unittest

import tests.kodi_stubs  # noqa: F401

from lib import sync_baton


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)


class _Store:
    def __init__(self):
        self.value = ''

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class TestSyncBaton(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.now = [1000.0]
        self.log = _Log()
        sync_baton._held_here.clear()

    def _hold(self, owner, label='x', wait=10, **kw):
        def sleep(seconds):
            self.now[0] += seconds
        return sync_baton.hold(owner, label, self.log, wait_seconds=wait, store=self.store, clock=lambda: self.now[0], sleep=sleep, **kw)

    def test_a_free_baton_is_taken_and_given_back(self):
        with self._hold('a') as got:
            self.assertTrue(got)
            self.assertTrue(self.store.value.startswith('a|'))
        self.assertEqual(self.store.value, '')

    def test_another_owner_waits_then_gives_up(self):
        with self._hold('a', 'the watch/rating sync'):
            with self._hold('b', 'the episode check', wait=5) as got:
                self.assertFalse(got)
            self.assertTrue(self.store.value.startswith('a|'))          # b's give-up must not free a's baton
        self.assertTrue(any('waits for the watch/rating sync' in l for l in self.log.lines))

    def test_the_same_owner_can_take_it_again_inside(self):
        with self._hold('a') as outer:
            with self._hold('a') as inner:
                self.assertTrue(outer and inner)
            self.assertTrue(self.store.value.startswith('a|'))          # still held by the outer block
        self.assertEqual(self.store.value, '')

    def test_a_holder_that_never_released_stops_counting_after_the_ttl(self):
        self.store.value = 'ghost|{0}|something'.format(self.now[0] + sync_baton.BATON_TTL_SECONDS)
        with self._hold('a', wait=1) as got:
            self.assertFalse(got)
        self.now[0] += sync_baton.BATON_TTL_SECONDS + 1
        with self._hold('a') as got:
            self.assertTrue(got)

    def test_shutdown_stops_the_wait(self):
        self.store.value = 'other|{0}|something'.format(self.now[0] + 1000)
        with self._hold('a', wait=3600, is_aborted=lambda: True) as got:
            self.assertFalse(got)


if __name__ == '__main__':
    unittest.main()
