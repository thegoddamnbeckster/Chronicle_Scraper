# -*- coding: utf-8 -*-
"""Shared XML-building blocks for Kodi-native NFOs -- used by nfo_writer.py
(movies) and tv_nfo_writer.py (TV shows/episodes). Everything here is
genuinely identical between the two (actors, uniqueid, ratings,
streamdetails, and the local-art-file lookup patterns), factored out once
rather than kept as two independently-drifting copies.
"""

import re
import xml.etree.ElementTree as ET

from lib.movie_art_sync import listdir_with_timeout

UNIQUEID_TYPES = ('imdb', 'tmdb', 'tvdb', 'trakt')

_LOCAL_ART_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tbn', '.bmp', '.gif')
_FANART_SUFFIX_RE = re.compile(r'^fanart\d*\.')


def add_text(parent, tag, text):
    if text is None or text == '':
        return None
    el = ET.SubElement(parent, tag)
    el.text = str(text)
    return el


def add_actors(root, cast):
    for i, actor in enumerate(cast or []):
        actor_el = ET.SubElement(root, 'actor')
        if isinstance(actor, dict):
            add_text(actor_el, 'name', actor.get('name'))
            add_text(actor_el, 'role', actor.get('role'))
            # Chronicle's own resolved headshot for this person (docs/plans/2026-08-28-
            # people-section-design.md Section 7) -- null until Chronicle has actually
            # resolved a photo for them, in which case add_text's own None/'' check
            # skips the tag entirely, same as every other optional field here. Kodi's
            # actor NFO schema already supports <thumb>, just never had anything to put
            # in it before this.
            add_text(actor_el, 'thumb', actor.get('thumbUrl'))
        else:
            add_text(actor_el, 'name', actor)
        add_text(actor_el, 'order', i)


def add_directors_and_writers(root, crew):
    """Kodi's NFO only has dedicated person-tags for director and writer
    ("credits") -- every other job title (producer, composer, etc.) has no
    NFO-equivalent tag, so it isn't written even though Chronicle's own API
    keeps every crew credit it was given."""
    for member in crew or []:
        if not isinstance(member, dict):
            continue
        job = (member.get('job') or '').lower()
        if job == 'director':
            add_text(root, 'director', member.get('name'))
        elif job in ('writer', 'screenplay', 'story', 'teleplay'):
            add_text(root, 'credits', member.get('name'))


def add_uniqueids(root, external_ids):
    for id_type in UNIQUEID_TYPES:
        value = (external_ids or {}).get(id_type)
        if not value:
            continue
        uid_el = ET.SubElement(root, 'uniqueid', {'type': id_type})
        if id_type == 'imdb':
            uid_el.set('default', 'true')
        uid_el.text = str(value)


def add_ratings(root, ratings):
    if not ratings:
        return
    ratings_el = ET.SubElement(root, 'ratings')
    for source, rating in ratings.items():
        rating_el = ET.SubElement(ratings_el, 'rating', {'name': source, 'max': '10'})
        add_text(rating_el, 'value', rating.get('rating'))
        add_text(rating_el, 'votes', rating.get('votes') or 0)


def add_streamdetails(root, streamdetails):
    """Adds Kodi's own <fileinfo><streamdetails> block (video/audio/subtitle
    codec, resolution, HDR type, channel counts, track languages) -- data
    only Kodi itself has, from actually having opened and probed the real
    file. streamdetails is the dict lib/movie_art_sync.py's
    get_streamdetails() (or lib/tvshow_location.py's get_episode())
    returns, or None/empty when Kodi hasn't probed this file yet -- in which
    case this simply adds nothing, exactly as if the caller had never
    asked."""
    if not streamdetails:
        return
    video = streamdetails.get('video') or []
    audio = streamdetails.get('audio') or []
    subtitle = streamdetails.get('subtitle') or []
    if not (video or audio or subtitle):
        return

    fileinfo_el = ET.SubElement(root, 'fileinfo')
    sd_el = ET.SubElement(fileinfo_el, 'streamdetails')

    for v in video:
        v_el = ET.SubElement(sd_el, 'video')
        add_text(v_el, 'codec', v.get('codec'))
        add_text(v_el, 'aspect', v.get('aspect'))
        add_text(v_el, 'width', v.get('width'))
        add_text(v_el, 'height', v.get('height'))
        if v.get('duration'):
            add_text(v_el, 'durationinseconds', v['duration'])
        add_text(v_el, 'stereomode', v.get('stereomode'))
        add_text(v_el, 'hdrtype', v.get('hdrtype'))

    for a in audio:
        a_el = ET.SubElement(sd_el, 'audio')
        add_text(a_el, 'codec', a.get('codec'))
        add_text(a_el, 'language', a.get('language'))
        add_text(a_el, 'channels', a.get('channels'))

    for s in subtitle:
        s_el = ET.SubElement(sd_el, 'subtitle')
        add_text(s_el, 'language', s.get('language'))


