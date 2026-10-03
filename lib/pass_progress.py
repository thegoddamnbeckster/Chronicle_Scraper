# -*- coding: utf-8 -*-
"""Visible progress for the add-on's background passes (the post-scan "verify every movie/episode"
checks and the watch-history/ratings sync).

Per-user requirement (2026-10-03): these passes walk the whole library and write corrections into it,
which makes Kodi's screens refresh and flicker. For someone who doesn't know what is going on that is
alarming, so every pass must say what it is doing from the moment it starts until it ends:

  * a corner progress dialog opens the instant the pass starts, saying so and warning that the screen
    may refresh, then shows "412 of 1,227 -- <title>" with a percentage as it works;
  * kodi.log gets a start line, a progress line every minute, and (from the caller) a final summary.

Shared by the Movies and TV add-ons (build.ps1 copies this one file into the TV package). Every UI
call is guarded: a progress dialog is never worth breaking the pass it is reporting on.
"""

import time

import xbmcgui

_MAX_LABEL_CHARS = 60


class PassProgress:

    def __init__(self, heading, intro, logger, ui_interval_seconds=1.0, log_interval_seconds=60.0,
                 dialog_factory=None, clock=None):
        self._heading = heading
        self._intro = intro
        self._log = logger
        self._ui_interval = ui_interval_seconds
        self._log_interval = log_interval_seconds
        self._dialog_factory = dialog_factory or xbmcgui.DialogProgressBG
        self._clock = clock or time.time
        self._dialog = None
        self._started_at = None
        self._last_ui = None
        self._last_log = None
        self._ui_failed = False
        self._finished = False

    def start(self):
        """Open the dialog now, before the pass does anything slow (listing the library can take a
        while), so there is never a stretch where the library is being worked on in silence."""
        self._started_at = self._last_log = self._clock()
        self._log.info('{0}: starting'.format(self._heading))
        self._guarded(self._open)

    def update(self, index, total, label):
        """index is how many items are done before this one (0-based position of the current item)."""
        if self._finished:
            return
        now = self._clock()
        if self._started_at is None:
            self.start()

        position = min(index + 1, total) if total else index + 1
        percent = min(100, int(index * 100 / total)) if total else 0
        label = (label or '').strip()
        if len(label) > _MAX_LABEL_CHARS:
            label = label[:_MAX_LABEL_CHARS - 1] + '…'

        if self._last_ui is None or now - self._last_ui >= self._ui_interval:
            self._last_ui = now
            message = '{0:,} of {1:,}  --  {2}'.format(position, total, label) if total \
                else '{0:,} done  --  {1}'.format(index, label)
            self._guarded(lambda: self._show(percent, message))

        if now - self._last_log >= self._log_interval:
            self._last_log = now
            self._log.info('{0}: {1:,} of {2:,} ({3}%) -- {4} -- {5:.0f}s elapsed'.format(
                self._heading, position, total, percent, label, now - self._started_at))

    def finish(self):
        """Close the dialog. Safe to call whether or not start()/update() ever ran."""
        self._finished = True   # a straggling update() must not reopen a dialog nothing will close
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            self._guarded(dialog.close)

    # ── internals ─────────────────────────────────────────────────────────────

    def _open(self):
        self._dialog = self._dialog_factory()
        self._dialog.create(self._heading, self._intro)

    def _show(self, percent, message):
        if self._dialog is None:
            self._open()
        self._dialog.update(percent, message=message)

    def _guarded(self, action):
        """Runs a UI action; on failure logs once and stops trying (the pass itself carries on)."""
        if self._ui_failed:
            return
        try:
            action()
        except Exception as exc:
            self._ui_failed = True
            dialog, self._dialog = self._dialog, None
            if dialog is not None:
                try:
                    dialog.close()   # never leave a half-working dialog hanging on screen
                except Exception:
                    pass
            self._log.warning('{0}: progress dialog unavailable ({1}) -- continuing without it'.format(
                self._heading, exc))
