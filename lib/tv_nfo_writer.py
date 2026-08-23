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


def _build_show_nfo(details, local_art=None, episode_guide=None):
    root = ET.Element('tvshow')

    # Kodi prefers a local NFO already sitting in the folder over calling this scraper live --
    # that's the whole reason this writer exists (see module docstring). But when Kodi reads
    # the NFO directly rather than invoking get_details(), the only way it learns HOW to fetch
    # this show's episode list from Chronicle again (for new episodes added later) is this tag.
    # Without it, Kodi logs "no episode guide or we are using the local scraper" for every
    # episode and never calls getepisodelist() at all -- confirmed live. Must match the exact
    # string tvshow_scraper.py's own vtag.setEpisodeGuide() call uses for the live-scrape path,
    # so both paths resolve to the same lookup.
    nfo_common.add_text(root, 'episodeguide', episode_guide)

    nfo_common.add_text(root, 'title', details.get('title'))
    nfo_common.add_text(root, 'showtitle', details.get('title'))
    nfo_common.add_text(root, 'year', details.get('year'))
    nfo_common.add_text(root, 'premiered', details.get('premiered'))
    nfo_common.add_text(root, 'plot', details.get('overview'))
    nfo_common.add_text(root, 'mpaa', details.get('mpaa'))
    nfo_common.add_text(root, 'country', details.get('country'))
    nfo_common.add_text(root, 'studio', details.get('studio'))
    nfo_common.add_text(root, 'status', details.get('status'))
    if details.get('runtimeMinutes'):
        nfo_common.add_text(root, 'runtime', details['runtimeMinutes'])

    for genre in details.get('genres') or []:
        nfo_common.add_text(root, 'genre', genre)
    for tag in details.get('tags') or []:
        nfo_common.add_text(root, 'tag', tag)

    for season in details.get('seasons') or []:
        number = season.get('number')
        name = season.get('name')
        if number is not None and name:
            season_el = ET.SubElement(root, 'namedseason', {'number': str(number)})
            season_el.text = name

    nfo_common.add_actors(root, details.get('cast'))
    nfo_common.add_uniqueids(root, details.get('externalIds'))
    nfo_common.add_ratings(root, details.get('ratings'))
    nfo_common.build_art_block(root, details.get('artwork'), local_art, _SHOW_ART_TAGS)

    return root


def sync_show_nfo(title, year, details, location=None, episode_guide=None):
    """Writes tvshow.nfo for this show from Chronicle's `details` dict (the
    same one ScraperController's /tv/details returns).

    location, if given, is a pre-resolved (folder, tvshowid) tuple -- pass
    this when the caller already looked the show up for another reason so
    this doesn't repeat the same VideoLibrary/source-browsing lookup a
    second time.

    episode_guide should be the exact same lookup string the caller passes
    to vtag.setEpisodeGuide() during a live get_details() -- see
    _build_show_nfo()'s own comment for why this has to be written into the
    file, not just set on the live VideoInfoTag."""
    if location:
        folder, _tvshowid = location
    else:
        folder, _tvshowid = find_show_location(title, year)
    if not folder:
        return

    dest = folder + 'tvshow.nfo'
    local_art = nfo_common.list_local_art_plain(folder, _SHOW_LOCAL_ART_SUFFIXES)

    root = _build_show_nfo(details, local_art=local_art, episode_guide=episode_guide)
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


def _build_episode_nfo(details, streamdetails=None, local_art=None):
    root = ET.Element('episodedetails')

    nfo_common.add_text(root, 'title', details.get('title'))
    nfo_common.add_text(root, 'season', details.get('season'))
    nfo_common.add_text(root, 'episode', details.get('episode'))
    nfo_common.add_text(root, 'plot', details.get('overview'))
    nfo_common.add_text(root, 'aired', details.get('aired'))
    if details.get('runtimeMinutes'):
        nfo_common.add_text(root, 'runtime', details['runtimeMinutes'])

    nfo_common.add_directors_and_writers(root, details.get('crew'))
    nfo_common.add_actors(root, details.get('cast'))
    nfo_common.add_uniqueids(root, details.get('externalIds'))
    nfo_common.add_ratings(root, details.get('ratings'))

    # A full {art_type: [{'url': ...}, ...]} candidate-list dict, same shape
    # movie/show artwork already uses -- ScraperController.CollectEpisodeArtwork
    # re-keys what it calls "poster" server-side to "thumb" specifically so
    # this lines up without any translation needed here.
    nfo_common.build_art_block(root, details.get('artwork'), local_art, _EPISODE_ART_TAGS)

    nfo_common.add_streamdetails(root, streamdetails)

    return root


def sync_episode_nfo(details, folder, video_basename, streamdetails=None):
    """Writes this episode's own NFO from Chronicle's `details` dict (the
    same one ScraperController's /tv/episode-details returns), plus
    streamdetails (see lib/tvshow_location.py's get_episode()).

    folder+video_basename are the episode's OWN file's folder/basename --
    unlike movies/shows, an episode NFO always sits right next to its own
    video file, named to match it exactly (Kodi's episode-NFO convention
    has no bare fallback name the way movie.nfo/tvshow.nfo have)."""
    if not folder or not video_basename:
        return

    dest = folder + video_basename + '.nfo'
    local_art = nfo_common.list_local_art_prefixed(folder, [video_basename], _EPISODE_LOCAL_ART_SUFFIXES)

    root = _build_episode_nfo(details, streamdetails=streamdetails, local_art=local_art)
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
