# -*- coding: utf-8 -*-
"""Writes Kodi-native tvshow.nfo and per-episode NFO files from Chronicle's
data -- the TV-side equivalent of nfo_writer.py (movies). Same rationale:
Kodi checks for a show/episode's own local NFO before ever invoking this
scraper, so Chronicle_Scraper writes its own fresh one every time it IS
invoked, keeping Kodi's NFO-first behaviour working for Chronicle rather
than against it. See nfo_writer.py's own module docstring for the fuller
explanation (identical mechanism, just for shows/episodes instead of
movies) and lib/nfo_common.py for the XML-building blocks shared between
the two writers.

Two NFOs, two different local conventions:
  tvshow.nfo    -- one per show, in the show's own root folder, always
                   named "tvshow.nfo" (Kodi has no per-show-name variant the
                   way movies have <video-name>.nfo). Local art there uses
                   Kodi's plain-filename convention ("poster.jpg", no
                   prefix) -- the same one collection_sync.py's dedicated
                   Movie Set Information folder uses, since a TV show's own
                   folder plays the analogous role for its own art.
  <episode>.nfo -- one per episode, next to the episode's own video file,
                   named to match its basename exactly (no bare fallback
                   name Kodi recognises for episodes the way "movie.nfo" or
                   "tvshow.nfo" work at the other two levels).
"""

import xml.etree.ElementTree as ET

import xbmcvfs

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger
from lib.tvshow_location import find_show_location
from lib import nfo_common

log = Logger('tv_nfo_writer')

_SHOW_ART_TAGS = (
    ('poster', 'poster'),
    ('fanart', 'fanart'),
    ('clearlogo', 'clearlogo'),
    ('banner', 'banner'),
    ('clearart', 'clearart'),
    ('characterart', 'characterart'),
)
_SHOW_LOCAL_ART_SUFFIXES = (
    ('poster', 'poster'),
    ('banner', 'banner'),
    ('clearlogo', 'clearlogo'),
    ('clearart', 'clearart'),
    ('characterart', 'characterart'),
)

_EPISODE_ART_TAGS = (('thumb', 'thumb'),)
_EPISODE_LOCAL_ART_SUFFIXES = (('thumb', 'thumb'),)


def sync_show_nfo(media_item_id, title, year, location=None):
    """Writes tvshow.nfo for this show, fetching the pre-built document from Chronicle (GET
    .../tv/sidecar -- see ChronicleClient.fetch_show_sidecar and
    docs/plans/2026-09-02-kodi-nfo-plugin-design.md) rather than assembling it here, then
    splicing in local-art-file fallback (the one thing Chronicle's server structurally cannot
    discover -- see nfo_common.splice_local_art_fallback's own doc).

    location, if given, is a pre-resolved (folder, tvshowid) tuple -- pass this when the caller
    already looked the show up for another reason so this doesn't repeat the same
    VideoLibrary/source-browsing lookup a second time."""
    if location:
        folder, _tvshowid = location
    else:
        folder, _tvshowid = find_show_location(title, year)
    if not folder:
        return

    dest = folder + 'tvshow.nfo'

    xml_bytes_in = ChronicleClient().fetch_show_sidecar(media_item_id)
    if not xml_bytes_in:
        log.warning('sync_show_nfo: Chronicle returned no sidecar for media_item_id={0} -- '
                    'NFO not written this pass'.format(media_item_id))
        return
    try:
        root = ET.fromstring(xml_bytes_in)
    except ET.ParseError as exc:
        log.warning('sync_show_nfo: sidecar from Chronicle for media_item_id={0} was not '
                    'parseable XML: {1}'.format(media_item_id, exc))
        return

    local_art = nfo_common.list_local_art_plain(folder, _SHOW_LOCAL_ART_SUFFIXES)
    nfo_common.splice_local_art_fallback(root, local_art, _SHOW_ART_TAGS)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding='utf-8')

    try:
        f = xbmcvfs.File(dest, 'w')
        try:
            f.write(bytearray(xml_bytes))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write show NFO {0}: {1}".format(dest, exc))
        return

    log.info('Wrote show NFO for "{0}" ({1}) from Chronicle to {2}'.format(title, year, dest))


def sync_episode_nfo(media_item_id, details, folder, video_basename, streamdetails=None):
    """Writes this episode's own NFO, fetching the pre-built document from Chronicle (GET
    .../tv/episode-sidecar -- see ChronicleClient.fetch_episode_sidecar and
    docs/plans/2026-09-02-kodi-nfo-plugin-design.md) rather than assembling it here, then
    splicing in the two things that endpoint structurally cannot supply: streamdetails (Kodi's
    own per-file technical probe, see lib/tvshow_location.py's get_episode()) and local-art-file
    fallback (see nfo_common.splice_local_art_fallback's own doc). `details` (the same dict
    ScraperController's /tv/episode-details returns) is used only for the log lines below, not
    to build the document itself.

    folder+video_basename are the episode's OWN file's folder/basename -- unlike movies/shows,
    an episode NFO always sits right next to its own video file, named to match it exactly
    (Kodi's episode-NFO convention has no bare fallback name the way movie.nfo/tvshow.nfo
    have)."""
    if not folder or not video_basename:
        # Logged (not a silent no-op) so a rebuild pass that never writes
        # this episode's NFO has a visible cause in kodi.log instead of
        # looking like a stall -- see tvshow_scraper.py's own
        # get_episode_details() logging for why folder/video_basename can
        # still be empty here (VideoLibrary lookup AND the pre-refresh path
        # cache both came up empty for this episode).
        log.warning('sync_episode_nfo: S{0}E{1} "{2}" -- no folder/video_basename resolved, '
                    'skipping NFO write'.format(
                    details.get('season'), details.get('episode'), details.get('title')))
        return

    dest = folder + video_basename + '.nfo'

    xml_bytes_in = ChronicleClient().fetch_episode_sidecar(media_item_id)
    if not xml_bytes_in:
        log.warning('sync_episode_nfo: Chronicle returned no sidecar for media_item_id={0} -- '
                    'NFO not written this pass'.format(media_item_id))
        return
    try:
        root = ET.fromstring(xml_bytes_in)
    except ET.ParseError as exc:
        log.warning('sync_episode_nfo: sidecar from Chronicle for media_item_id={0} was not '
                    'parseable XML: {1}'.format(media_item_id, exc))
        return

    nfo_common.add_streamdetails(root, streamdetails)
    local_art = nfo_common.list_local_art_prefixed(folder, [video_basename], _EPISODE_LOCAL_ART_SUFFIXES)
    nfo_common.splice_local_art_fallback(root, local_art, _EPISODE_ART_TAGS)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding='utf-8')

    try:
        f = xbmcvfs.File(dest, 'w')
        try:
            f.write(bytearray(xml_bytes))
        finally:
            f.close()
    except Exception as exc:
        log.warning("Couldn't write episode NFO {0}: {1}".format(dest, exc))
        return

    log.info('Wrote episode NFO for S{0}E{1} "{2}" from Chronicle to {3}'.format(
             details.get('season'), details.get('episode'), details.get('title'), dest))
