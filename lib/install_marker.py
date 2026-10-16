# -*- coding: utf-8 -*-
"""Detects "this is the first service start since the add-on was installed or upgraded".

Per-user direction (2026-09-26): right after an install/upgrade is not an appropriate time to start a
library scan on the user's behalf -- the user is usually mid-task on the device, and every install
restarts the service, so each one re-triggered a long scan. The startup scan checks are skipped for
that one start; they run again on the next normal Kodi start (Chronicle's new-content flag stays
unacknowledged, so nothing is lost).
"""

import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('install_marker')


def _marker_path():
    """Under the add-on's own data folder, so the movie and TV scrapers each track their own installs."""
    return 'special://profile/addon_data/{0}/installed_version.txt'.format(xbmcaddon.Addon().getAddonInfo('id'))


def is_first_start_of_version(version, path=None):
    """True the first time this exact version's service starts (records it); False afterwards.
    Any failure to read/write the marker counts as "not fresh" -- never suppress on a guess."""
    try:
        path = path or _marker_path()
        previous = None
        if xbmcvfs.exists(path):
            handle = xbmcvfs.File(path, 'r')
            try:
                previous = bytes(handle.readBytes()).decode('utf-8').strip()
            finally:
                handle.close()
        if previous == version:
            return False
        xbmcvfs.mkdirs(path.rsplit('/', 1)[0] + '/')
        out = xbmcvfs.File(path, 'w')
        try:
            out.write(version.encode('utf-8'))
        finally:
            out.close()
        # No marker at all is a brand-new install; a different one is an upgrade. Both are fresh.
        return True
    except Exception as exc:
        log.warning('Could not read/write the install marker: {0}'.format(exc))
        return False
