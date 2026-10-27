# -*- coding: utf-8 -*-
"""Tells Chronicle this Kodi install exists, and which add-on is telling it.

Chronicle never calls a Kodi device -- every exchange is an add-on pulling from Chronicle -- so
nothing about the device's address, its web-server port or its remote-control login is read or
sent. What IS sent is who is asking: the device's name, this add-on's name, id and version, and the
Kodi build. Each add-on pairs with its own API key and so gets its own row in Chronicle's device
list (a Kodi with both Scraper add-ons shows two), and these fields make it obvious which row is
which. The row also keys a device's library ids (report_kodi_id) and the refresh signal.

Shared by the movie and TV add-ons (one copy in shared/). Called after a successful "Connect to
Chronicle" pairing and once when each add-on's background service starts. Safe to repeat: it upserts
server-side, keyed by the add-on's own API token.
"""

import xbmc
import xbmcaddon

from lib.chronicle_client import ChronicleClient


def describe():
    """(device name, add-on name, add-on id, add-on version, Kodi build) for this install."""
    addon = xbmcaddon.Addon()
    return (xbmc.getInfoLabel('System.FriendlyName') or 'Kodi',
            addon.getAddonInfo('name'), addon.getAddonInfo('id'), addon.getAddonInfo('version'),
            xbmc.getInfoLabel('System.BuildVersion') or '')


def register():
    """Best-effort and silent: ChronicleClient's own guard covers a not-yet-configured Chronicle."""
    ChronicleClient().register_device(*describe())
