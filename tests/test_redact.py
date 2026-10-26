# -*- coding: utf-8 -*-
"""
A Kodi source path can carry a login (smb://user:password@host/share). The add-on put such paths in notifications and log lines,
so the username and password were shown on screen whenever the movie collections folder could not be reached (2026-10-13).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.kodi_stubs as kodi_stubs  # noqa: F401 -- side-effect: stubs xbmc & friends

from lib import logger
from lib.redact import redact_credentials


class TestRedaction(unittest.TestCase):

    def test_smb_login_is_masked_and_the_rest_is_kept(self):
        self.assertEqual('smb://***@10.0.0.162/Video7/Movie Collections/',
                         redact_credentials('smb://kodi:s3cret@10.0.0.162/Video7/Movie Collections/'))

    def test_a_user_with_no_password(self):
        self.assertEqual('smb://***@nas/share', redact_credentials('smb://guest@nas/share'))

    def test_a_password_containing_an_at_sign_or_colon(self):
        self.assertEqual('smb://***@nas/share', redact_credentials('smb://me:p@ss:word@nas/share'))

    def test_other_schemes(self):
        for url in ('nfs://u:p@host/export', 'ftp://u:p@host/dir', 'sftp://u:p@host:22/dir', 'webdav://u:p@host/dav', 'http://u:p@host:8080/x'):
            masked = redact_credentials(url)
            self.assertNotIn(':p@', masked, url)
            self.assertIn('***@', masked)
            self.assertIn('host', masked)

    def test_a_login_inside_a_percent_encoded_multipath_is_masked(self):
        text = 'multipath://smb%3a%2f%2fkodi%3as3cret%40nas%2fVideos%2f/smb%3a%2f%2fother%3apw%40nas2%2fMovies%2f/'
        masked = redact_credentials(text)

        self.assertNotIn('s3cret', masked)
        self.assertNotIn('pw%40', masked)
        self.assertNotIn('kodi%3a', masked)
        self.assertIn('nas%2fVideos%2f', masked)
        self.assertIn('nas2%2fMovies%2f', masked)

    def test_a_path_in_a_sentence_keeps_the_sentence(self):
        self.assertEqual('Movie collections folder not reachable: smb://***@nas/Collections/',
                         redact_credentials('Movie collections folder not reachable: smb://u:p@nas/Collections/'))

    def test_several_logins_in_one_text(self):
        masked = redact_credentials('tried smb://a:1@h1/x then smb://b:2@h2/y')
        self.assertEqual('tried smb://***@h1/x then smb://***@h2/y', masked)

    def test_text_without_a_login_is_unchanged(self):
        for text in ('smb://10.0.0.162/Video7/Movies/', '/storage/emulated/0/Movies', r'C:\Movies', 'contact me@example.com about it',
                     'special://profile/addon_data/x/', 'multipath://smb%3a%2f%2fnas%2fVideos%2f/', '', 'plain text'):
            self.assertEqual(text, redact_credentials(text), text)

    def test_an_email_address_is_not_a_login(self):
        self.assertEqual('mail to someone@example.com', redact_credentials('mail to someone@example.com'))

    def test_non_strings_come_back_untouched(self):
        self.assertIsNone(redact_credentials(None))
        self.assertEqual(5, redact_credentials(5))


class TestApiKeyIsHidden(unittest.TestCase):
    def test_json_and_its_repr_never_show_the_key(self):
        for text in ('{"api_key":"chr_live_S3CRET","a":1}', '{"apiKey": "chr_live_S3CRET"}', "{'api_key': 'chr_live_S3CRET'}",
                     'x {\\"api_key\\":\\"chr_live_S3CRET\\",\\"u\\":1}'):
            out = redact_credentials(text)
            self.assertNotIn('S3CRET', out, text)
            self.assertIn('***', out, text)

    def test_the_rest_of_the_line_is_kept(self):
        self.assertEqual('{"api_key":"***","url":"http://nas:7979"}',
                         redact_credentials('{"api_key":"chr_live_S3CRET","url":"http://nas:7979"}'))

    def test_the_word_api_alone_changes_nothing(self):
        self.assertEqual('the api is up', redact_credentials('the api is up'))

    def test_the_logger_hides_it_too(self):
        with patch.object(logger.xbmc, 'log') as xlog:
            logger.Logger('t').info('settings {"api_key":"chr_live_S3CRET"}')
        self.assertNotIn('S3CRET', xlog.call_args[0][0])


class TestLoggerNeverWritesALogin(unittest.TestCase):

    def test_every_level_is_redacted(self):
        log = logger.Logger('t')
        for level in ('debug', 'info', 'warning', 'error'):
            with patch.object(logger.xbmc, 'log') as xlog:
                getattr(log, level)('Movie set folder not writable: smb://kodi:s3cret@nas/Collections/')
            written = xlog.call_args[0][0]
            self.assertNotIn('s3cret', written, level)
            self.assertNotIn('kodi:', written, level)
            self.assertIn('smb://***@nas/Collections/', written, level)


if __name__ == '__main__':
    unittest.main()