def build_art_block(root, artwork, local_art, art_tags):
    """Builds the <art> element shared by movie and TV-show NFOs: Chronicle's
    own top-ranked remote candidate per slot, falling back to a local file
    only when Chronicle has nothing for that slot at all, plus every local
    fanart alternate appended after Chronicle's own fanart candidates (see
    art_tags -- the same (art_type, tag) pairs both nfo_writer.py and
    tv_nfo_writer.py use). Removes the <art> element again if it ended up
    empty. local_art is a {art_type: [bare filenames]} dict, e.g. from
    list_local_art_prefixed()/list_local_art_plain() below."""
    artwork = artwork or {}
    local_art = local_art or {}
    art_el = ET.SubElement(root, 'art')
    for art_type, tag in art_tags:
        candidates = artwork.get(art_type)
        local_files = local_art.get(art_type) or []
        if art_type == 'fanart':
            if not candidates and not local_files:
                continue
            fanart_el = ET.SubElement(art_el, 'fanart')
            for candidate in candidates or []:
                add_text(fanart_el, 'thumb', candidate['url'])
            for filename in local_files:
                add_text(fanart_el, 'thumb', filename)
        else:
            if candidates:
                add_text(art_el, tag, candidates[0]['url'])
            elif local_files:
                add_text(art_el, tag, local_files[0])
    if len(art_el) == 0:
        root.remove(art_el)


def _classify_local_art_suffix(suffix, art_suffixes):
    if _FANART_SUFFIX_RE.match(suffix):
        return 'fanart'
    for local_key, art_type in art_suffixes:
        if suffix.startswith(local_key + '.'):
            return art_type
    return None


def list_local_art_prefixed(folder, prefixes, art_suffixes):
    """Every image file in folder whose name is "<one of prefixes>-<art
    suffix>", classified by art_suffixes (a list of (local_key, art_type)
    pairs, e.g. nfo_writer.py's own poster/banner/clearlogo/etc. mapping).
    This is the movie/movie-set local-art convention -- a file named after
    the video's own basename or the containing folder's name, a hyphen,
    then the art type ("X-Men (2000)-poster.jpg"). Returns {art_type:
    [bare filenames]}; 'fanart' may have several (the base file plus every
    numbered alternate found), every other type has at most one."""
    _dirs, files = listdir_with_timeout(folder)
    if not files:
        return {}

    prefix_set = {p.lower() for p in prefixes if p}
    if not prefix_set:
        return {}

    found = {}
    for name in files:
        lower = name.lower()
        if not lower.endswith(_LOCAL_ART_EXTENSIONS):
            continue
        matched_prefix = next((p for p in prefix_set if lower.startswith(p + '-')), None)
        if matched_prefix is None:
            continue
        suffix = lower[len(matched_prefix) + 1:]
        art_type = _classify_local_art_suffix(suffix, art_suffixes)
        if art_type:
            found.setdefault(art_type, []).append(name)

    return found


def list_local_art_plain(folder, art_suffixes):
    """Every image file in folder named exactly "<art type>.<ext>" with no
    prefix at all -- the convention Kodi uses for a TV show's own root
    folder (and the same one collection_sync.py already uses for the
    dedicated Movie Set Information folder): plain "poster.jpg",
    "fanart.jpg", "banner.jpg", etc. Returns {art_type: [bare filenames]},
    same shape as list_local_art_prefixed()."""
    _dirs, files = listdir_with_timeout(folder)
    if not files:
        return {}

    found = {}
    for name in files:
        lower = name.lower()
        if not lower.endswith(_LOCAL_ART_EXTENSIONS):
            continue
        art_type = _classify_local_art_suffix(lower, art_suffixes)
        if art_type:
            found.setdefault(art_type, []).append(name)

    return found
