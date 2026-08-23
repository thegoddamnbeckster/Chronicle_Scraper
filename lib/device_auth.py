# -*- coding: utf-8 -*-
"""Orchestrates the QR-code device authentication flow for Chronicle.

Reused near-verbatim from Chronicle_Scrobbler's proven, live-tested implementation
(same Chronicle.API DeviceAuthController contract, same QR-rendering fixes already
worked out there):

  1. POST /api/v1/auth/device  → get code, display_code, qr_url, verification_url
  2. Download QR PNG from qr_url
  3. Show QR dialog with the QR image, display code and verification URL
  4. Poll GET /api/v1/auth/device/{code}/poll every 5 seconds in background
  5. When status == "approved", save apiKey to settings and close dialog
  6. When denied/expired, show error and close
"""

import socket
import threading
import time
import json
import urllib.request
import urllib.error

import xbmcvfs
import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.qr_dialog import QRDialog

ADDON = xbmcaddon.Addon()
log   = Logger('device_auth')

_POLL_INTERVAL = 5      # seconds between polls
_USER_AGENT    = 'Kodi/Chronicle-Scraper/1.0'

# "Connect to Chronicle" is a RunScript action fired from the same Settings screen as the
# chronicle_url text field (BUG-041): Kodi doesn't guarantee that field's just-typed edit is
# committed to the addon's settings store before RunScript launches this as a separate process,
# so a same-session first attempt can read back an empty value even though the user just typed
# a real one. A short retry window absorbs that race instead of failing outright.
_URL_SETTLE_ATTEMPTS      = 3
_URL_SETTLE_DELAY_SECONDS = 0.5


