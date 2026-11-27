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


class TestOneWriterForEpisodeText(unittest.TestCase):
    """With the TV add-on's episode check on, only it writes an episode's text: two passes matching episodes differently
    undid each other on every run (downstairs, 2026-11-26)."""

    def _sync(self, check_enabled, tv_installed=True):
        import xbmcaddon
        from unittest import mock
        from lib import watch_rating_sync as w

        def addon(addon_id=None):
            if addon_id == w._TV_ADDON_ID and not tv_installed:
                raise RuntimeError('Unknown addon id')
            a = mock.MagicMock()
            a.getSettingBool.return_value = check_enabled
            return a

        client = mock.MagicMock()
        client.get_episode_details.return_value = {'title': 'The Blinds (3)', 'overview': 'Plot.'}
        with mock.patch.object(xbmcaddon, 'Addon', side_effect=addon), \
                mock.patch.object(w, '_build_state_updates', return_value={}), \
                mock.patch.object(w, '_set_episode_details') as set_details:
            w._sync_one_episode(client, {'title': 'The Voice'},
                                {'episodeid': 7, 'season': 29, 'episode': 3, 'title': 'Worst Cooks', 'plot': 'Plot.'}, 99)
        return set_details

    def test_the_tv_check_alone_writes_episode_text_when_it_is_on(self):
        self._sync(check_enabled=True).assert_not_called()

    def test_this_pass_writes_it_when_the_check_is_off(self):
        self._sync(check_enabled=False).assert_called_once_with(7, {'title': 'The Blinds (3)'})

    def test_this_pass_writes_it_when_there_is_no_tv_add_on(self):
        self._sync(check_enabled=True, tv_installed=False).assert_called_once_with(7, {'title': 'The Blinds (3)'})


if __name__ == '__main__':
    unittest.main()
