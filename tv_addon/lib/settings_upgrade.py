# -*- coding: utf-8 -*-
"""One-time settings migration -- runs the first time ANY part of this addon executes
after upgrading to a version that changed a setting's shipped default, no Kodi restart
or addon disable/enable needed.

Why this has to exist at all: a Kodi addon's settings.xml `default="..."` attribute only
ever applies to a setting Kodi has NEVER persisted a value for (a fresh install) -- the very
first time ANYTHING reads a setting (even before a user ever opens this addon's own Settings
screen), Kodi writes that value into the addon's own settings.xml on disk. Once that's
happened, bumping the shipped default in a later addon update changes nothing for an
already-installed copy -- and critically, this has NOTHING to do with Kodi caching old
Python code: reinstalling the addon, disabling/re-enabling it, or restarting Kodi entirely
all reload the code fine, but none of them touch an already-persisted settings value. Only
this addon's own code, explicitly calling setSettingBool(), can flip it.

v1.10.0 changed write_nfo's shipped default from False to True (see addon.xml's own
changelog: this addon's own copy of the setting must ALSO be on for a rebuild pass -- driven
from the sibling movie addon -- to actually write anything for TV shows/episodes; leaving it
off by default meant episode/rating/progress changes could silently never reach Kodi even
after the movie addon's own settings were fixed). Without this migration, every
already-installed copy of this addon keeps running with the old (False) value forever.

Marked via a hidden settings key (visible="false" in settings.xml, never shown in the UI --
same technique already used for connected_display_name) rather than comparing addon
versions, since this only ever needs to run once, ever.
"""

import xbmcaddon
import xbmcgui

from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('settings_upgrade')

_MIGRATION_KEY = 'migrated_write_nfo_default_v1_10_0'


def ensure_defaults_migrated():
    """Idempotent -- safe to call from every entry point (default.py, python/tvshow_scraper.py)
    every time each one starts, so the migration is guaranteed to run on whichever of them Kodi
    happens to invoke first after the update, without depending on the user opening this
    addon's menu or Kodi being restarted."""
    if ADDON.getSettingBool(_MIGRATION_KEY):
        return

    changed = not ADDON.getSettingBool('write_nfo')
    if changed:
        ADDON.setSettingBool('write_nfo', True)
    ADDON.setSettingBool(_MIGRATION_KEY, True)

    if changed:
        log.info('settings_upgrade: one-time migration turned ON write_nfo (v1.10.0 changed '
                 'its shipped default -- see addon.xml changelog)')
        xbmcgui.Dialog().notification(
            ADDON.getLocalizedString(32000),
            ADDON.getLocalizedString(32124),
            icon=xbmcgui.NOTIFICATION_INFO,
            time=15000,
        )
    else:
        log.info('settings_upgrade: nothing to migrate -- write_nfo already on')
