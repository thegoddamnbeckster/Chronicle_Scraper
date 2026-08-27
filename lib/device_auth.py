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
from lib.chronicle_client import ChronicleClient, call_with_timeout
from lib.qr_dialog import QRDialog

ADDON = xbmcaddon.Addon()
log   = Logger('device_auth')

_POLL_INTERVAL = 5      # seconds between polls
_USER_AGENT    = 'Kodi/Chronicle-Scraper/1.0'


class DeviceAuthManager:
    """Drives the full QR-code auth flow."""

    def __init__(self):
        self._client     = ChronicleClient()
        # Set by _initiate() whenever it returns None, so run() can show the
        # user what actually went wrong instead of one generic "could not
        # contact Chronicle" message regardless of cause (empty URL, DNS
        # failure, connection refused, timeout, wrong port, 404/500 from a
        # reachable-but-misconfigured server, malformed JSON...). Confirmed
        # this was a real, live complaint: the dialog gave the exact same
        # text every time, with no way to tell "you haven't entered a URL
        # yet" apart from "your URL is wrong" apart from "the server is
        # down" -- all three need a different fix from the user.
        self._last_error = None

    def run(self) -> bool:
        """
        Start the device auth flow.
        Returns True if an API key was successfully obtained, False otherwise.
        """
        # ── 1. Initiate ─────────────────────────────────────────────────────
        log.info('DeviceAuthManager.run(): starting; chronicle_url on disk = {0!r}'.format(
                 ADDON.getSetting('chronicle_url')))
        self._last_error = None
        result = self._initiate()
        log.info('DeviceAuthManager.run(): _initiate() returned {0}'.format(
                 'None' if result is None else 'a result dict (code={0!r})'.format(result.get('code'))))
        if result is None:
            reason = self._last_error or ADDON.getLocalizedString(32065)
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                '{0}\n\n{1}'.format(ADDON.getLocalizedString(32065), reason),
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
        log.info('DeviceAuthManager.run(): constructing QRDialog (qr_path={0!r})'.format(qr_path))
        dialog = QRDialog(
            qr_path          = qr_path or '',
            display_code     = display_code,
            verification_url = verification_url,
            expires_in       = expires_in,
            stop_event       = stop_event,
            api_key_holder   = api_key_holder,
        )
        log.info('DeviceAuthManager.run(): calling QRDialog.doModal()')
        dialog.doModal()     # Blocks until closed (approved, denied, expired, or cancelled)
        log.info('DeviceAuthManager.run(): QRDialog.doModal() returned (dialog closed)')
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

    def _initiate(self):
        """POST /api/v1/auth/device — returns parsed JSON data dict, or None on
        failure. On None, self._last_error carries a specific, user-facing
        reason — see the docstring on _last_error's declaration above."""
        base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        if not base_url:
            self._last_error = ADDON.getLocalizedString(32085)  # "Chronicle URL is not set."
            return None

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
            def _do():
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read().decode('utf-8'))
                    return body.get('data')
            return call_with_timeout(_do, 10)
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read().decode('utf-8', errors='replace').strip()[:200]
            except Exception:
                pass
            log.error('Device auth initiation failed: HTTP {0} {1} — {2}'.format(
                       exc.code, exc.reason, detail or '(no body)'))
            self._last_error = 'HTTP {0} {1}{2}'.format(
                exc.code, exc.reason, ' — {0}'.format(detail) if detail else '')
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, 'reason', None) or exc
            log.error('Device auth initiation failed: {0} (url={1})'.format(reason, base_url))
            self._last_error = '{0}\n({1})'.format(reason, base_url)
            return None
        except Exception as exc:
            log.error('Device auth initiation failed: {0}'.format(exc))
            self._last_error = str(exc)
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
            def _do():
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.read()
            data = call_with_timeout(_do, 10)

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
                def _do():
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return json.loads(resp.read().decode('utf-8'))
                body    = call_with_timeout(_do, 10)
                data    = body.get('data', {})
                status  = data.get('status', 'pending')
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
