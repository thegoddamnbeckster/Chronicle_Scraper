# -*- coding: utf-8 -*-
"""Tells the user when an add-on is not really talking to Chronicle.

Without this the add-ons failed quietly: a missing or revoked API key just logged a line and every scan, scrobble and sync carried on
"successfully" with nothing done, while the Settings page still said "Connected as ...". Now the first time a problem shows up (and
again every few hours while it lasts) the add-on raises a Kodi warning that says what is wrong and what to do about it.

Three problems are reported:
  * not_configured -- no Chronicle address or no API key is saved, so no request can even be made.
  * unauthorized   -- Chronicle answered HTTP 401/403: the API key was revoked, deleted or never valid for this server.
  * unreachable    -- Chronicle did not answer at all (after the retries), twice in a row.

How it is wired: each add-on's client builds one ConnectionHealth with install(), which also registers it as the observer of http_core, so
every request any of the add-on's code makes is watched without each call site doing anything. The one thing that is checked explicitly is
the configuration, because a request that is never made cannot fail (ConnectionHealth.not_configured).

The state is kept in a Kodi home-window property, not in a module variable: Kodi runs the service, the scraper and the Settings page in
separate Python interpreters, and all of them must share one "already told the user" memory or the same warning would pop up from each.
"""

import time

# How long a reported problem stays quiet before the same problem is raised again. Long enough not to nag, short enough that someone who
# dismissed it and forgot hears about it again.
RENOTIFY_AFTER_SECONDS = 6 * 60 * 60

# Consecutive unreachable results before the user is told: a request is already retried a couple of times, but a single dropped
# packet or a server restart should not raise a warning.
UNREACHABLE_THRESHOLD = 2

NOTIFICATION_MILLISECONDS = 15000

NOT_CONFIGURED = 'not_configured'
UNAUTHORIZED = 'unauthorized'
UNREACHABLE = 'unreachable'

_HOME_WINDOW_ID = 10000
_PROPERTY_PREFIX = 'chronicle.health.'


def message_for(problem, label, base_url=''):
    """(heading, text) of the warning for a problem."""
    heading = 'Chronicle: {0} is not connected'.format(label)
    if problem == NOT_CONFIGURED:
        return heading, ('No Chronicle address or API key is saved, so nothing is syncing. '
                         'Open this add-on\'s settings and choose Edit Connection.')
    if problem == UNAUTHORIZED:
        return heading, ('Chronicle rejected this device\'s API key, so nothing is syncing. '
                         'Open this add-on\'s settings and choose Edit Connection to reconnect.')
    return ('Chronicle: {0} cannot reach Chronicle'.format(label),
            'No answer from {0}. Nothing is syncing until it can be reached.'.format(base_url or 'the Chronicle server'))


class _HomeWindowStore:
    """The shared "what was last reported" memory: a property on Kodi's home window, read and written through xbmcgui."""

    def get(self, key):
        try:
            import xbmcgui
            return xbmcgui.Window(_HOME_WINDOW_ID).getProperty(_PROPERTY_PREFIX + key) or ''
        except Exception:  # noqa: BLE001 -- no window (tests, shutdown): behave as if nothing had been reported
            return ''

    def set(self, key, value):
        try:
            import xbmcgui
            xbmcgui.Window(_HOME_WINDOW_ID).setProperty(_PROPERTY_PREFIX + key, value)
        except Exception:  # noqa: BLE001
            pass


def _default_notify(heading, text):
    import xbmcgui
    xbmcgui.Dialog().notification(heading, text, xbmcgui.NOTIFICATION_WARNING, NOTIFICATION_MILLISECONDS)


class ConnectionHealth:
    def __init__(self, addon_id, label, log, base_url_getter=None, notify=None, store=None, clock=time.time):
        """addon_id/label: which add-on is speaking. base_url_getter(): the saved Chronicle address (for the "unreachable" text)."""
        self._addon_id = addon_id
        self._label = label
        self._log = log
        self._base_url_getter = base_url_getter or (lambda: '')
        self._notify = notify or _default_notify
        self._store = store or _HomeWindowStore()
        self._clock = clock
        self._unreachable_in_a_row = 0

    # ── what the client reports ──────────────────────────────────────────────────

    def not_configured(self, base_url, api_key):
        """Call where a request would be made. Returns True when the add-on is configured (nothing reported); otherwise reports the
        missing connection and returns False."""
        if base_url and api_key:
            return True
        self._report(NOT_CONFIGURED)
        return False

    def observe(self, kind, detail=None):
        """The http_core observer: kind is 'ok', 'http' (detail = status code) or 'unreachable'."""
        if kind == 'http' and detail in (401, 403):
            self._unreachable_in_a_row = 0
            self._report(UNAUTHORIZED)
        elif kind == 'unreachable':
            self._unreachable_in_a_row += 1
            if self._unreachable_in_a_row >= UNREACHABLE_THRESHOLD:
                self._report(UNREACHABLE)
        else:
            # An answer of any other kind (even a 404 or 500) proves the address is right and the key was accepted.
            self._unreachable_in_a_row = 0
            self._clear()

    def current_problem(self):
        """The problem last reported and not cleared since, or '' -- for the Settings status line."""
        return self._store.get(self._addon_id)

    # ── internals ────────────────────────────────────────────────────────────────

    def _report(self, problem):
        """Marks the problem as current and raises the warning, unless this same problem was already raised within
        RENOTIFY_AFTER_SECONDS. The time of the last warning is kept per problem and survives _clear(): a request that happens to be
        answered (an unauthenticated health check, say) between two failures must not make the same warning pop up again each time."""
        self._store.set(self._addon_id, problem)
        now = self._clock()
        stamp_key = '{0}.{1}'.format(self._addon_id, problem)
        try:
            if now - float(self._store.get(stamp_key)) < RENOTIFY_AFTER_SECONDS:
                return
        except ValueError:
            pass                                          # never warned about this problem yet
        self._store.set(stamp_key, str(now))
        heading, text = message_for(problem, self._label, self._base_url_getter())
        self._log.warning('{0}: {1}'.format(heading, text))
        try:
            self._notify(heading, text)
        except Exception as exc:  # noqa: BLE001 -- a notification must never break the call that triggered it
            self._log.warning('could not show the connection warning: {0}'.format(exc))

    def _clear(self):
        if self._store.get(self._addon_id):
            self._store.set(self._addon_id, '')


def _saved_base_url():
    """The saved Chronicle address, read through a fresh Addon object (an old one would not see a later change)."""
    import xbmcaddon
    return (xbmcaddon.Addon().getSetting('chronicle_url') or '').strip()


def install(addon, log, base_url_getter=None):
    """Builds the ConnectionHealth for this add-on and makes it watch every http_core request. Returns it. base_url_getter: where the
    saved address is, when it is not this add-on's own chronicle_url setting (the rating add-on uses the scrobbler's)."""
    from lib import http_core
    health = ConnectionHealth(addon.getAddonInfo('id'), addon.getAddonInfo('name'), log,
                              base_url_getter=base_url_getter or _saved_base_url)
    http_core.set_observer(health.observe)
    return health
