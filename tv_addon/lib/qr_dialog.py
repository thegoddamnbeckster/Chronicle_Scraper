# -*- coding: utf-8 -*-
"""QR code device-auth dialog for the Kodi UI.

Displays the QR image, human-readable display code, and verification URL.
A background thread watches the stop_event; when it fires the dialog updates
its status label and closes itself automatically.
"""

import os
import threading
import time

import xbmc
import xbmcgui
import xbmcaddon

from lib.logger import Logger

ADDON      = xbmcaddon.Addon()
ADDON_PATH = ADDON.getAddonInfo('path')
log        = Logger('qr_dialog')

# Solid backing panel — WindowDialog itself is fully transparent, so without
# an image control behind everything else the QR card just floats over
# whatever's on screen (Settings, in this case) with no way to read either.
PANEL_PATH = os.path.join(ADDON_PATH, 'resources', 'qr_panel.png')

# Kodi action IDs
ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK      = 92

# ── Layout constants (1280 × 720 Kodi coordinate space) ──────────────────────

SCR_W, SCR_H = 1280, 720

CARD_W = 460
CARD_H = 580   # +40 over the original 540, to make room for the countdown label
CARD_X = (SCR_W - CARD_W) // 2   # 410
CARD_Y = (SCR_H - CARD_H) // 2   # 70

QR_SIZE = 220
QR_X    = CARD_X + (CARD_W - QR_SIZE) // 2   # 520
QR_Y    = CARD_Y + 100                         # 190


