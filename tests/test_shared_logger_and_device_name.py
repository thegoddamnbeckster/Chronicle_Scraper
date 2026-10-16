# -*- coding: utf-8 -*-
"""shared logger (per-add-on tag, credential masking) and device_name (the four-tier device identity)."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import device_name, logger


class TestLogger(unittest.TestCase):
    def setUp(self):
        logger._prefix = None
        self.addCleanup(setattr, logger, '_prefix', None)
        xbmc.log.reset_mock()

    def _addon(self, addon_id):
        addon = mock.MagicMock()
        addon.getAddonInfo.return_value = addon_id
        return mock.patch.object(logger.xbmcaddon, 'Addon', return_value=addon)

    def test_tag_per_addon(self):
        for addon_id, tag in (('script.chronicle.scraper.movie', '[ChronicleScraperMovie]'),
                              ('script.chronicle.scraper.tv', '[ChronicleScraperTV]'),
                              ('service.chronicle.scrobbler', '[ChronicleScrobbler]'),
                              ('script.chronicle.rating', '[ChronicleRating]'),
                              ('other.addon', '[Chronicle]')):
            logger._prefix = None
            with self._addon(addon_id):
                logger.Logger('client').info('hello')
            self.assertEqual(xbmc.log.call_args[0][0], '{0}[client] hello'.format(tag), addon_id)

    def test_levels(self):
        with self._addon('service.chronicle.scrobbler'):
            log = logger.Logger('c')
            log.debug('d')
            log.info('i')
            log.warning('w')
            log.error('e')
        self.assertEqual([c[0][1] for c in xbmc.log.call_args_list],
                         [xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR])

    def test_credentials_in_paths_are_masked(self):
        with self._addon('service.chronicle.scrobbler'):
            logger.Logger().info('scanning smb://user:secret@nas/videos')
        message = xbmc.log.call_args[0][0]
        self.assertNotIn('secret', message)

    def test_component_is_optional(self):
        with self._addon('script.chronicle.rating'):
            logger.Logger().info('x')
        self.assertEqual(xbmc.log.call_args[0][0], '[ChronicleRating] x')

    def test_failing_addon_lookup_never_breaks_logging(self):
        with mock.patch.object(logger.xbmcaddon, 'Addon', side_effect=RuntimeError('no addon context')):
            logger.Logger('c').info('still logs')
        self.assertEqual(xbmc.log.call_args[0][0], '[Chronicle][c] still logs')


class TestDeviceName(unittest.TestCase):
    def _labels(self, **labels):
        return mock.patch.object(device_name.xbmc, 'getInfoLabel', side_effect=lambda k: labels.get(k, ''))

    def _sockets(self, fqdn='', host=''):
        return (mock.patch.object(device_name.socket, 'getfqdn', return_value=fqdn),
                mock.patch.object(device_name.socket, 'gethostname', return_value=host))

    def _name(self, fqdn='', host='', **labels):
        a, b = self._sockets(fqdn, host)
        with a, b, self._labels(**labels):
            return device_name.get_device_name()

    def test_dns_name_wins(self):
        self.assertEqual(self._name(fqdn='vision.lan', host='vision', **{'System.FriendlyName': 'Den'}), 'vision.lan')

    def test_hostname_when_fqdn_is_loopback(self):
        self.assertEqual(self._name(fqdn='localhost', host='vision'), 'vision')

    def test_custom_friendly_name_when_the_os_gives_nothing(self):
        self.assertEqual(self._name(fqdn='localhost', host='localhost', **{'System.FriendlyName': 'Downstairs Shield'}), 'Downstairs Shield')

    def test_default_friendly_name_is_ignored_for_the_ip(self):
        self.assertEqual(self._name(**{'System.FriendlyName': 'Kodi', 'Network.IPAddress': '10.0.0.203'}), '10.0.0.203')

    def test_placeholder_ips_are_not_used(self):
        for ip in ('Busy', '0.0.0.0', 'localhost', ''):
            self.assertEqual(self._name(**{'Network.IPAddress': ip}), 'Kodi', ip)

    def test_lookup_exceptions_fall_through_to_kodi(self):
        with mock.patch.object(device_name.socket, 'getfqdn', side_effect=OSError), \
                mock.patch.object(device_name.socket, 'gethostname', side_effect=OSError), \
                mock.patch.object(device_name.xbmc, 'getInfoLabel', side_effect=RuntimeError):
            self.assertEqual(device_name.get_device_name(), 'Kodi')


if __name__ == '__main__':
    unittest.main()
