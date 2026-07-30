# -*- coding: utf-8 -*-
"""Writes a Kodi-native movie NFO file into the movie's own folder from
Chronicle's data -- an addon setting, defaulted on, since Kodi's own
NFO-first behaviour otherwise works against Chronicle rather than for it.

Why this exists: confirmed directly (kodi.log, plus a live JSON-RPC
VideoLibrary.RefreshMovie test against X-Men) that Kodi checks for an
existing local NFO before ever invoking this scraper's find/getdetails/
getartwork at all -- there is no settings toggle for this (re-confirmed
against all 318 Settings API entries, same result as movie_art_sync.py's
own "prefer online artwork" search). Any movie tinyMediaManager (or any
other tool) has already written a full NFO for therefore never reaches
Chronicle_Scraper again on subsequent scans, no matter how many times Kodi
rescans the library -- and since TMM only writes an NFO when explicitly told
to, a movie scraped once and never re-told keeps whatever TMM wrote forever,
even after Chronicle's own data changes.

Rather than fight Kodi's NFO-first behaviour, this embraces it: every time
Chronicle_Scraper DOES get invoked -- which still happens for any movie
without a local NFO yet, e.g. a brand-new addition -- it also writes a fresh
Kodi-native NFO using Chronicle's current data. That NFO then becomes what
Kodi keeps using on every future scan, until Chronicle_Scraper overwrites it
again the next time it runs, keeping the two in sync instead of a stale
local file silently winning forever with no way back in.

Named to match the real video file's own basename (e.g. "X-Men (2000).nfo"
next to "X-Men (2000).mkv") whenever that's known, rather than the generic
"movie.nfo" -- confirmed against Kodi's own NFO documentation that a
<video-name>.nfo takes precedence over a same-folder "movie.nfo" if both
exist, so naming it "movie.nfo" here would NOT actually override a
video-name-matching NFO tinyMediaManager (or anything else) already left
behind. Falls back to "movie.nfo" only when the real video filename can't be
determined (find_movie_location() couldn't resolve it via VideoLibrary or
source-browsing) -- still Kodi-valid, just lower precedence if some other
tool's NFO is still sitting there under the real video name.

Deliberately does NOT try to detect or special-case a TMM-authored NFO --
treats any existing local NFO the same way (an opportunity to refresh it
with Chronicle's own data), consistent with movie_art_sync.py's own decision
to always overwrite rather than preserve.
"""

import xml.etree.ElementTree as ET

import xbmcvfs

from lib.logger import Logger
from lib.movie_art_sync import find_movie_location

log = Logger('nfo_writer')

_ART_TAGS = (
    ('poster', 'poster'),
    ('fanart', 'fanart'),
    ('clearlogo', 'clearlogo'),
    ('banner', 'banner'),
    ('clearart', 'clearart'),
    ('discart', 'discart'),
    ('characterart', 'characterart'),
)

_UNIQUEID_TYPES = ('imdb', 'tmdb', 'tvdb', 'trakt')


def _add_text(parent, tag, text):
    if text is None or text == '':
        return None
    el = ET.SubElement(parent, tag)
    el.text = str(text)
    return el


def _build_movie_nfo(details):
    root = ET.Element('movie')

    _add_text(root, 'title', details.get('title'))
    _add_text(root, 'originaltitle', details.get('title'))
    _add_text(root, 'year', details.get('year'))
    _add_text(root, 'plot', details.get('overview'))
    _add_text(root, 'tagline', details.get('tagline'))
    if details.get('runtimeMinutes'):
        _add_text(root, 'runtime', details['runtimeMinutes'])
    _add_text(root, 'mpaa', details.get('mpaa'))
    _add_text(root, 'premiered', details.get('premiered'))
    _add_text(root, 'country', details.get('country'))
    _add_text(root, 'studio', details.get('studio'))

    for genre in details.get('genres') or []:
        _add_text(root, 'genre', genre)
    for tag in details.get('tags') or []:
        _add_text(root, 'tag', tag)

    collection = details.get('collection') or {}
    if collection.get('name'):
        set_el = ET.SubElement(root, 'set')
        _add_text(set_el, 'name', collection['name'])
        _add_text(set_el, 'overview', collection.get('overview'))

    for director in details.get('directors') or []:
        _add_text(root, 'director', director)

    for i, actor in enumerate(details.get('cast') or []):
        actor_el = ET.SubElement(root, 'actor')
        _add_text(actor_el, 'name', actor)
        _add_text(actor_el, 'order', i)

    external_ids = details.get('externalIds') or {}
    for id_type in _UNIQUEID_TYPES:
        value = external_ids.get(id_type)
        if not value:
            continue
        uid_el = ET.SubElement(root, 'uniqueid', {'type': id_type})
        if id_type == 'imdb':
            uid_el.set('default', 'true')
        uid_el.text = str(value)

    ratings = details.get('ratings') or {}
    if ratings:
        ratings_el = ET.SubElement(root, 'ratings')
        for source, rating in ratings.items():
            rating_el = ET.SubElement(ratings_el, 'rating', {'name': source, 'max': '10'})
            _add_text(rating_el, 'value', rating.get('rating'))
            _add_text(rating_el, 'votes', rating.get('votes') or 0)

    artwork = details.get('artwork') or {}
    art_el = ET.SubElement(root, 'art')
    for art_type, tag in _ART_TAGS:
        candidates = artwork.get(art_type)
        if candidates:
            _add_text(art_el, tag, candidates[0]['url'])
    if len(art_el) == 0:
        root.remove(art_el)

    if details.get('trailerUrl'):
        _add_text(root, 'trailer', details['trailerUrl'])

    return root


def sync_movie_nfo(title, year, details, location=None):
    """Writes a fresh Kodi-native NFO for this movie from Chronicle's
    `details` dict (the same one ScraperController's /movies/details
    returns), overwriting whatever was there before -- see module docstring
    for why this deliberately doesn't try to preserve an existing NFO.

    location, if given, is a pre-resolved (folder, video_basename) tuple from
    find_movie_location() -- pass this when the caller already looked the
    movie up for another reason (e.g. also syncing local art) so this
    doesn't repeat the same source-browsing/listdir work a second time."""
    folder, video_basename = location if location else find_movie_location(title, year)
    if not folder:
        return

    filename = (video_basename or 'movie') + '.nfo'
    dest = folder + filename

    root = _build_movie_nfo(details)
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding='utf-8')

    try:
        f = xbmcvfs.File(dest, 'w')
        try:
            f.write(bytearray(xml_bytes))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write NFO {0}: {1}".format(dest, exc))
        return

    log.info('Wrote NFO for "{0}" ({1}) from Chronicle to {2}'.format(title, year, dest))
