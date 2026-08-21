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

See lib/nfo_common.py for the XML-building blocks shared with
tv_nfo_writer.py (actors, uniqueid, ratings, streamdetails, art block, local
art file lookup) -- only the movie-specific field layout lives here.
"""

import posixpath
import xml.etree.ElementTree as ET

import xbmcvfs

from lib.logger import Logger
from lib.movie_art_sync import find_movie_location
from lib import nfo_common

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

# Local-file suffix -> the same art_type keys used in _ART_TAGS above, so a
# file found on disk slots into exactly the same NFO tag Chronicle's own
# remote candidates would use. Excludes 'poster'/'fanart' matches that are
# actually numbered fanart alternates -- those are recognised separately by
# nfo_common's own fanart-suffix pattern.
_LOCAL_ART_SUFFIXES = (
    ('poster', 'poster'),
    ('clearlogo', 'clearlogo'),
    ('banner', 'banner'),
    ('clearart', 'clearart'),
    ('discart', 'discart'),
    ('characterart', 'characterart'),
)


def _build_movie_nfo(details, streamdetails=None, local_art=None):
    root = ET.Element('movie')

    nfo_common.add_text(root, 'title', details.get('title'))
    nfo_common.add_text(root, 'originaltitle', details.get('title'))
    nfo_common.add_text(root, 'year', details.get('year'))
    nfo_common.add_text(root, 'plot', details.get('overview'))
    nfo_common.add_text(root, 'tagline', details.get('tagline'))
    if details.get('runtimeMinutes'):
        nfo_common.add_text(root, 'runtime', details['runtimeMinutes'])
    nfo_common.add_text(root, 'mpaa', details.get('mpaa'))
    nfo_common.add_text(root, 'premiered', details.get('premiered'))
    nfo_common.add_text(root, 'country', details.get('country'))
    nfo_common.add_text(root, 'studio', details.get('studio'))

    for genre in details.get('genres') or []:
        nfo_common.add_text(root, 'genre', genre)
    for tag in details.get('tags') or []:
        nfo_common.add_text(root, 'tag', tag)

    collection = details.get('collection') or {}
    if collection.get('name'):
        set_el = ET.SubElement(root, 'set')
        nfo_common.add_text(set_el, 'name', collection['name'])
        nfo_common.add_text(set_el, 'overview', collection.get('overview'))

    nfo_common.add_directors_and_writers(root, details.get('crew'))
    nfo_common.add_actors(root, details.get('cast'))
    nfo_common.add_uniqueids(root, details.get('externalIds'))
    nfo_common.add_ratings(root, details.get('ratings'))
    nfo_common.build_art_block(root, details.get('artwork'), local_art, _ART_TAGS)

    if details.get('trailerUrl'):
        nfo_common.add_text(root, 'trailer', details['trailerUrl'])

    nfo_common.add_streamdetails(root, streamdetails)

    return root


def sync_movie_nfo(title, year, details, location=None, streamdetails=None):
    """Writes a fresh Kodi-native NFO for this movie from Chronicle's
    `details` dict (the same one ScraperController's /movies/details
    returns), overwriting whatever was there before -- see module docstring
    for why this deliberately doesn't try to detect/special-case a prior
    NFO's own authorship. Preserving a prior NFO's *data* is handled one
    layer up: see lib/legacy_nfo.py and lib/nfo_rebuild.py's delete step,
    plus scraper.py's get_details(), which merges any harvested data into
    `details` before ever calling this.

    location, if given, is a pre-resolved (folder, video_basename) tuple --
    pass this when the caller already looked the movie up for another reason
    (e.g. also syncing local art) so this doesn't repeat the same
    VideoLibrary/source-browsing lookup a second time.

    streamdetails, if given, is Kodi's own per-file technical info (see
    lib/movie_art_sync.py's get_streamdetails()) -- written into a
    <fileinfo><streamdetails> block Chronicle itself has no way to supply."""
    if location:
        folder, video_basename = location
    else:
        folder, video_basename, _full_filename, _via_fallback = find_movie_location(title, year)
    if not folder:
        return

    filename = (video_basename or 'movie') + '.nfo'
    dest = folder + filename

    # A movie's local art may be named after either the real video file's
    # own basename or the containing folder's name -- movie_art_sync.py's
    # own sync_movie_art() writes using the folder name, while other tools
    # (tinyMediaManager and others) commonly use the video basename instead;
    # both are legitimate conventions Kodi recognises, so both are checked.
    folder_name = posixpath.basename(folder.rstrip('/'))
    prefixes = [p for p in (video_basename, folder_name) if p]
    local_art = nfo_common.list_local_art_prefixed(folder, prefixes, _LOCAL_ART_SUFFIXES)

    root = _build_movie_nfo(details, streamdetails=streamdetails, local_art=local_art)
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
