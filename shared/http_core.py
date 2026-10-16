# -*- coding: utf-8 -*-
"""What every Chronicle Kodi add-on's HTTP client has in common.

Shared by the movie and TV scraper add-ons, the scrobbler and the rating add-on (each add-on's build copies this file into its own
lib/). Each add-on keeps its own ChronicleClient with the calls it needs; the parts that must behave the same everywhere live here,
so a fix is made once:

  * call_with_timeout    -- a hard time limit that also covers the DNS lookup urlopen(timeout=) does not.
  * call_with_retries    -- the same, retried a couple of times for a request that never landed.
  * normalize_base_url   -- what the user typed for the server address, made safe to use (http/https only).
  * user_agent / api_headers -- the identity every request carries.
"""

import threading
import time
import urllib.error
import urllib.parse

# urlopen(timeout=N) only bounds the socket once it exists -- the DNS lookup (getaddrinfo) before that is NOT covered by that
# timeout on any platform. A dead DNS server or a stale hostname can hang a "timed" call forever, well past the timeout passed in:
# "Chronicle is unreachable and the add-on just hangs". call_with_timeout() is the backstop: it runs the call on a daemon thread and
# gives up after timeout + WATCHDOG_GRACE_SECONDS even if that thread never returns, leaving the runaway thread to die on its own
# (daemon=True means it cannot keep Kodi from exiting).
WATCHDOG_GRACE_SECONDS = 5

# A full library rescan fires a burst of concurrent scraper calls at Chronicle; one transient timeout during that burst could leave an
# item without artwork until something re-triggered the scrape. Retrying a couple of times with a short backoff absorbs that
# contention without masking a genuinely-down Chronicle (which still fails every retry). Not applied to an HTTP error answer from a
# reachable server (a 401 will not change on a retry) except the few statuses that mean "not serving right now".
RETRY_BACKOFF_SECONDS = (1, 3)
RETRYABLE_HTTP_STATUSES = (429, 502, 503, 504)

# The User-Agent each add-on has always sent (Chronicle's request log names the device by it).
_USER_AGENTS = {
    'script.chronicle.scraper.movie': 'Kodi/Chronicle-Scraper/1.0',
    'script.chronicle.scraper.tv': 'Kodi/Chronicle-Scraper/1.0',
    'service.chronicle.scrobbler': 'Kodi/Chronicle-Scrobbler/1.0',
    'script.chronicle.rating': 'Kodi/Chronicle-Rating/1.0',
}
_DEFAULT_USER_AGENT = 'Kodi/Chronicle/1.0'


def user_agent(addon_id):
    """The User-Agent for the add-on with this id."""
    return _USER_AGENTS.get(addon_id, _DEFAULT_USER_AGENT)


def api_headers(api_key, agent, json_body=True):
    """The headers every authenticated Chronicle request carries."""
    headers = {'X-Api-Key': api_key, 'User-Agent': agent}
    if json_body:
        headers['Content-Type'] = 'application/json'
    return headers


def call_with_timeout(fn, timeout):
    """Runs fn() and returns its result, raising TimeoutError if it has not finished after timeout + the watchdog grace."""
    result = {}

    def _target():
        try:
            result['value'] = fn()
        except BaseException as exc:  # noqa: BLE001 -- handed to the caller's thread, which re-raises it
            result['error'] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout + WATCHDOG_GRACE_SECONDS)
    if t.is_alive():
        raise TimeoutError('no response within {0}s (DNS/network hang)'.format(timeout + WATCHDOG_GRACE_SECONDS))
    if 'error' in result:
        raise result['error']
    return result.get('value')


def call_with_retries(fn, timeout, log_label, log, sleep=time.sleep, backoff=None):
    """call_with_timeout, tried again after a connection-level failure (never after an HTTP error response)."""
    delays = RETRY_BACKOFF_SECONDS if backoff is None else backoff
    attempts = len(delays) + 1
    for attempt in range(attempts):
        try:
            return call_with_timeout(fn, timeout)
        except urllib.error.HTTPError as exc:
            # HTTPError is a kind of URLError, so it must be caught first or every 401/404 would be retried. Only a server that
            # is momentarily not serving (restarting, behind a proxy that has not found it) is worth another try.
            if exc.code not in RETRYABLE_HTTP_STATUSES or attempt >= attempts - 1:
                raise
            delay = delays[attempt]
            log.warning('{0}: attempt {1}/{2} got HTTP {3} -- retrying in {4}s'.format(
                log_label, attempt + 1, attempts, exc.code, delay))
            sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= attempts - 1:
                raise
            delay = delays[attempt]
            log.warning('{0}: attempt {1}/{2} failed ({3}) -- retrying in {4}s'.format(
                log_label, attempt + 1, attempts, exc, delay))
            sleep(delay)
    return None  # unreachable: the last attempt either returns or raises


def normalize_base_url(text):
    """The server address the user typed, made usable: whitespace and trailing slashes removed, and "http://" assumed when no
    scheme was typed ("chronicle.local:7979"). Returns '' for anything that is not an http(s) address -- urllib would otherwise accept
    file:// and ftp:// and read local files or talk to other services on the user's behalf."""
    text = (text or '').strip().rstrip('/')
    if not text or text.lower() in ('http:', 'https:'):   # nothing typed but a scheme (its slashes were stripped above)
        return ''
    if '://' not in text:
        text = 'http://' + text
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port  # noqa: B018 -- raises ValueError for a malformed port ("host:abc"), which would otherwise only fail at connect time
    except ValueError:
        return ''
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        return ''
    return text


def is_loopback(url):
    """True when the address points at the device itself (localhost, 127.x, ::1): fine on the server's own machine, wrong for a Kodi
    box that is somewhere else on the network."""
    try:
        host = (urllib.parse.urlsplit(url if '://' in url else 'http://' + url).hostname or '').lower()
    except ValueError:
        return False
    return host in ('localhost', 'localhost.localdomain', '::1') or host.startswith('127.')


# Every Chronicle add-on that asks the user for its own server address; used to offer the address one of them already has.
# (Chronicle Rating is not among them: it reuses the scrobbler's connection outright.)
SIBLING_ADDON_IDS = (
    'script.chronicle.scraper.movie',
    'script.chronicle.scraper.tv',
    'service.chronicle.scrobbler',
)


def find_shared_chronicle_url(this_addon_id, log, addon_factory=None):
    """The first non-blank chronicle_url another installed Chronicle add-on already has, or None -- so the "Edit Connection" prompt can
    be pre-filled instead of starting blank (per-user request 2026-08-29: "save the typing"). Read-only: never writes to another add-on's
    settings. A sibling that is not installed, or has nothing set yet, is skipped."""
    if addon_factory is None:
        import xbmcaddon
        addon_factory = xbmcaddon.Addon
    for addon_id in SIBLING_ADDON_IDS:
        if addon_id == this_addon_id:
            continue
        try:
            other = addon_factory(addon_id)
        except RuntimeError:
            continue  # not installed on this device
        url = (other.getSetting('chronicle_url') or '').strip()
        if url:
            log.info('find_shared_chronicle_url: found {0!r} from {1}'.format(url, addon_id))
            return url
    return None