class DeviceAuthManager:
    """Drives the full QR-code auth flow."""

    def __init__(self):
        self._client = ChronicleClient()

    def run(self) -> bool:
        """
        Start the device auth flow.
        Returns True if an API key was successfully obtained, False otherwise.
        """
        # ── 1. Initiate ─────────────────────────────────────────────────────
        base_url = self._read_chronicle_url()
        if not base_url:
            log.warning('Device auth: chronicle_url is empty after {0} settle attempt(s) -- '
                        'either it genuinely is not configured, or Kodi has not yet committed '
                        'a just-typed value from the Settings screen (see BUG-041).'
                        .format(_URL_SETTLE_ATTEMPTS))
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32108),  # Chronicle server URL is not set
            )
            return False

        result = self._initiate(base_url)
        if result is None:
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32065),  # Could not contact Chronicle
            )
            return False

        code             = result['code']
        display_code     = result['displayCode']
        qr_url           = result['qrUrl']
        verification_url = result['verificationUrl']
        expires_in       = int(result.get('expiresInSeconds', 900))

        log.info('Device auth initiated — display code: {0}'.format(display_code))

        # ── 2. Download QR image ────────────────────────────────────────────
        # Filename includes the code so every attempt gets a fresh path -- Kodi's
        # texture manager caches loaded images in memory by path for the session,
        # so reusing one fixed filename kept showing whatever was first loaded
        # there even after the file on disk had been overwritten with new bytes.
        qr_path = self._download_qr(qr_url, code)

        # ── 3. Start polling thread ─────────────────────────────────────────
        api_key_holder = [None]   # shared result slot
        stop_event     = threading.Event()
        poll_thread    = threading.Thread(
            target=self._poll_loop,
            args=(code, api_key_holder, stop_event),
            daemon=True,
        )
        poll_thread.start()

        # ── 4. Show QR dialog ───────────────────────────────────────────────
        dialog = QRDialog(
            qr_path          = qr_path or '',
            display_code     = display_code,
            verification_url = verification_url,
            expires_in       = expires_in,
            stop_event       = stop_event,
            api_key_holder   = api_key_holder,
        )
        dialog.doModal()     # Blocks until closed (approved, denied, expired, or cancelled)
        del dialog

        stop_event.set()
        poll_thread.join(timeout=10)

        # ── 5. Save API key if approved ─────────────────────────────────────
        api_key = api_key_holder[0]
        if api_key:
            ADDON.setSetting('api_key', api_key)
            ADDON.setSetting('auth_status', ADDON.getLocalizedString(32081))  # "Connected"
            log.info('API key saved successfully')
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32066),  # Connected to Chronicle!
            )
            return True

        return False

    # ── private ────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_chronicle_url() -> str:
        """Reads chronicle_url, retrying briefly if it comes back empty -- see the
        _URL_SETTLE_ATTEMPTS comment above for why."""
        for attempt in range(_URL_SETTLE_ATTEMPTS):
            base_url = ADDON.getSetting('chronicle_url').strip().rstrip('/')
            if base_url:
                return base_url
            if attempt < _URL_SETTLE_ATTEMPTS - 1:
                time.sleep(_URL_SETTLE_DELAY_SECONDS)
        return ''

    def _initiate(self, base_url: str):
        """POST /api/v1/auth/device — returns parsed JSON data dict or None."""
        device_name = self._get_device_name()

        try:
            url     = '{0}/api/v1/auth/device'.format(base_url)
            payload = json.dumps({'deviceName': device_name}).encode('utf-8')
            req     = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json',
                         'User-Agent': _USER_AGENT},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data')
        except Exception as exc:
            log.error('Device auth initiation failed: {0}'.format(exc))
            return None

    @staticmethod
    def _get_device_name() -> str:
        """Prefer the machine's actual DNS name over Kodi's own FriendlyName setting --
        FriendlyName is just an arbitrary label the user can set to anything, while a
        real DNS name is a verifiable identifier for the physical device, which matters
        more on a screen that's asking "is this really my device connecting?".

        socket.getfqdn() tries a genuine reverse-DNS/hosts lookup and upgrades to a
        dotted name when that succeeds, but on a typical home LAN there's usually no
        such record -- it then falls back to the plain OS hostname, which is still a
        real, meaningful identifier (often mDNS-resolvable as "<hostname>.local") and
        clearly better than an arbitrary Kodi settings label. Only the degenerate
        "localhost" non-answer is treated as "no usable name".
        """
        try:
            fqdn = socket.getfqdn().strip()
            if fqdn and fqdn.lower() not in ('localhost', 'localhost.localdomain'):
                return fqdn
        except Exception as exc:
            log.debug('DNS name lookup failed: {0}'.format(exc))

        friendly_name = xbmcgui.Window(10000).getProperty('System.FriendlyName')
        return 'Kodi — {0}'.format(friendly_name or 'Kodi')

    def _download_qr(self, qr_url: str, code: str) -> str:
        """Download QR PNG to a temp file unique to this code, return its special:// VFS
        path (or '' on failure).

        Writes through xbmcvfs.File() rather than Python's raw open() -- raw open()
        puts real bytes on disk but bypasses Kodi's own VFS layer entirely, so
        xbmcvfs.exists() (and ControlImage, which resolves through that same layer)
        never sees the file. Confirmed via Chronicle_Scrobbler's own live debugging
        of this exact QR-rendering path.
        """
        vfs_path = 'special://temp/chronicle_scraper_qr_{0}.png'.format(code[:16])
        try:
            req = urllib.request.Request(
                qr_url,
                headers={'User-Agent': _USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()

            f = xbmcvfs.File(vfs_path, 'w')
            try:
                f.write(bytearray(data))
            finally:
                f.close()

            log.debug('QR image downloaded to {0}'.format(vfs_path))
            return vfs_path
        except Exception as exc:
            log.warning('QR download failed: {0}'.format(exc))
            return ''

    def _poll_loop(self, code: str, api_key_holder: list, stop_event: threading.Event):
        """Background thread: poll Chronicle until approved, denied, expired, or cancelled."""
        base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        url      = '{0}/api/v1/auth/device/{1}/poll'.format(base_url, code)

        while not stop_event.is_set():
            stop_event.wait(_POLL_INTERVAL)
            if stop_event.is_set():
                break

            try:
                req = urllib.request.Request(
                    url,
                    headers={'User-Agent': _USER_AGENT},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body   = json.loads(resp.read().decode('utf-8'))
                    data   = body.get('data', {})
                    status = data.get('status', 'pending')
                    api_key = data.get('apiKey')

                log.debug('Poll status: {0}'.format(status))

                if status == 'approved' and api_key:
                    api_key_holder[0] = api_key
                    stop_event.set()
                    break
                elif status in ('denied', 'expired'):
                    stop_event.set()
                    break

            except Exception as exc:
                log.warning('Poll error: {0}'.format(exc))
