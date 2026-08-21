# -*- coding: utf-8 -*-
"""Harvests whatever a pre-existing local movie/show/episode NFO already
contains, and stashes it in the addon's own per-Kodi-instance profile
directory so it can be recovered by the NEXT scrape of the same item --
specifically for nfo_rebuild.py's "delete local NFO, force a re-scrape"
action, which would otherwise silently throw away anything a different
tool (e.g. tinyMediaManager) had already written that Chronicle itself
doesn't have.

Why a stash file instead of holding this in memory: nfo_rebuild.py's delete
step and the later scrape that actually rewrites the NFO are two completely
separate invocations of this addon's Python interpreter -- Kodi's own C++
core launches a fresh script process per scraper action (find/getdetails),
sometime after a refresh is issued, with no channel back to whatever issued
it. Nothing survives in memory across that gap; only disk does.

Stashed under special://profile/, not the item's own folder -- xbmcvfs
resolves special://profile/ to THIS Kodi instance's own profile directory
(Vision's own, the Shield's own, whichever is running), so the stash always
lands somewhere this same instance can read back a moment later regardless
of which physical box or install this is, with no path assumptions of any
kind. It never needs to be portable BETWEEN instances -- only within the one
that deleted the NFO and will shortly re-scrape the same item.

One-shot by design: load_and_clear_stash() deletes the stash entry the
moment it's consumed, so a legacy NFO's data is folded into Chronicle (and
the freshly-written NFO) exactly once, not re-applied forever as a
permanent shadow source that could keep overriding real Chronicle data
after the user fixes it there.

Three parsers, one per NFO root Kodi recognises for video content this
addon writes -- <movie>, <tvshow>, <episodedetails> -- sharing the actor/
director-writer/uniqueid/ratings block parsers below, since those four
blocks are identical across all three schemas; only the top-level field
list differs per type.
"""

import json
import re
import xml.etree.ElementTree as ET

import xbmcaddon
import xbmcvfs

from lib.logger import Logger

log = Logger('legacy_nfo')

ADDON = xbmcaddon.Addon()

_STASH_DIR = 'special://profile/addon_data/{0}/legacy_nfo_stash/'.format(ADDON.getAddonInfo('id'))

_UNIQUEID_TYPES = ('imdb', 'tmdb', 'tvdb', 'trakt')

_SAFE_KEY_RE = re.compile(r'[^A-Za-z0-9._-]+')


def _text_of(root, tag):
    el = root.find(tag)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _parse_text_fields(root, field_map, data):
    """field_map is [(xml_tag, chronicle_key), ...] for plain string fields."""
    for tag, key in field_map:
        value = _text_of(root, tag)
        if value:
            data[key] = value


def _parse_cast(root):
    cast = []
    for actor_el in root.findall('actor'):
        name = actor_el.findtext('name')
        if not name or not name.strip():
            continue
        role = actor_el.findtext('role')
        cast.append({'name': name.strip(), 'role': role.strip() if role else None})
    return cast


def _parse_crew(root):
    """<director>/<credits> (writer) -- the only two job titles Kodi's NFO
    schema has dedicated tags for, on movie/episode NFOs alike."""
    crew = []
    for director_el in root.findall('director'):
        if director_el.text and director_el.text.strip():
            crew.append({'name': director_el.text.strip(), 'job': 'Director'})
    for credits_el in root.findall('credits'):
        if credits_el.text and credits_el.text.strip():
            crew.append({'name': credits_el.text.strip(), 'job': 'Writer'})
    return crew


def _parse_external_ids(root):
    external_ids = {}
    for uid_el in root.findall('uniqueid'):
        uid_type = (uid_el.get('type') or '').lower()
        if uid_type in _UNIQUEID_TYPES and uid_el.text and uid_el.text.strip():
            external_ids[uid_type] = uid_el.text.strip()
    return external_ids


def _parse_ratings(root):
    ratings = {}
    ratings_el = root.find('ratings')
    if ratings_el is not None:
        for rating_el in ratings_el.findall('rating'):
            source = rating_el.get('name')
            value = rating_el.findtext('value')
            votes = rating_el.findtext('votes')
            if not source or not value:
                continue
            try:
                ratings[source] = {'rating': float(value), 'votes': int(votes) if votes else 0}
            except ValueError:
                continue
    return ratings


def _parse_root(xml_bytes):
    """Shared entry point for all three parsers below. Returns the parsed
    ET root, or None if unparseable -- the caller checks the root's own
    .tag against what it expects and returns {} itself if it doesn't
    match."""
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.info("Existing NFO isn't parseable XML (possibly a bare-URL NFO) -- "
                 "nothing to harvest: {0}".format(exc))
        return None


