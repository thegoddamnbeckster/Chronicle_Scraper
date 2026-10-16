# -*- coding: utf-8 -*-
"""Thin wrapper around xbmc.log for consistent Chronicle log lines, shared by every Chronicle Kodi add-on.

Every message is prefixed with the add-on's own tag -- [ChronicleScraperMovie], [ChronicleScraperTV], [ChronicleScrobbler],
[ChronicleRating] -- and the component, [ChronicleScraperMovie][client], so it is easy to grep in the Kodi log and obvious which
add-on a line came from when several are installed. (The two scrapers once shared the identical "[ChronicleScraper]" prefix, which
made kodi.log impossible to read, 2026-08-27.)

Credentials in a URL-style path (smb://user:password@host/...) are masked in every line; see redact.py.
"""

import xbmc
import xbmcaddon

from lib.redact import redact_credentials

_TAGS = {
    'script.chronicle.scraper.movie': '[ChronicleScraperMovie]',
    'script.chronicle.scraper.tv': '[ChronicleScraperTV]',
    'service.chronicle.scrobbler': '[ChronicleScrobbler]',
    'script.chronicle.rating': '[ChronicleRating]',
}
_DEFAULT_TAG = '[Chronicle]'

_prefix = None


def addon_tag():
    """The log tag of the add-on this code is running in (looked up once)."""
    global _prefix
    if _prefix is None:
        try:
            _prefix = _TAGS.get(xbmcaddon.Addon().getAddonInfo('id'), _DEFAULT_TAG)
        except Exception:  # noqa: BLE001 -- logging must never be the thing that fails
            return _DEFAULT_TAG
    return _prefix


class Logger:
    """Component-scoped logger backed by xbmc.log."""

    def __init__(self, component: str = ''):
        self._component = '[{0}]'.format(component) if component else ''

    @property
    def _tag(self):
        return addon_tag() + self._component

    def debug(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, redact_credentials(msg)), xbmc.LOGDEBUG)

    def info(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, redact_credentials(msg)), xbmc.LOGINFO)

    def warning(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, redact_credentials(msg)), xbmc.LOGWARNING)

    def error(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, redact_credentials(msg)), xbmc.LOGERROR)
