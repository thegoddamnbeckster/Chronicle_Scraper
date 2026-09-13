# -*- coding: utf-8 -*-
"""Parses whatever a pre-existing local show/episode NFO already contains (e.g. one written by
tinyMediaManager, or any other Kodi-schema-compliant tool) into Chronicle's own canonical field
names -- used by python/tvshow_scraper.py's nfo_url() to identify an already-organized show or
episode straight from its own existing NFO, without Chronicle ever having to guess a folder/
filename match. Nothing here depends on Chronicle Scraper's own (now-removed) local NFO writing
-- an NFO's origin doesn't matter, only its Kodi-schema shape does.

This module used to also stash a local NFO's contents before Chronicle Scraper's own now-removed
"delete local NFO, force a re-scrape" rebuild action deleted it, so the data could be folded back
in on the next scrape. That whole flow (save_stash/load_and_clear_stash, and the movie-schema
parse_legacy_nfo() parser this TV addon never actually used) was removed 2026-09-13 along with
the rebuild feature itself -- see git history if you need the old rationale.

Two parsers, one per NFO root this addon's nfo_url() can receive -- <tvshow>, <episodedetails> --
sharing the actor/director-writer/uniqueid/ratings block parsers below, since those blocks are
identical across both schemas; only the top-level field list differs per type.
"""

import xml.etree.ElementTree as ET

from lib.logger import Logger

log = Logger('legacy_nfo')

_UNIQUEID_TYPES = ('imdb', 'tmdb', 'tvdb', 'trakt')


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


def parse_legacy_tvshow_nfo(xml_bytes):
    """Parses a Kodi-native tvshow.nfo into a dict using Chronicle's own
    canonical field names (the same ones ScraperController's /tv/details
    returns). Returns None if the bytes aren't parseable XML at all, or {} if parseable but not
    a <tvshow> root, or if parsing succeeded but nothing recognised was found."""
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
    parse_legacy_tvshow_nfo() above, but for an <episodedetails> root."""
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
