# -*- coding: utf-8 -*-
"""One background sync at a time across every Chronicle add-on on this Kodi.

The movie and TV scrapers each run their own service, and each used to start its own post-scan work the instant Kodi's scan ended: the
movie add-on's watch/rating sync, the TV add-on's episode check and (from the start-up pass) the collection art sync all ran at once,
fighting the library and the shares. The intended shape is a single task: Kodi's scan (the video files) first, then the watch/rating
sync, then the collection art, one after another.

The baton is how the services take turns. Whoever holds it runs; everyone else waits their turn. It lives in a Kodi home-window property
because the services are separate Python interpreters that can only share what the window holds. A holder is re-entrant per owner (the
post-scan thread holds it across both of its passes, so nothing slips in between), and a holder that vanished without releasing (a hung
thread) stops counting after BATON_TTL_SECONDS.
"""

import contextlib
import threading
import time

BATON_TTL_SECONDS = 90 * 60
POLL_SECONDS = 2
DEFAULT_WAIT_SECONDS = 60 * 60

_HOME_WINDOW_ID = 10000
_PROPERTY = 'chronicle.sync.baton'

_held_here = {}                      # owner -> how many times this process has taken it
_held_here_lock = threading.Lock()


class _HomeWindowStore:
    """The baton's one shared value: a property on Kodi's home window. When there is no window to use (tests, a very early or very late
    moment) it falls back to a value kept in this process, so the services still take turns among themselves."""

    def __init__(self):
        self._local = ''

    def _window(self):
        try:
            import xbmcgui
            return xbmcgui.Window(_HOME_WINDOW_ID)
        except Exception:  # noqa: BLE001
            return None

    def get(self):
        window = self._window()
        if window is not None:
            try:
                return window.getProperty(_PROPERTY) or ''
            except Exception:  # noqa: BLE001
                pass
        return self._local

    def set(self, value):
        window = self._window()
        if window is not None:
            try:
                window.setProperty(_PROPERTY, value)
                return
            except Exception:  # noqa: BLE001
                pass
        self._local = value


_STORE = _HomeWindowStore()


def owner_id(addon_id):
    """A name for the caller that is the same only within one thread of one add-on."""
    return '{0}:{1}'.format(addon_id, threading.get_ident())


def _parse(raw):
    owner, _, rest = raw.partition('|')
    expiry, _, label = rest.partition('|')
    try:
        return owner, float(expiry), label
    except ValueError:
        return '', 0.0, ''


@contextlib.contextmanager
def hold(owner, label, log, is_aborted=None, wait_seconds=DEFAULT_WAIT_SECONDS, store=None, clock=time.time, sleep=time.sleep):
    """Waits for the baton and holds it for the with-block. Yields True when it is held (run the work), False when it could not be had
    within wait_seconds or Kodi is shutting down (skip the work). `label` says what the holder is doing, for the log lines."""
    store = store or _STORE
    got = _acquire(owner, label, log, is_aborted, wait_seconds, store, clock, sleep)
    try:
        yield got
    finally:
        if got:
            _release(owner, store)


def _acquire(owner, label, log, is_aborted, wait_seconds, store, clock, sleep):
    with _held_here_lock:
        if _held_here.get(owner):
            _held_here[owner] += 1
            return True
    deadline = clock() + wait_seconds
    announced = False
    while True:
        holder, expiry, holder_label = _parse(store.get())
        if not holder or holder == owner or expiry <= clock():
            store.set('{0}|{1}|{2}'.format(owner, clock() + BATON_TTL_SECONDS, label))
            # Two interpreters can both see it free in the same instant: look again and see whose write stood.
            sleep(0.2)
            if _parse(store.get())[0] == owner:
                with _held_here_lock:
                    _held_here[owner] = 1
                if announced:
                    log.info('sync: {0} -- its turn now'.format(label))
                return True
        elif not announced:
            announced = True
            log.info('sync: {0} waits for {1} to finish first'.format(label, holder_label or holder))
        if (is_aborted and is_aborted()) or clock() >= deadline:
            log.info('sync: {0} gave up waiting for {1}'.format(label, holder_label or holder or 'the other sync'))
            return False
        sleep(POLL_SECONDS)


def _release(owner, store):
    with _held_here_lock:
        count = _held_here.get(owner, 0) - 1
        if count > 0:
            _held_here[owner] = count
            return
        _held_here.pop(owner, None)
    if _parse(store.get())[0] == owner:
        store.set('')
