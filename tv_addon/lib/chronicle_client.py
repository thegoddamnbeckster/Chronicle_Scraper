# -*- coding: utf-8 -*-
"""HTTP client for the Chronicle REST API.

Uses urllib from the Python standard library — no third-party packages
needed inside Kodi's Python environment.

Authentication: X-Api-Key header (same Chronicle API key format and
device-auth flow as Chronicle_Scrobbler).
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import xbmcaddon

from lib import timeutil
from lib.http_core import (
    call_with_retries, call_with_timeout, find_shared_chronicle_url as _find_shared_url, normalize_base_url, user_agent)
from lib import connection_health
from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('client')
health = connection_health.install(ADDON, log)

_USER_AGENT = user_agent(ADDON.getAddonInfo('id'))


def find_shared_chronicle_url():
    """Another installed Chronicle add-on's saved server address, to pre-fill the "Edit Connection" prompt (see http_core)."""
    return _find_shared_url(ADDON.getAddonInfo('id'), log)


# search_movie/search_show can trigger Chronicle's resolve-or-create path for a
# title it's never seen before -- which walks every configured metadata
# provider SEQUENTIALLY (each individually bounded up to 25s server-side), not
# in parallel. Confirmed directly (2026-07-30, a full-library rescan):
# 64 distinct titles hit the plain 20s default below and got nothing back even
# though Chronicle was still working -- not a hang, just legitimately slow
# multi-provider enrichment for a brand-new title. The everyday lookups
# (get_movie_details etc.) stay at the tighter default since those only ever
# read an item Chronicle has already resolved.
_SEARCH_TIMEOUT_SECONDS = 90


