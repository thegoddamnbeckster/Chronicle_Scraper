# -*- coding: utf-8 -*-
"""Thin wrapper around xbmc.log for consistent Chronicle Scraper (TV) log lines.

Every message is prefixed with [ChronicleScraperTV][<component>] so it's easy
to grep in the Kodi log -- and, critically, so it's distinguishable from the
sibling Movies addon's own [ChronicleScraper] lines. Both addons used to share
the identical "[ChronicleScraper]" prefix (this file was a straight copy),
which made it impossible to tell from kodi.log alone which addon a given line
actually came from whenever both were installed -- confirmed as a real
diagnostic dead-end while investigating a live Connect-flow bug report
(2026-08-27).
"""

import xbmc

_PREFIX = '[ChronicleScraperTV]'


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
