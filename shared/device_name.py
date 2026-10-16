# -*- coding: utf-8 -*-
"""Resolves a human-meaningful name for this Kodi instance.

Shared by device_auth.py (device-auth pairing request) and media_info.py
(every scrobble payload) so a scrobble's deviceName column in Chronicle's
history/reports actually identifies which box played it (e.g. "Vision" or
a DNS name) instead of the literal, useless "Kodi" every installation used
to send -- indistinguishable from any other Kodi instance scrobbling to the
same Chronicle account.
"""

import ipaddress
import socket

import xbmc

from lib.logger import Logger

log = Logger('device_name')

_LOOPBACK_NAMES = ('localhost', 'localhost.localdomain')


def get_device_name() -> str:
    """Prefer the machine's actual DNS name over Kodi's own FriendlyName setting --
    FriendlyName is just an arbitrary label the user can set to anything, while a
    real DNS name is a verifiable identifier for the physical device.

    socket.getfqdn() tries a genuine reverse-DNS/hosts lookup and upgrades to a
    dotted name when that succeeds, but on a typical home LAN there's usually no
    such record -- it then falls back to the plain OS hostname, which is still a
    real, meaningful identifier (often mDNS-resolvable as "<hostname>.local") and
    clearly better than an arbitrary Kodi settings label. Only the degenerate
    "localhost" non-answer is treated as "no usable name".

    Confirmed live (2026-08-28, Shield Android TV box): BOTH socket calls come
    back unusable there -- Android's sandboxed network stack doesn't expose a
    real hostname/FQDN to Python the way desktop OSes do -- so this used to fall
    straight through to 'Kodi — {FriendlyName}', and since FriendlyName itself
    defaults to the literal string "Kodi" until a user explicitly customizes it
    (Settings -> Services -> Control -> Device Name), the Now Playing banner
    showed the genuinely useless "Kodi — Kodi" on every un-customized install --
    indistinguishable from the original bug this function was written to fix.
    Per-user spec (2026-08-28): "try to use the device friendly name, like
    Downstairs Shield. failing that's try Kodi's friendly name. if it's Kodi,
    use the IP address of the device." Four tiers, in order:
      1. socket.getfqdn() / socket.gethostname() -- the device's own network-
         level identity (what "device friendly name" means here: NOT Kodi's
         own setting, the actual machine).
      2. System.FriendlyName -- Kodi's own configurable label, but ONLY when
         it's a real customization, not the literal unmodified default "Kodi".
      3. Network.IPAddress -- always available on any device with a live
         network connection, and at minimum distinguishes multiple
         un-customized Kodi instances from each other on the Now Playing
         banner (e.g. "10.0.0.161" vs "10.0.0.162"), unlike a generic
         "Kodi (unnamed device)" placeholder every un-named install would
         send identically.
      4. Bare "Kodi" -- only if literally nothing above produced anything
         (no network interface up at all); should be unreachable in practice.

    Confirmed live (2026-08-28, Shield Android TV box): BOTH socket calls come
    back unusable there -- Android's sandboxed network stack doesn't expose a
    real hostname/FQDN to Python the way desktop OSes do -- so this used to
    fall straight through to 'Kodi — {FriendlyName}', and since FriendlyName
    itself defaults to the literal string "Kodi" until a user explicitly
    customizes it (Settings -> Services -> Control -> Device Name), the Now
    Playing banner showed the genuinely useless "Kodi — Kodi" on every
    un-customized install.
    """
    try:
        fqdn = socket.getfqdn().strip()
        if fqdn and fqdn.lower() not in _LOOPBACK_NAMES:
            return fqdn
    except Exception as exc:
        log.debug('DNS name lookup failed: {0}'.format(exc))

    try:
        hostname = socket.gethostname().strip()
        if hostname and hostname.lower() not in _LOOPBACK_NAMES:
            return hostname
    except Exception as exc:
        log.debug('Local hostname lookup failed: {0}'.format(exc))

    try:
        # Confirmed live (2026-08-29): this used to read Window(10000).getProperty(...),
        # which only ever returns a value some script explicitly wrote with
        # setProperty() -- nothing does that for "System.FriendlyName", so this tier
        # silently returned empty on EVERY call, no matter what the user had actually
        # set as their Kodi Device Name (confirmed against a Shield with its Device
        # Name customized to "Kodi downstairs" -- this tier never once returned it,
        # falling through to the IP tier below every time instead). getInfoLabel() is
        # the correct way to read a built-in Kodi infolabel -- the same method the IP
        # tier right below already (correctly) uses.
        friendly_name = (xbmc.getInfoLabel('System.FriendlyName') or '').strip()
        if friendly_name and friendly_name.lower() != 'kodi':
            # A real, user-customized label -- trust it as-is, no "Kodi — " prefix
            # needed; the web UI already shows this is a Kodi session separately.
            return friendly_name
    except Exception as exc:
        log.debug('System.FriendlyName lookup failed: {0}'.format(exc))

    try:
        ip = (xbmc.getInfoLabel('Network.IPAddress') or '').strip()
        # Confirmed live (2026-08-29): this infolabel doesn't only ever return a real
        # dotted address or an empty string -- while Kodi's network status is still
        # being computed it can return the literal placeholder word "Busy", which the
        # old check here happily accepted (non-empty, not "localhost", not "0.0.0.0")
        # and sent to Chronicle as a scrobble's deviceName verbatim. Validating it
        # actually parses as an IP address is what "the IP address of the device"
        # in the per-user spec actually means -- not "whatever non-empty string this
        # infolabel happened to return".
        if ip and ip.lower() not in _LOOPBACK_NAMES and ip != '0.0.0.0':
            try:
                ipaddress.ip_address(ip)
                return ip
            except ValueError:
                log.debug("Network.IPAddress returned a non-IP placeholder: {0!r}".format(ip))
    except Exception as exc:
        log.debug('IP address lookup failed: {0}'.format(exc))

    # Nothing above produced anything at all -- no network interface up,
    # presumably. Should be unreachable in practice (every device this addon
    # runs on needs a working network connection to reach Chronicle at all).
    return 'Kodi'
