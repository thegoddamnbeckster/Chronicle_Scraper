# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs

from lib import install_marker


class TestInstallMarker(unittest.TestCase):

    def setUp(self):
        kodi_stubs.reset_vfs()

    def test_first_start_of_a_version_is_fresh_then_not(self):
        self.assertTrue(install_marker.is_first_start_of_version('3.17.19'))
        self.assertFalse(install_marker.is_first_start_of_version('3.17.19'))

    def test_an_upgrade_is_fresh_again(self):
        install_marker.is_first_start_of_version('3.17.19')
        self.assertTrue(install_marker.is_first_start_of_version('3.17.20'))


if __name__ == '__main__':
    unittest.main()