class ChronicleClient:
    """Talks to Chronicle's scraper-facing endpoints (api/v1/scraper/movies/*)."""

    def __init__(self):
        self._base_url = normalize_base_url(ADDON.getSetting('chronicle_url'))
        self._api_key  = ADDON.getSetting('api_key')

    def refresh_settings(self):
        """Re-read URL and API key from addon settings.

        Call this after a device-auth flow completes so the client picks up
        the newly saved API key without needing to be reconstructed.
        """
        self._base_url = normalize_base_url(ADDON.getSetting('chronicle_url'))
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
        if not health.not_configured(self._base_url, self._api_key):
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
        if not health.not_configured(self._base_url, self._api_key):
            log.warning('Chronicle URL or API key not configured — search skipped')
            return None

        url = '{0}/api/v1/scraper/tv/search?title={1}'.format(
            self._base_url, urllib.parse.quote(title))
        if year:
            url += '&year={0}'.format(year)
        return self._get(url, 'search_show({0!r}, {1!r})'.format(title, year), full_url=True,
                          timeout=_SEARCH_TIMEOUT_SECONDS)

    def resolve_show_by_external_id(self, source: str, external_id: str):
        """GET /api/v1/scraper/tv/resolve-by-external-id?source=&externalId= --
        the lookup getartwork() needs that find/getdetails never do, since Kodi
        passes the show's own default uniqueid (imdb) back as a bare string for
        that one action instead of this addon's own opaque lookup-string format. Never
        creates anything, unlike search_show() -- a getartwork call for a show
        Chronicle doesn't already know has nothing to create it FROM (no
        title, no year, just an id)."""
        if not health.not_configured(self._base_url, self._api_key):
            log.warning('Chronicle URL or API key not configured — resolve_show_by_external_id skipped')
            return None

        url = '{0}/api/v1/scraper/tv/resolve-by-external-id?source={1}&externalId={2}'.format(
            self._base_url, urllib.parse.quote(source), urllib.parse.quote(external_id))
        return self._get(url, 'resolve_show_by_external_id({0!r}, {1!r})'.format(source, external_id),
                          full_url=True, timeout=_SEARCH_TIMEOUT_SECONDS)

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

    def get_episode_details_by_file(self, file_name: str, season=None, episode=None):
        """GET /api/v1/scraper/tv/episode-details-by-file?fileName=&season=&episode= -- same
        shape as get_episode_details(), resolved purely from the video file's own basename
        instead of a known Chronicle id. Read-only: never creates anything. Returns None when
        there's no unambiguous match (nothing in Chronicle for this exact file, the filename is
        genuinely ambiguous, or season/episode was supplied and contradicts the matched item's
        own season/episode number -- the same "don't trust a possibly-stale filename record"
        guard the movie addon's get_movie_details_by_file uses via year) -- same as any other
        failure here, since a caller walking Kodi's own full inventory (full_sync_check.py) has
        nothing safer to do than skip that file. Always pass both when known."""
        url = '/api/v1/scraper/tv/episode-details-by-file?fileName={0}'.format(urllib.parse.quote(file_name))
        if season is not None:
            url += '&season={0}'.format(season)
        if episode is not None:
            url += '&episode={0}'.format(episode)
        return self._get(url, 'get_episode_details_by_file({0!r})'.format(file_name))

    def report_resolved_file(self, media_item_id: int, filename: str):
        """POST /api/v1/scraper/movies/{id}/resolved-file -- tells Chronicle the
        real filename this addon just discovered the slow way (title/year
        matching against Kodi's VideoLibrary or source browsing), so future
        requests can skip straight to it via KnownFileName instead of
        re-deriving it on every scrape. Best-effort: failures are logged and
        swallowed, same as every other client call here -- this is a nice-to-
        have that speeds up future scrapes, not something the current one
        depends on."""
        if not health.not_configured(self._base_url, self._api_key):
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

    def report_kodi_id(self, media_item_id: int, kind: str, kodi_id: int):
        """POST /api/v1/scraper/report-kodi-id -- tells Chronicle this device's own internal
        VideoLibrary id (tvshowid/episodeid) for a MediaItem, so Chronicle knows which
        Kodi library entry each item is on this device.
        A no-op server-side (not an error) when this device hasn't registered itself yet, e.g.
        remote control is off in Kodi's own Settings -- see lib/device_registration.py.
        Best-effort: failures are logged and swallowed, same as
        report_resolved_file()."""
        if not health.not_configured(self._base_url, self._api_key):
            return
        url = '{0}/api/v1/scraper/report-kodi-id'.format(self._base_url)
        data = json.dumps({'mediaItemId': media_item_id, 'kind': kind, 'kodiId': kodi_id}).encode('utf-8')
        req = self._build_request(url, data=data, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            status = call_with_timeout(_do, 10)
            if status != 200:
                log.warning('report_kodi_id({0}, {1!r}, {2}): unexpected HTTP {3}'.format(
                            media_item_id, kind, kodi_id, status))
        except urllib.error.HTTPError as exc:
            log.warning('report_kodi_id({0}, {1!r}, {2}): Chronicle returned HTTP {3} ({4})'.format(
                        media_item_id, kind, kodi_id, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('report_kodi_id({0}, {1!r}, {2}): Chronicle not reachable ({3})'.format(
                        media_item_id, kind, kodi_id, exc))
        except Exception as exc:
            log.warning('report_kodi_id({0}, {1!r}, {2}): unexpected error: {3}'.format(
                        media_item_id, kind, kodi_id, exc))

    def register_device(self, name: str, addon_name: str = '', addon_id: str = '', addon_version: str = '',
                        kodi_version: str = ''):
        """POST /api/v1/scraper/devices/kodi/register -- tells Chronicle this Kodi install exists and which
        add-on is saying so: the device's name, this add-on's name, id and version, and the Kodi build (the
        server notes the address the request came from). Each add-on has its own API key and so its own row;
        these fields make it obvious which is which. Chronicle never calls a device, so no address, port or
        login is sent. Best-effort: failures are logged and swallowed, same as every other client call here --
        registration is repeated at the next service start."""
        if not health.not_configured(self._base_url, self._api_key):
            return
        url = '{0}/api/v1/scraper/devices/kodi/register'.format(self._base_url)
        data = json.dumps({'name': name, 'addonName': addon_name, 'addonId': addon_id,
                           'addonVersion': addon_version, 'kodiVersion': kodi_version}).encode('utf-8')
        req = self._build_request(url, data=data, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        label = 'register_device({0!r}, {1!r} {2})'.format(name, addon_id, addon_version)
        try:
            status = call_with_timeout(_do, 10)
            if status == 200:
                log.info('{0}: registered with Chronicle'.format(label))
            else:
                log.warning('{0}: unexpected HTTP {1}'.format(label, status))
        except urllib.error.HTTPError as exc:
            log.warning('{0}: Chronicle returned HTTP {1} ({2})'.format(label, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('{0}: Chronicle not reachable ({1})'.format(label, exc))
        except Exception as exc:
            log.warning('{0}: unexpected error: {1}'.format(label, exc))

    def report_scan_active(self):
        """POST /api/v1/scraper/scan-active -- heartbeat telling Chronicle "a Kodi device is
        actively scanning right now" (see IKodiDeviceService.IsScanActiveAsync server-side). Called from
        service.py's own idle loop while Kodi's library scan OR this addon's own scraper-activity
        tail is still running -- the tail matters just as much as Kodi's own directory-walk
        phase, since that's where the actual per-item contention happens. No explicit "finished"
        call by design: the server-side flag simply expires a couple of minutes after the last
        heartbeat. Best-effort, same as report_kodi_id(): a failure here must never interrupt
        whatever scan/scrape is actually in progress."""
        if not health.not_configured(self._base_url, self._api_key):
            return
        url = '{0}/api/v1/scraper/scan-active'.format(self._base_url)
        req = self._build_request(url, data=b'{}', method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            call_with_timeout(_do, 10)
        except Exception as exc:
            log.warning('report_scan_active(): {0}'.format(exc))

    def push_watched(self, media_item_id: int, timestamp_iso):
        """POST /api/v1/scrobble -- imports Kodi's own local watched status into Chronicle
        when it's newer than what Chronicle already has (see lib/progress_sync.py's
        resolve_watched_direction). Sibling of push_resume, same reasoning: reuses the
        scrobble endpoint's own existing "most recent wins" guard server-side rather than
        duplicating it here. Best-effort: failures are logged and swallowed."""
        if not health.not_configured(self._base_url, self._api_key):
            return
        url = '{0}/api/v1/scrobble'.format(self._base_url)
        payload = {
            'mediaItemId':     media_item_id,
            'progressPercent': 100,
            'markedAsWatched': True,
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
                log.info('push_watched({0}): recorded'.format(media_item_id))
            else:
                log.warning('push_watched({0}): unexpected HTTP {1}'.format(media_item_id, status))
        except urllib.error.HTTPError as exc:
            log.warning('push_watched({0}): Chronicle returned HTTP {1} ({2})'.format(
                        media_item_id, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('push_watched({0}): Chronicle not reachable ({1})'.format(media_item_id, exc))
        except Exception as exc:
            log.warning('push_watched({0}): unexpected error: {1}'.format(media_item_id, exc))

    def reset_watch_progress(self, media_item_id: int):
        """POST /api/v1/library/by-media/{id}/reset-watch-progress -- resets this device's
        Chronicle user's watch status for one item back to never-watched. Used only by Library
        Repair's fabricated-watched pass (see lib/library_repair.py), which clears Kodi's own
        bad watched marks and must clear Chronicle's in the same run or reconciliation pushes
        them straight back. Returns True on success, False on any failure (unlike the
        best-effort push_* calls, the caller needs to know)."""
        if not health.not_configured(self._base_url, self._api_key):
            return False
        url = '{0}/api/v1/library/by-media/{1}/reset-watch-progress'.format(self._base_url, media_item_id)
        req = self._build_request(url, data=b'{}', method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            return call_with_timeout(_do, 10) == 200
        except urllib.error.HTTPError as exc:
            log.warning('reset_watch_progress({0}): Chronicle returned HTTP {1} ({2})'.format(
                        media_item_id, exc.code, exc.reason))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning('reset_watch_progress({0}): Chronicle not reachable ({1})'.format(media_item_id, exc))
        except Exception as exc:
            log.warning('reset_watch_progress({0}): unexpected error: {1}'.format(media_item_id, exc))
        return False

    def push_resume(self, media_item_id: int, progress_percent: float, timestamp_iso):
        """POST /api/v1/scrobble -- imports Kodi's own local resume position into
        Chronicle when it's newer than what Chronicle already has (see
        lib/progress_sync.py's resolve_progress_direction). Reuses the scrobble
        endpoint's own existing "most recent wins" guard server-side rather than
        duplicating it here. Best-effort: failures are logged and swallowed,
        same as report_resolved_file()."""
        if not health.not_configured(self._base_url, self._api_key):
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
        if not health.not_configured(self._base_url, self._api_key):
            log.warning('Chronicle URL or API key not configured — {0} skipped'.format(log_label))
            return None

        url = path_or_url if full_url else '{0}{1}'.format(self._base_url, path_or_url)
        req = self._build_request(url)

        def _do():
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data')

        try:
            return call_with_retries(_do, timeout, log_label, log)
        except urllib.error.HTTPError as exc:
            # Chronicle is reachable but returned an error status (e.g. 500 from an
            # unrelated request being canceled server-side, 401 from a stale API
            # key). Distinct from "not reachable at all" below -- worth telling
            # those apart when reading the log later.
            log.error('{0}: Chronicle returned HTTP {1} ({2})'.format(log_label, exc.code, exc.reason))
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.error('{0}: Chronicle not reachable at {1} after retrying ({2})'.format(
                      log_label, self._base_url, exc))
            return None
        except Exception as exc:
            log.error('{0}: unexpected error: {1}'.format(log_label, exc))
            return None

    def _build_request(self, url: str, data=None, method: str = 'GET') -> urllib.request.Request:
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key':    self._api_key,
            'User-Agent':   _USER_AGENT,
            # Which time zone this device is in: Chronicle answers in it and reads offset-less timestamps in it.
            timeutil.TIMEZONE_HEADER: timeutil.device_timezone(),
        }
        return urllib.request.Request(url, data=data, headers=headers, method=method)