def parse_legacy_nfo(xml_bytes):
    """Parses a Kodi-native movie NFO (as written by this addon,
    tinyMediaManager, or any other Kodi-schema-compliant tool) into a dict
    using Chronicle's own canonical field names (the same ones
    ScraperController's /movies/details returns), ready to merge into a
    `details` dict and/or contribute back to Chronicle.

    Returns None if the bytes aren't parseable XML at all, or {} if
    parseable but not a <movie> root (Kodi's other valid NFO form is a bare
    URL string, which this addon never writes and has nothing to extract),
    or if parsing succeeded but nothing recognised was found."""
    root = _parse_root(xml_bytes)
    if root is None:
        return None
    if root.tag != 'movie':
        return {}

    data = {}
    _parse_text_fields(root, (
        ('title', 'title'), ('plot', 'overview'), ('tagline', 'tagline'),
        ('mpaa', 'mpaa'), ('premiered', 'premiered'), ('country', 'country'),
        ('studio', 'studio'), ('trailer', 'trailerUrl'),
    ), data)

    year = _text_of(root, 'year')
    if year and year.isdigit():
        data['year'] = int(year)
    runtime = _text_of(root, 'runtime')
    if runtime and runtime.isdigit():
        data['runtimeMinutes'] = int(runtime)

    genres = [g.text.strip() for g in root.findall('genre') if g.text and g.text.strip()]
    if genres:
        data['genres'] = genres
    tags = [t.text.strip() for t in root.findall('tag') if t.text and t.text.strip()]
    if tags:
        data['tags'] = tags

    cast = _parse_cast(root)
    if cast:
        data['cast'] = cast
    crew = _parse_crew(root)
    if crew:
        data['crew'] = crew
    external_ids = _parse_external_ids(root)
    if external_ids:
        data['externalIds'] = external_ids
    ratings = _parse_ratings(root)
    if ratings:
        data['ratings'] = ratings

    collection_el = root.find('set')
    if collection_el is not None:
        name = collection_el.findtext('name')
        if name and name.strip():
            overview = collection_el.findtext('overview')
            data['collection'] = {
                'name': name.strip(),
                'overview': overview.strip() if overview and overview.strip() else None,
            }

    return data


def parse_legacy_tvshow_nfo(xml_bytes):
    """Parses a Kodi-native tvshow.nfo into a dict using Chronicle's own
    canonical field names (the same ones ScraperController's /tv/details
    returns). Same return conventions as parse_legacy_nfo() above, but for
    a <tvshow> root."""
    root = _parse_root(xml_bytes)
    if root is None:
        return None
    if root.tag != 'tvshow':
        return {}

    data = {}
    _parse_text_fields(root, (
        ('title', 'title'), ('plot', 'overview'), ('mpaa', 'mpaa'),
        ('premiered', 'premiered'), ('country', 'country'),
        ('studio', 'studio'), ('status', 'status'),
    ), data)

    year = _text_of(root, 'year')
    if year and year.isdigit():
        data['year'] = int(year)
    runtime = _text_of(root, 'runtime')
    if runtime and runtime.isdigit():
        data['runtimeMinutes'] = int(runtime)

    genres = [g.text.strip() for g in root.findall('genre') if g.text and g.text.strip()]
    if genres:
        data['genres'] = genres
    tags = [t.text.strip() for t in root.findall('tag') if t.text and t.text.strip()]
    if tags:
        data['tags'] = tags

    cast = _parse_cast(root)
    if cast:
        data['cast'] = cast
    external_ids = _parse_external_ids(root)
    if external_ids:
        data['externalIds'] = external_ids
    ratings = _parse_ratings(root)
    if ratings:
        data['ratings'] = ratings

    return data


def parse_legacy_episode_nfo(xml_bytes):
    """Parses a Kodi-native episode NFO into a dict using Chronicle's own
    canonical field names (the same ones ScraperController's
    /tv/episode-details returns). Same return conventions as
    parse_legacy_nfo() above, but for an <episodedetails> root."""
    root = _parse_root(xml_bytes)
    if root is None:
        return None
    if root.tag != 'episodedetails':
        return {}

    data = {}
    _parse_text_fields(root, (
        ('title', 'title'), ('plot', 'overview'), ('aired', 'aired'),
    ), data)

    season = _text_of(root, 'season')
    if season and season.isdigit():
        data['season'] = int(season)
    episode = _text_of(root, 'episode')
    if episode and episode.isdigit():
        data['episode'] = int(episode)
    runtime = _text_of(root, 'runtime')
    if runtime and runtime.isdigit():
        data['runtimeMinutes'] = int(runtime)

    cast = _parse_cast(root)
    if cast:
        data['cast'] = cast
    crew = _parse_crew(root)
    if crew:
        data['crew'] = crew
    external_ids = _parse_external_ids(root)
    if external_ids:
        data['externalIds'] = external_ids
    ratings = _parse_ratings(root)
    if ratings:
        data['ratings'] = ratings

    return data


def _stash_path(stash_key):
    safe = _SAFE_KEY_RE.sub('_', stash_key or 'unknown')
    return _STASH_DIR + safe + '.json'


def save_stash(stash_key, data):
    """Best-effort -- a failure here just means the next scrape won't find
    anything to harvest, same as if there had been nothing worth keeping."""
    if not stash_key or not data:
        return
    try:
        if not xbmcvfs.exists(_STASH_DIR):
            xbmcvfs.mkdirs(_STASH_DIR)
        f = xbmcvfs.File(_stash_path(stash_key), 'w')
        try:
            f.write(bytearray(json.dumps(data).encode('utf-8')))
        finally:
            f.close()
        log.info('legacy_nfo: stashed {0} field(s) for {1!r} before deleting its NFO'.format(
                 len(data), stash_key))
    except Exception as exc:
        log.warning("Couldn't stash legacy NFO data for {0!r}: {1}".format(stash_key, exc))


def load_and_clear_stash(stash_key):
    """Returns the stashed dict for stash_key and deletes it (one-shot -- see
    module docstring), or None if there's nothing stashed for it."""
    if not stash_key:
        return None
    path = _stash_path(stash_key)
    if not xbmcvfs.exists(path):
        return None

    data = None
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = bytes(f.readBytes())
        finally:
            f.close()
        data = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        log.warning("Couldn't read stashed legacy NFO data for {0!r}: {1}".format(stash_key, exc))

    try:
        xbmcvfs.delete(path)
    except Exception as exc:
        log.warning("Couldn't delete consumed stash file {0}: {1}".format(path, exc))

    return data or None
