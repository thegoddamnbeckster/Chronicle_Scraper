# -*- coding: utf-8 -*-
"""Regression tests for lib/full_sync_check.py's diff_episode() -- the TV counterpart to the
movie addon's own diff_movie() tests. See that module's own doc for the full root-cause
writeup this mirrors. Per-user direction: only ever update a field that actually differs --
never a blind overwrite -- so these tests focus on exactly that boundary.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc before import below

from lib.full_sync_check import diff_episode


def _kodi_item(**overrides):
    base = {
        'title': 'Old Title', 'plot': 'Old plot.', 'firstaired': '2020-01-01',
        'art': {'thumb': 'https://old.example/thumb.jpg'},
    }
    base.update(overrides)
    return base


def _chronicle_details(**overrides):
    base = {
        'title': 'New Title', 'overview': 'New plot.', 'aired': '2020-02-02T00:00:00Z',
        'thumbUrl': 'https://new.example/thumb.jpg',
    }
    base.update(overrides)
    return base


class TestDiffEpisode(unittest.TestCase):

    def test_everything_matching_produces_no_updates(self):
        details = _chronicle_details(title='Old Title', overview='Old plot.',
                                       aired='2020-01-01T00:00:00Z',
                                       thumbUrl='https://old.example/thumb.jpg')
        kodi = _kodi_item()

        self.assertEqual(diff_episode(kodi, details), {})

    def test_title_and_plot_mismatch_detected(self):
        kodi = _kodi_item(title='Old Title', plot='Old plot.')
        details = _chronicle_details(title='New Title', overview='New plot.')

        updates = diff_episode(kodi, details)

        self.assertEqual(updates['title'], 'New Title')
        self.assertEqual(updates['plot'], 'New plot.')

    def test_matching_title_not_included_in_updates(self):
        kodi = _kodi_item(title='Same Title')
        details = _chronicle_details(title='Same Title')

        updates = diff_episode(kodi, details)

        self.assertNotIn('title', updates)

    def test_chronicle_missing_field_never_blanks_kodi_value(self):
        kodi = _kodi_item()
        details = _chronicle_details(overview=None)

        updates = diff_episode(kodi, details)

        self.assertNotIn('plot', updates)

    def test_aired_date_mismatch_uses_date_only(self):
        kodi = _kodi_item(firstaired='2020-01-01')
        details = _chronicle_details(aired='2020-02-02T15:30:00Z')

        updates = diff_episode(kodi, details)

        self.assertEqual(updates['firstaired'], '2020-02-02')

    def test_matching_aired_date_ignores_time_component(self):
        kodi = _kodi_item(firstaired='2020-01-01')
        details = _chronicle_details(aired='2020-01-01T15:30:00Z')

        updates = diff_episode(kodi, details)

        self.assertNotIn('firstaired', updates)

    def test_thumb_mismatch_preserves_other_art_slots(self):
        kodi = _kodi_item(art={'thumb': 'https://old.example/thumb.jpg', 'fanart': 'https://keep.example/fanart.jpg'})
        details = _chronicle_details()

        updates = diff_episode(kodi, details)

        self.assertEqual(updates['art']['thumb'], 'https://new.example/thumb.jpg')
        self.assertEqual(updates['art']['fanart'], 'https://keep.example/fanart.jpg')


if __name__ == '__main__':
    unittest.main()
