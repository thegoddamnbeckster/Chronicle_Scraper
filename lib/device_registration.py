# -*- coding: utf-8 -*-
"""Self-registers this Kodi instance's own remote-control (JSON-RPC over HTTP) address with
Chronicle, so its NfoPushService can push a freshly-changed item's NFO straight here instead
of waiting for a manual/scheduled rebuild pass or this device's own next library scan. See
Chronicle's own KodiDevice model for the full design and why this addon -- not the user --
supplies these details: everything needed (Kodi's own configured webserver enabled/port/
username/password, and this device's own outbound-facing LAN IP) is only ever known locally,
inside Kodi's own process; Chronicle's server has no way to discover any of it on its own.

Read-only against Kodi's own settings -- never changes them. Skips registration (silently,
once logged at info level) when "Allow remote control via HTTP" is off; there's nothing to
register in that case, and it's a common, valid configuration -- this addon's other features
all work fine without it, only server-initiated NFO pushes need it.

Called from default.py right after a successful "Connect to Chronicle" pairing, and
periodically from service.py's own idle loop, so a changed LAN IP (DHCP lease renewal) or a
toggled webserver setting doesn't leave Chronicle holding a stale, unreachable address
indefinitely.
"""

import json
import socket

import xbmc

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('device_registration')

# Kodi 18+ ("Leia" onward) setting ids -- current for every version this addon already
# targets (xbmc.python 3.0.0+, see addon.xml), so no older "network.webserver*" fallback.
_SETTING_ENABLED  = 'services.webserver'
_SETTING_PORT     = 'services.webserverport'
_SETTING_USERNAME = 'services.webserverusername'
_SETTING_PASSWORD = 'services.webserverpassword'


def _get_setting_value(setting_id):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'Settings.GetSettingValue',
        'params': {'setting': setting_id},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning('device_registration: could not read setting {0!r}: {1}'.format(setting_id, exc))
        return None
    if 'error' in response:
        log.warning('device_registration: Settings.GetSettingValue rejected {0!r}: {1}'.format(
                    setting_id, response['error']))
        return None
    return response.get('result', {}).get('value')


def _local_ip():
    """This device's own outbound-facing LAN IP via the UDP-connect trick -- no packet is
    actually sent (UDP "connect" just picks a local interface/route, no handshake), which is
    more reliable than socket.gethostbyname(socket.gethostname()), a common source of a
    loopback or wrong-interface address on a multi-homed or misconfigured machine. 8.8.8.8 is
    just a stable, always-routable address to pick a route toward; nothing is ever sent to it.
    Returns None if this device has no route to any external network at all -- registration is
    skipped in that case since there'd be nothing reachable to report anyway."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception as exc:
        log.warning('device_registration: could not determine this device\'s own LAN IP: {0}'.format(exc))
        return None
    finally:
        s.close()


def register():
    """Best-effort and silent on the common "nothing to register" case (remote control off,
    or Chronicle not yet configured -- ChronicleClient's own guard handles the latter, same as
    every other client call). Safe to call repeatedly: upserts server-side, keyed by this
    device's own API token -- see Chronicle's KodiDevice model for why that's the right key."""
    enabled = _get_setting_value(_SETTING_ENABLED)
    if not enabled:
        log.info('device_registration: Kodi\'s "Allow remote control via HTTP" is off -- '
                 'nothing to register (Chronicle cannot push NFO updates straight to this '
                 'device, but every other feature of this addon works fine without it).')
        return

    port = _get_setting_value(_SETTING_PORT)
    if not port:
        log.warning('device_registration: remote control is on but no port setting was returned -- skipping.')
        return

    host = _local_ip()
    if not host:
        return

    username = _get_setting_value(_SETTING_USERNAME) or None
    password = _get_setting_value(_SETTING_PASSWORD) or None
    device_name = xbmc.getInfoLabel('System.FriendlyName') or 'Kodi'

    ChronicleClient().register_device(device_name, host, int(port), username, password)