class QRDialog(xbmcgui.WindowDialog):
    """Full-screen overlay that guides the user through the QR auth flow.

    Parameters
    ----------
    qr_path:          Local path to the downloaded QR PNG (may be '' on error).
    display_code:     Human-readable code shown on screen, e.g. "A1B2-C3D4".
    verification_url: URL the user can type instead of scanning the QR.
    expires_in:       Seconds until the code expires (informational).
    stop_event:       threading.Event — set by the poll thread when done.
    api_key_holder:   list[str | None] — [0] is filled in on approval.
    """

    def __init__(
        self,
        qr_path:          str,
        display_code:     str,
        verification_url: str,
        expires_in:       int,
        stop_event,
        api_key_holder,
    ):
        super().__init__()

        self._qr_path          = qr_path
        self._display_code     = display_code
        self._verification_url = verification_url
        self._expires_in       = expires_in
        self._stop_event       = stop_event
        self._api_key_holder   = api_key_holder

        self._status_label    = None
        self._countdown_label = None
        self._cancel_btn_id   = None
        self._monitor_thread  = None

        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        cx = CARD_X
        cw = CARD_W
        y  = CARD_Y + 22

        # Dim the whole screen first, then draw the opaque card on top of that —
        # both added before any text/QR controls so nothing behind the dialog
        # (e.g. the Settings page that opened it) shows through either layer.
        self.addControl(xbmcgui.ControlImage(
            0, 0, SCR_W, SCR_H,
            PANEL_PATH, colorDiffuse='B3000000',
        ))
        self.addControl(xbmcgui.ControlImage(
            CARD_X, CARD_Y, CARD_W, CARD_H,
            PANEL_PATH,
        ))

        # Title
        self.addControl(xbmcgui.ControlLabel(
            cx, y, cw, 44,
            ADDON.getLocalizedString(32060),   # "Connect to Chronicle"
            font='font20', alignment=2,        # 2 = XBFONT_CENTER_X
        ))
        y += 52

        # Subtitle
        self.addControl(xbmcgui.ControlLabel(
            cx, y, cw, 28,
            ADDON.getLocalizedString(32063),   # "Scan the QR code with your phone"
            font='font12', alignment=2,
        ))
        y += 36

        # QR image — show only when the download succeeded
        if self._qr_path:
            self.addControl(xbmcgui.ControlImage(
                QR_X, QR_Y, QR_SIZE, QR_SIZE,
                self._qr_path,
            ))
        y = QR_Y + QR_SIZE + 18

        # Verification URL (truncate long URLs with ellipsis)
        url_text = self._verification_url
        if len(url_text) > 54:
            url_text = url_text[:52] + '…'
        self.addControl(xbmcgui.ControlLabel(
            cx, y, cw, 24,
            url_text,
            font='font10', alignment=2,
        ))
        y += 32

        # "Your code" label
        self.addControl(xbmcgui.ControlLabel(
            cx, y, cw, 22,
            ADDON.getLocalizedString(32068),   # "Your code"
            font='font10', alignment=2,
        ))
        y += 26

        # Display code — large, prominent
        self.addControl(xbmcgui.ControlLabel(
            cx, y, cw, 54,
            self._display_code,
            font='font30', alignment=2,
        ))
        y += 62

        # Status (mutable — updated by monitor thread)
        self._status_label = xbmcgui.ControlLabel(
            cx, y, cw, 30,
            ADDON.getLocalizedString(32064),   # "Waiting for approval…"
            font='font12', alignment=2,
        )
        self.addControl(self._status_label)
        y += 34

        # Countdown — informational only; actual expiry is still detected
        # server-side by the poll thread in device_auth.py regardless of
        # whether this ever reaches zero (e.g. Kodi under heavy load).
        self._countdown_label = xbmcgui.ControlLabel(
            cx, y, cw, 26,
            '',
            font='font10', alignment=2,
            textColor='FFFFDD00',
        )
        self.addControl(self._countdown_label)
        y += 34

        # Cancel button — deliberately NOT given custom focus/noFocus textures: a
        # hardcoded color would look the same in every skin, which isn't what we want.
        # No texture params means Kodi draws its own current skin's native button/focus
        # look (whatever accent color that skin uses), while textColor/focusedColor
        # still force legible white text and the larger box makes sure "Cancel" fits.
        btn_w = 180
        btn_h = 44
        btn_x = cx + (cw - btn_w) // 2
        cancel_btn = xbmcgui.ControlButton(
            btn_x, y, btn_w, btn_h,
            ADDON.getLocalizedString(32069),   # "Cancel"
            font='font12',
            textColor='FFFFFFFF',
            focusedColor='FFFFFFFF',
            disabledColor='FF999999',
            alignment=2 | 4,   # centered horizontally and vertically
        )
        self.addControl(cancel_btn)
        self.setFocus(cancel_btn)
        self._cancel_btn_id = cancel_btn.getId()

    # ── Modal entry ───────────────────────────────────────────────────────────

    def doModal(self):
        """Start the background monitor then block on Kodi's event loop."""
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
        )
        self._monitor_thread.start()
        super().doModal()

    # ── Input handlers ────────────────────────────────────────────────────────

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            log.debug('QRDialog: back/escape — cancelling')
            self._stop_event.set()
            self.close()

    def onControl(self, control):
        if self._cancel_btn_id and control.getId() == self._cancel_btn_id:
            log.debug('QRDialog: cancel button — cancelling')
            self._stop_event.set()
            self.close()

    # ── Background monitor ────────────────────────────────────────────────────

    def _monitor_loop(self):
        """Tick the countdown every second; when stop_event fires (set by the
        polling thread in device_auth.py -- approved, denied, cancelled, or
        actually expired), show the final status and close.

        The countdown reaching zero here is purely cosmetic -- real expiry is
        still decided server-side and detected via polling, same as before;
        this loop just stops ticking once it hits 00:00 and waits for that.
        """
        monitor  = xbmc.Monitor()
        deadline = time.monotonic() + self._expires_in

        while not monitor.abortRequested() and not self._stop_event.is_set():
            remaining = max(0, int(deadline - time.monotonic()))
            self._set_countdown(ADDON.getLocalizedString(32084).format(
                '{0:02d}:{1:02d}'.format(remaining // 60, remaining % 60)
            ))
            if self._stop_event.wait(timeout=1.0):
                break

        if not self._stop_event.is_set():
            return   # Kodi is shutting down, not a real completion

        self._set_countdown('')
        if self._api_key_holder[0]:
            # Approved — key was stored
            self._set_status(ADDON.getLocalizedString(32066))   # "Connected to Chronicle!"
            xbmc.sleep(1400)
        else:
            # Denied, expired, or cancelled by the user
            self._set_status(ADDON.getLocalizedString(32067))   # "Cancelled or expired."
            xbmc.sleep(1800)
        self.close()

    def _set_status(self, text: str):
        if self._status_label is not None:
            self._status_label.setLabel(text)

    def _set_countdown(self, text: str):
        if self._countdown_label is not None:
            self._countdown_label.setLabel(text)
