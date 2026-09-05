# -*- coding: utf-8 -*-
"""Shared XML-building blocks for Kodi-native NFOs -- used by nfo_writer.py
(movies) and tv_nfo_writer.py (TV shows/episodes).

Chronicle's own data (actors, directors/writers, uniqueid, ratings, and
Chronicle's own remote artwork candidates) is no longer built here -- as of
docs/plans/2026-09-02-kodi-nfo-plugin-design.md, that logic moved server-side
into Chronicle.Plugin.Kodi.NFO's KodiNfoBuilder, which both writers now fetch
pre-built XML from (see ChronicleClient.fetch_movie_sidecar/fetch_show_sidecar/
fetch_episode_sidecar) instead of assembling it in Python. What's left here is
strictly Kodi-local data neither Chronicle's server nor that plugin can ever
have: Kodi's own per-file technical probe (add_streamdetails) and local art
files already sitting on disk (list_local_art_prefixed/list_local_art_plain,
spliced in via splice_local_art_fallback) -- see that design doc's "Not
solved here" section for why local-art discovery specifically stays
client-side.
"""

import re
import xml.etree.ElementTree as ET

from lib.movie_art_sync import listdir_with_timeout

_LOCAL_ART_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tbn', '.bmp', '.gif')
_FANART_SUFFIX_RE = re.compile(r'^fanart\d*\.')


def add_text(parent, tag, text):
    if text is None or text == '':
        return None
    el = ET.SubElement(parent, tag)
    el.text = str(text)
    return el


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


def splice_local_art_fallback(root, local_art, art_tags):
    """Fills whatever art slot the sidecar Chronicle already built left empty (it only ever
    writes a slot it has a real remote candidate for), from local image files sitting next to
    the item -- list_local_art_prefixed()/list_local_art_plain() below. Only ever ADDS to the
    <art> element the fetched sidecar came with (creating one if the sidecar had none at all);
    never touches or reorders anything Chronicle's own data already supplied there. See
    docs/plans/2026-09-02-kodi-nfo-plugin-design.md's "Not solved here": local-art-file
    discovery is deliberately not server-side, since Chronicle's server has no way to browse
    the item's own folder on whatever machine is running this addon. local_art is a {art_type:
    [bare filenames]} dict, e.g. from list_local_art_prefixed()/list_local_art_plain() below;
    art_tags is the same (art_type, tag) pairs both nfo_writer.py and tv_nfo_writer.py use."""
    local_art = local_art or {}
    if not local_art:
        return

    art_el = root.find('art')
    if art_el is None:
        art_el = ET.SubElement(root, 'art')

    for art_type, tag in art_tags:
        local_files = local_art.get(art_type) or []
        if not local_files:
            continue
        if art_type == 'fanart':
            fanart_el = art_el.find('fanart')
            if fanart_el is None:
                fanart_el = ET.SubElement(art_el, 'fanart')
            for filename in local_files:
                add_text(fanart_el, 'thumb', filename)
        elif art_el.find(tag) is None:
            # Chronicle already supplied this slot (a <tag> element is already present) --
            # a local file never overrides Chronicle's own remote candidate, only fills a
            # genuine gap.
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
