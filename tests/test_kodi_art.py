# -*- coding: utf-8 -*-
"""kodi_art: reading an art value the way Kodi reports it."""
import unittest
from urllib.parse import quote

import tests.kodi_stubs  # noqa: F401

from lib.kodi_art import kodi_art_url


def wrapped(url):
    """How Kodi's JSON interface reports an art URL it stores."""
    return 'image://{0}/'.format(quote(url, safe=''))


class TestKodiArtUrl(unittest.TestCase):
    def test_a_wrapped_address_comes_back_plain(self):
        url = 'https://image.tmdb.org/t/p/w500/abc.jpg'
        self.assertEqual(kodi_art_url(wrapped(url)), url)

    def test_an_address_with_its_own_escapes_and_query_survives(self):
        url = 'https://x/a%20b.jpg?x=1&y=2'
        self.assertEqual(kodi_art_url(wrapped(url)), url)

    def test_a_plain_address_or_nothing_is_left_alone(self):
        self.assertEqual(kodi_art_url('https://x/y.jpg'), 'https://x/y.jpg')
        self.assertEqual(kodi_art_url(None), '')
        self.assertEqual(kodi_art_url(''), '')


if __name__ == '__main__':
    unittest.main()
