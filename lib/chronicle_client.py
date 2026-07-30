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

from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('client')

_USER_AGENT = 'Kodi/Chronicle-Scraper/1.0'

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

    def search_movie(self, title: str, year=None):
        """GET /api/v1/scraper/movies/search?title=&year=

        Chronicle resolves-or-creates the item server-side (through its own
        configured metadata providers) and returns exactly one candidate --
        there's nothing for this addon to disambiguate.

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
        return self._get(url, 'search_movie({0!r}, {1!r})'.format(title, year), full_url=True,
                          timeout=_SEARCH_TIMEOUT_SECONDS)

    def get_movie_details(self, media_item_id: int):
        """GET /api/v1/scraper/movies/details?id=

        Returns the full details dict -- title, overview, tagline, year,
        premiered, mpaa, country, studio, runtimeMinutes, genres, cast,
        directors, tags, ratings (per-source), trailerUrl, externalIds
        (imdb/tvdb/tmdb/trakt), artwork (per-arttype candidate lists) and
        collection (set name/overview/poster/backdrop) -- or None on failure.
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

    def test_connection(self):
        """GET /api/health — verify connectivity and API key.

        Returns a (success: bool, message: str) tuple.
        """
        if not self._base_url:
            return False, 'Chronicle URL is not configured.'

        url = '{0}/api/health'.format(self._base_url)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, ''
                return False, 'Unexpected HTTP {0}'.format(resp.status)
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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data')
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
