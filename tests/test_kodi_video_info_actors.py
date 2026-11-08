"""The cast Chronicle sends reaches Kodi with each actor's photo (Musk (2026), 2026-11-07: three actors showed silhouettes)."""
import unittest

import tests.kodi_stubs  # noqa: F401  (installs the xbmc stubs before the module under test is imported)
from lib.kodi_video_info import build_actors


class TestBuildActors(unittest.TestCase):
    def test_each_actor_carries_name_role_order_and_photo(self):
        actors = build_actors([{'name': 'Al', 'role': 'Vincent', 'thumbUrl': 'https://img/al.jpg'},
                               {'name': 'Bo', 'role': 'Neil', 'thumbUrl': 'https://img/bo.jpg'}])
        self.assertEqual([(a.name, a.role, a.order, a.thumbnail) for a in actors],
                         [('Al', 'Vincent', 0, 'https://img/al.jpg'), ('Bo', 'Neil', 1, 'https://img/bo.jpg')])

    def test_an_actor_without_a_photo_or_role_stays_blank_not_none(self):
        (actor,) = build_actors([{'name': 'Cy', 'role': None, 'thumbUrl': None}])
        self.assertEqual((actor.role, actor.thumbnail), ('', ''))

    def test_no_cast_is_an_empty_list(self):
        self.assertEqual(build_actors(None), [])
        self.assertEqual(build_actors([]), [])


if __name__ == '__main__':
    unittest.main()
