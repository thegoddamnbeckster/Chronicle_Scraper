# -*- coding: utf-8 -*-
"""Thin wrapper around xbmc.log for consistent Chronicle Scraper log lines.

Every message is prefixed with [ChronicleScraper][<component>] so it's easy
to grep in the Kodi log.
"""

import xbmc

_PREFIX = '[ChronicleScraper]'


class Logger:
    """Component-scoped logger backed by xbmc.log."""

    def __init__(self, component: str = ''):
        tag = '[{0}]'.format(component) if component else ''
        self._tag = _PREFIX + tag

    def debug(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, msg), xbmc.LOGDEBUG)

    def info(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, msg), xbmc.LOGINFO)

    def warning(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, msg), xbmc.LOGWARNING)

    def error(self, msg: str) -> None:
        xbmc.log('{0} {1}'.format(self._tag, msg), xbmc.LOGERROR)
