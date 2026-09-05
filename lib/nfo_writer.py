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

Chronicle's own fields (title, cast, uniqueid, ratings, remote artwork, etc.)
are built server-side (see Chronicle.Plugin.Kodi.NFO's KodiNfoBuilder,
fetched here via ChronicleClient.fetch_movie_sidecar) -- this module's own
job is splicing in what only Kodi itself can know (streamdetails, local art
files) and writing the result to disk. See lib/nfo_common.py for those
Kodi-local building blocks, shared with tv_nfo_writer.py.
"""

import posixpath
import xml.etree.ElementTree as ET

import xbmcvfs

from lib.chronicle_client import ChronicleClient
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


def sync_movie_nfo(media_item_id, title, year, location=None, streamdetails=None):
    """Writes a fresh Kodi-native NFO for this movie, overwriting whatever was there before --
    see module docstring for why this deliberately doesn't try to detect/special-case a prior
    NFO's own authorship. Preserving a prior NFO's *data* is handled one layer up: see
    lib/legacy_nfo.py and lib/nfo_rebuild.py's delete step, plus scraper.py's get_details(),
    which merges any harvested data into Chronicle's own contribution before ever calling this.

    The document itself is fetched pre-built from Chronicle (GET .../movies/sidecar --see
    ChronicleClient.fetch_movie_sidecar and docs/plans/2026-09-02-kodi-nfo-plugin-design.md)
    rather than assembled here -- this function's own job is only to splice in the two things
    that plugin structurally cannot know (Kodi's own streamdetails probe, and local art files
    sitting on disk) and write the result to the right path.

    location, if given, is a pre-resolved (folder, video_basename) tuple -- pass this when the
    caller already looked the movie up for another reason (e.g. also syncing local art) so
    this doesn't repeat the same VideoLibrary/source-browsing lookup a second time.

    streamdetails, if given, is Kodi's own per-file technical info (see
    lib/movie_art_sync.py's get_streamdetails()) -- written into a
    <fileinfo><streamdetails> block Chronicle itself has no way to supply."""
    if location:
        folder, video_basename = location
    else:
        folder, video_basename, _full_filename, _via_fallback, _movie_id = find_movie_location(title, year)
    if not folder:
        return

    filename = (video_basename or 'movie') + '.nfo'
    dest = folder + filename

    xml_bytes_in = ChronicleClient().fetch_movie_sidecar(media_item_id)
    if not xml_bytes_in:
        log.warning('sync_movie_nfo: Chronicle returned no sidecar for media_item_id={0} -- '
                    'NFO not written this pass'.format(media_item_id))
        return
    try:
        root = ET.fromstring(xml_bytes_in)
    except ET.ParseError as exc:
        log.warning('sync_movie_nfo: sidecar from Chronicle for media_item_id={0} was not '
                    'parseable XML: {1}'.format(media_item_id, exc))
        return

    nfo_common.add_streamdetails(root, streamdetails)

    # A movie's local art may be named after either the real video file's
    # own basename or the containing folder's name -- movie_art_sync.py's
    # own sync_movie_art() writes using the folder name, while other tools
    # (tinyMediaManager and others) commonly use the video basename instead;
    # both are legitimate conventions Kodi recognises, so both are checked.
    folder_name = posixpath.basename(folder.rstrip('/'))
    prefixes = [p for p in (video_basename, folder_name) if p]
    local_art = nfo_common.list_local_art_prefixed(folder, prefixes, _LOCAL_ART_SUFFIXES)
    nfo_common.splice_local_art_fallback(root, local_art, _ART_TAGS)

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
