# -*- coding: utf-8 -*-
"""
watch_rating_sync.diff_episode_text -- the periodic sync's correction of a Kodi episode's
descriptive fields, including the season/episode numbers themselves. Episodes were showing the
wrong information because they were matched to Chronicle by Kodi's own (possibly wrong) numbers,
and never had their text compared at all in this pass.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc

from lib.watch_rating_sync import diff_episode_text


def _kodi(**over):
    base = {'season': 1, 'episode': 3, 'title': 'Dead and Confused', 'plot': 'Plot.',
            'firstaired': '2023-03-09', 'art': {'thumb': 'https://t/a.jpg', 'fanart': 'f'}}
    base.update(over)
    return base


class TestDiffEpisodeText(unittest.TestCase):

    def test_matching_episode_needs_nothing(self):
        details = {'title': 'Dead and Confused', 'overview': 'Plot.', 'aired': '2023-03-09T00:00:00',
                   'season': 1, 'episode': 3, 'thumbUrl': 'https://t/a.jpg'}
        self.assertEqual(diff_episode_text(_kodi(), details), {})

    def test_wrong_text_is_corrected_field_by_field(self):
        updates = diff_episode_text(_kodi(title='Wrong', plot='Wrong plot.'),
                                    {'title': 'Dead and Confused', 'overview': 'Plot.'})
        self.assertEqual(updates, {'title': 'Dead and Confused', 'plot': 'Plot.'})

    def test_numbers_are_never_rewritten(self):
        # Kodi's numbering follows the files (a multi-episode file is two Kodi entries), which can
        # legitimately differ from Chronicle's -- only descriptive text is corrected.
        self.assertEqual(diff_episode_text(_kodi(season=1, episode=5), {'season': 1, 'episode': 3}), {})

    def test_fields_chronicle_has_nothing_for_are_never_blanked(self):
        self.assertEqual(diff_episode_text(_kodi(), {'title': None, 'overview': None, 'aired': None}), {})

    def test_thumb_change_keeps_the_other_art_slots(self):
        updates = diff_episode_text(_kodi(), {'thumbUrl': 'https://t/b.jpg'})
        self.assertEqual(updates['art'], {'thumb': 'https://t/b.jpg', 'fanart': 'f'})


if __name__ == '__main__':
    unittest.main()
