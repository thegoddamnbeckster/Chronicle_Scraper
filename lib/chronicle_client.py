# -*- coding: utf-8 -*-
"""HTTP client for the Chronicle REST API.

Uses urllib from the Python standard library — no third-party packages
needed inside Kodi's Python environment.

Authentication: X-Api-Key header (same Chronicle API key format and
device-auth flow as Chronicle_Scrobbler).
"""

import json
import threading
import urllib.request
import urllib.error
import urllib.parse
import xbmcaddon

from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('client')

_USER_AGENT = 'Kodi/Chronicle-Scraper/1.0'

# Every OTHER Chronicle addon that independently asks the user for its own
# chronicle_url -- used by find_shared_chronicle_url() below. Chronicle_Rating is
# deliberately NOT here: it already reuses Chronicle_Scrobbler's saved connection
# outright rather than asking for a URL of its own at all.
_SIBLING_CHRONICLE_ADDON_IDS = (
    'script.chronicle.scraper.movie',
    'script.chronicle.scraper.tv',
    'service.chronicle.scrobbler',
)


def find_shared_chronicle_url():
    """Per-user request (2026-08-29): "each of the add-ons could check each other
    for the chronicle URL and just copy it in from another working add-on... save
    the typing." Checks every OTHER installed sibling Chronicle addon's own
    chronicle_url setting (read-only from here -- this never writes to another
    addon's settings, only reads) and returns the first non-blank one found, so
    the "Edit Connection" URL prompt can be pre-filled instead of starting blank.
    A sibling with nothing set of its own (fresh install, never configured) is
    silently skipped, not treated as "the shared URL is blank" -- only a REAL,
    already-working URL from another addon is ever offered. Returns None if no
    sibling is installed, or none of the installed ones have a URL set yet.
    """
    this_id = ADDON.getAddonInfo('id')
    for addon_id in _SIBLING_CHRONICLE_ADDON_IDS:
        if addon_id == this_id:
            continue
        try:
            other = xbmcaddon.Addon(addon_id)
        except RuntimeError:
            continue  # not installed on this device
        url = (other.getSetting('chronicle_url') or '').strip()
        if url:
            log.info('find_shared_chronicle_url: found {0!r} from {1}'.format(url, addon_id))
            return url
    return None

# urlopen(timeout=N) only bounds the socket once it exists -- the DNS lookup
# (getaddrinfo) that happens before that is NOT covered by that timeout on
# any platform. A dead/unreachable DNS server or a stale hostname can hang
# a "timed" call forever, well past whatever timeout= was passed in, which
# is exactly what "Chronicle is unreachable and the scraper just hangs"
# turned out to be -- not a missing timeout, but one that doesn't cover the
# step that actually hung. call_with_timeout() is the backstop: it runs the
# call on a daemon thread and gives up after timeout + _WATCHDOG_GRACE_SECONDS
# even if that thread never returns, leaving the runaway thread to die on
# its own (daemon=True means it can't block Kodi from exiting).
_WATCHDOG_GRACE_SECONDS = 5


def call_with_timeout(fn, timeout):
    result = {}

    def _target():
        try:
            result['value'] = fn()
        except BaseException as exc:
            result['error'] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout + _WATCHDOG_GRACE_SECONDS)
    if t.is_alive():
        raise TimeoutError('no response within {0}s (DNS/network hang)'.format(
            timeout + _WATCHDOG_GRACE_SECONDS))
    if 'error' in result:
        raise result['error']
    return result.get('value')


# search_movie/search_show can trigger Chronicle's resolve-or-create path for a
# title it's never seen before -- which walks every configured metadata
# provider SEQUENTIALLY (each individually bounded up to 25s server-side), not
# in parallel. Confirmed directly (2026-07-30, a full-library NFO rebuild):
# 64 distinct titles hit the plain 20s default below and got nothing back even
# though Chronicle was still working -- not a hang, just legitimately slow
# multi-provider enrichment for a brand-new title. The everyday lookups
# (get_movie_details etc.) stay at the tighter default since those only ever
# read an item Chronicle has already resolved.
_SEARCH_TIMEOUT_SECONDS = 90


