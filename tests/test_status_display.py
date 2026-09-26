# -*- coding: utf-8 -*-
import unittest

from lib.status_display import status_text


def _t(scanning=False, cleaning=False, active=False):
    return status_text(scanning, cleaning, active, 'scraping', 'scanning', 'cleaning')


class TestStatusText(unittest.TestCase):

    def test_nothing_running_shows_nothing(self):
        self.assertIsNone(_t())

    def test_a_kodi_scan_with_no_scraper_activity_yet_is_still_announced(self):
        self.assertEqual(_t(scanning=True), 'scanning')

    def test_scraper_activity_is_shown_even_while_kodi_scans(self):
        self.assertEqual(_t(scanning=True, active=True), 'scraping')

    def test_a_clean_is_announced(self):
        self.assertEqual(_t(cleaning=True, scanning=True), 'cleaning')


if __name__ == '__main__':
    unittest.main()
