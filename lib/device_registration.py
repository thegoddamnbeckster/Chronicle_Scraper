# -*- coding: utf-8 -*-
"""Tells Chronicle this Kodi instance exists, by name.

Chronicle never calls a Kodi device -- every exchange is this add-on pulling from Chronicle -- so
nothing about the device's address, its web-server port or its remote-control login is read or sent
any more. (They used to be: an earlier design had Chronicle reach out to registered devices, and the
row kept them long after nothing did.) The row is still needed: report_kodi_id() and the refresh
signal key a device's library ids on it.

Called from default.py right after a successful "Connect to Chronicle" pairing, and once when the
background service starts. Safe to call repeatedly: it upserts server-side, keyed by this device's
own API token.
"""

import xbmc

from lib.chronicle_client import ChronicleClient


def register():
    """Best-effort and silent: ChronicleClient's own guard covers a not-yet-configured Chronicle."""
    ChronicleClient().register_device(xbmc.getInfoLabel('System.FriendlyName') or 'Kodi')