class ChronicleClient:
    """Talks to Chronicle's scraper-facing endpoints (api/v1/scraper/movies/*)."""

    def __init__(self):
        self._base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        self._api_key  = ADDON.getSetting('api_key')

    def refresh_settings(self):
        """Re-read URL and API key from addon settings.

        Call this after a device-auth flow completes so the client picks up
        the newly saved API key without needing to be reconstructed.
        """
        self._base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        self._api_key  = ADDON.getSetting('api_key')

    # ── scraper endpoints ────────────────────────────────────────────────────

    def search_movie(self, title: str, year=None, filename=None):
        """GET /api/v1/scraper/movies/search?title=&year=&fileName=

        Chronicle resolves-or-creates the item server-side (through its own
        configured metadata providers) and returns exactly one candidate --
        there's nothing for this addon to disambiguate.

        filename, when given, is the real video file's own basename (with
        extension) -- a verified fact about the physical file, not a re-
        derived title guess. Chronicle checks it BEFORE title-matching: if
        some other already-known item's own recorded file has this exact
        basename, that item wins outright even when its stored title doesn't
        tokenize-match what Kodi derived from the folder name. This is what
        stops a fan edit filed under a franchise-prefixed folder (e.g.
        "Alien - Derelict") from spawning a second, wrongly-typed, posterless
        duplicate of the real item ("Derelict") every time title-only
        matching would otherwise miss it.

        Returns {'id', 'title', 'year', 'posterUrl'} dict, or None on any
        failure (not configured, network error, Chronicle couldn't resolve it).
        """
        if not self._base_url or not self._api_key:
            log.warning('Chronicle URL or API key not configured — search skipped')
            return None

        url = '{0}/api/v1/scraper/movies/search?title={1}'.format(
            self._base_url, urllib.parse.quote(title))
        if year:
            url += '&year={0}'.format(year)
        if filename:
            url += '&fileName={0}'.format(urllib.parse.quote(filename))
        return self._get(url, 'search_movie({0!r}, {1!r}, {2!r})'.format(title, year, filename), full_url=True,
                          timeout=_SEARCH_TIMEOUT_SECONDS)

    def get_movie_details(self, media_item_id: int):
        """GET /api/v1/scraper/movies/details?id=

        Returns the full details dict -- title, overview, tagline, year,
        premiered, mpaa, country, studio, runtimeMinutes, genres,
        cast (list of {name, role} -- role is None/absent when the source
        provider doesn't supply a character name), crew (list of {name, job}
        -- every non-actor credit Chronicle has, e.g. director/writer/
        producer/composer; job is None/absent when the source provider only
        supplies a flat name list), tags, ratings (per-source), trailerUrl,
        externalIds (imdb/tvdb/tmdb/trakt), artwork (per-arttype candidate
        lists) and collection -- or None on failure.

        "collection" carries every art type Kodi's movie-set folder accepts
        (posterUrl, backdropUrl, logoUrl, bannerUrl, clearartUrl, discUrl,
        thumbUrl) plus "pinnedSlots": the canonical slot names the user
        explicitly chose in Chronicle. Sets have no scraper hook of their own,
        so this payload is the only channel their artwork has into Kodi.
        """
        return self._get('/api/v1/scraper/movies/details?id={0}'.format(media_item_id),
                          'get_movie_details({0})'.format(media_item_id))

    def search_show(self, title: str, year=None):
        """GET /api/v1/scraper/tv/search?title=&year= -- same resolve-or-create
        pattern as search_movie(), for the show itself only."""
        if not self._base_url or not self._api_key:
            log.warning('Chronicle URL or API key not configured — search skipped')
            return None

        url = '{0}/api/v1/scraper/tv/search?title={1}'.format(
            self._base_url, urllib.parse.quote(title))
        if year:
            url += '&year={0}'.format(year)
        return self._get(url, 'search_show({0!r}, {1!r})'.format(title, year), full_url=True,
                          timeout=_SEARCH_TIMEOUT_SECONDS)

    def get_show_details(self, media_item_id: int):
        """GET /api/v1/scraper/tv/details?id= -- show-level details plus every
        season Chronicle already has for it."""
        return self._get('/api/v1/scraper/tv/details?id={0}'.format(media_item_id),
                          'get_show_details({0})'.format(media_item_id))

    def get_episode_list(self, show_id: int):
        """GET /api/v1/scraper/tv/episodes?showId= -- every episode Chronicle
        already has under this show, as a list of {id, season, episode, title}."""
        return self._get('/api/v1/scraper/tv/episodes?showId={0}'.format(show_id),
                          'get_episode_list({0})'.format(show_id))

    def get_episode_details(self, media_item_id: int):
        """GET /api/v1/scraper/tv/episode-details?id= -- full details for one episode."""
        return self._get('/api/v1/scraper/tv/episode-details?id={0}'.format(media_item_id),
                          'get_episode_details({0})'.format(media_item_id))

    def report_resolved_file(self, media_item_id: int, filename: str):
        """POST /api/v1/scraper/movies/{id}/resolved-file -- tells Chronicle the
        real filename this addon just discovered the slow way (title/year
        matching against Kodi's VideoLibrary or source browsing), so future
        requests can skip straight to it via KnownFileName instead of
        re-deriving it on every scrape. Best-effort: failures are logged and
        swallowed, same as every other client call here -- this is a nice-to-
        have that speeds up future scrapes, not something the current one
        depends on."""
        if not self._base_url or not self._api_key:
            return
        url = '{0}/api/v1/scraper/movies/{1}/resolved-file'.format(self._base_url, media_item_id)
        data = json.dumps({'fileName': filename}).encode('utf-8')
        req = self._build_request(url, data=data, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            status = call_with_timeout(_do, 10)
            if status == 200:
                log.info('report_resolved_file({0}, {1!r}): recorded'.format(media_item_id, filename))
            else:
                log.warning('report_resolved_file({0}, {1!r}): unexpected HTTP {2}'.format(
                            media_item_id, filename, status))
        except urllib.error.HTTPError as exc:
            log.warning('report_resolved_file({0}, {1!r}): Chronicle returned HTTP {2} ({3})'.format(
                        media_item_id, filename, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('report_resolved_file({0}, {1!r}): Chronicle not reachable ({2})'.format(
                        media_item_id, filename, exc))
        except Exception as exc:
            log.warning('report_resolved_file({0}, {1!r}): unexpected error: {2}'.format(
                        media_item_id, filename, exc))

    def contribute_metadata(self, media_item_id: int, source: str, metadata: dict):
        """POST /api/v1/media/{id}/metadata/{source} -- contributes fields
        harvested from a local source (e.g. a pre-existing NFO another tool
        wrote, about to be overwritten -- see lib/legacy_nfo.py) into
        Chronicle's own MetadataContributionService. Lands in its own named
        partition and only ever fills a field Chronicle doesn't already have
        from a real provider -- it can't clobber better data. Best-effort:
        failures are logged and swallowed, same as report_resolved_file() --
        this is a nice-to-have enrichment, not something the current scrape
        depends on."""
        if not self._base_url or not self._api_key or not metadata:
            return
        url = '{0}/api/v1/media/{1}/metadata/{2}'.format(
            self._base_url, media_item_id, urllib.parse.quote(source, safe=''))
        data = json.dumps({'metadata': metadata}).encode('utf-8')
        req = self._build_request(url, data=data, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status

        try:
            status = call_with_timeout(_do, 20)
            if status == 200:
                log.info('contribute_metadata({0}, {1!r}): {2} field(s) contributed'.format(
                         media_item_id, source, len(metadata)))
            else:
                log.warning('contribute_metadata({0}, {1!r}): unexpected HTTP {2}'.format(
                            media_item_id, source, status))
        except urllib.error.HTTPError as exc:
            log.warning('contribute_metadata({0}, {1!r}): Chronicle returned HTTP {2} ({3})'.format(
                        media_item_id, source, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('contribute_metadata({0}, {1!r}): Chronicle not reachable ({2})'.format(
                        media_item_id, source, exc))
        except Exception as exc:
            log.warning('contribute_metadata({0}, {1!r}): unexpected error: {2}'.format(
                        media_item_id, source, exc))

    def push_resume(self, media_item_id: int, progress_percent: float, timestamp_iso):
        """POST /api/v1/scrobble -- imports Kodi's own local resume position into
        Chronicle when it's newer than what Chronicle already has (see
        lib/progress_sync.py's resolve_progress_direction). Reuses the scrobble
        endpoint's own existing "most recent wins" guard server-side rather than
        duplicating it here. Best-effort: failures are logged and swallowed,
        same as report_resolved_file()/contribute_metadata()."""
        if not self._base_url or not self._api_key:
            return
        url = '{0}/api/v1/scrobble'.format(self._base_url)
        payload = {
            'mediaItemId':     media_item_id,
            'progressPercent': progress_percent,
            'deviceName':      'Chronicle Scraper (reconciled from local Kodi playback)',
        }
        if timestamp_iso:
            payload['timestamp'] = timestamp_iso
        data = json.dumps(payload).encode('utf-8')
        req = self._build_request(url, data=data, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            status = call_with_timeout(_do, 10)
            if status == 200:
                log.info('push_resume({0}, {1}): recorded'.format(media_item_id, progress_percent))
            else:
                log.warning('push_resume({0}, {1}): unexpected HTTP {2}'.format(
                            media_item_id, progress_percent, status))
        except urllib.error.HTTPError as exc:
            log.warning('push_resume({0}, {1}): Chronicle returned HTTP {2} ({3})'.format(
                        media_item_id, progress_percent, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('push_resume({0}, {1}): Chronicle not reachable ({2})'.format(
                        media_item_id, progress_percent, exc))
        except Exception as exc:
            log.warning('push_resume({0}, {1}): unexpected error: {2}'.format(
                        media_item_id, progress_percent, exc))

    def get_current_user(self):
        """GET /api/v1/users/me -- the identity behind this addon's own API key.
        Accepts the same X-Api-Key auth as every scraper endpoint (Chronicle's
        default authorization policy takes either a JWT or an API key -- see
        Chronicle.API's Program.cs), so this needs no separate auth path.

        Used only for the connection-status display in default.py's menu
        heading/Settings status field -- never anything scraping depends on.
        Short 5s timeout (vs. the 20s default elsewhere in this client):
        this runs every time the addon's menu opens, so a slow/unreachable
        server must not make the menu itself feel stuck; a plain "Connected"
        fallback (see default.py's _refresh_auth_status()) covers the miss.

        Returns {'username', 'displayName', ...} dict, or None on any
        failure (not configured, network error, revoked key)."""
        return self._get('/api/v1/users/me', 'get_current_user()', timeout=5)

    def test_connection(self):
        """GET /api/health — verify connectivity and API key.

        Returns a (success: bool, message: str) tuple.
        """
        if not self._base_url:
            return False, 'Chronicle URL is not configured.'

        url = '{0}/api/health'.format(self._base_url)
        req = self._build_request(url)

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            status = call_with_timeout(_do, 10)
            if status == 200:
                return True, ''
            return False, 'Unexpected HTTP {0}'.format(status)
        except urllib.error.HTTPError as exc:
            return False, 'HTTP {0}: {1}'.format(exc.code, exc.reason)
        except Exception as exc:
            return False, str(exc)

    # ── private ─────────────────────────────────────────────────────────────────

    def _get(self, path_or_url: str, log_label: str, full_url: bool = False, timeout: int = 20):
        """Shared GET helper: builds the request, parses the {success,data} envelope,
        logs and swallows any failure so callers just get None back."""
        if not self._base_url or not self._api_key:
            log.warning('Chronicle URL or API key not configured — {0} skipped'.format(log_label))
            return None

        url = path_or_url if full_url else '{0}{1}'.format(self._base_url, path_or_url)
        req = self._build_request(url)

        def _do():
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data')

        try:
            return call_with_timeout(_do, timeout)
        except urllib.error.HTTPError as exc:
            # Chronicle is reachable but returned an error status (e.g. 500 from an
            # unrelated request being canceled server-side, 401 from a stale API
            # key). Distinct from "not reachable at all" below -- worth telling
            # those apart when reading the log later.
            log.error('{0}: Chronicle returned HTTP {1} ({2})'.format(log_label, exc.code, exc.reason))
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.error('{0}: Chronicle not reachable at {1} ({2})'.format(log_label, self._base_url, exc))
            return None
        except Exception as exc:
            log.error('{0}: unexpected error: {1}'.format(log_label, exc))
            return None

    def _build_request(self, url: str, data=None, method: str = 'GET') -> urllib.request.Request:
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key':    self._api_key,
            'User-Agent':   _USER_AGENT,
        }
        return urllib.request.Request(url, data=data, headers=headers, method=method)
