# -*- coding: utf-8 -*-
"""Keeps local artwork files for TV shows, seasons, and episodes in sync
with Chronicle's current pick -- the TV-side equivalent of
movie_art_sync.py's sync_movie_art(). Same reasoning applies here as there:
Kodi re-applies local art on its own internal schedule, independent of any
scraper call (movie_art_sync.py's own module docstring documents this
confirmed live, via kodi.log, for movies), so the local file has to already
agree with Chronicle's pick or Kodi will keep showing something stale. Uses
the same lib/art_sync_cache.py skip-if-unchanged optimization movies get,
so an unchanged file costs zero network I/O here too.

Three local conventions, one per level -- all already used for READING
local art into NFOs (see lib/nfo_common.py's list_local_art_plain/
list_local_art_prefixed and tv_nfo_writer.py's own callers); this module is
what WRITES those same files in the first place:

  show    -- plain filenames in the show's own root folder ("poster.jpg",
             "fanart.jpg"), the same convention collection_sync.py's Movie
             Set Information folder already uses, since a TV show's own
             folder plays the analogous role for its own art.
  season  -- ALSO in the show's own root folder (Kodi has no per-season
             subfolder convention), prefixed "seasonNN-" (zero-padded) or
             "season-specials-" for season 0.
  episode -- named after the episode's own video file, exactly like a
             movie's local art ("<video-basename>-thumb.jpg"), since an
             episode (like a movie) is identified by its own physical file,
             not a container folder.

Deliberately mirrors movie_art_sync.py's own scope, not more: only
poster+fanart for shows/seasons (the same two types _ART_FILES covers for
movies), only thumb for episodes -- Kodi has no "poster" concept for a
single episode, and ScraperController.CollectEpisodeArtwork already
re-keys what it calls "poster" server-side to "thumb" for exactly that
reason, so a "poster" key never actually arrives here for an episode.
"""

from lib import art_sync_cache
from lib.logger import Logger
from lib.remote_file import write_remote_file
from lib.tvshow_location import find_show_location

log = Logger('tv_art_sync')

_SHOW_ART_FILES = (
    ('poster', 'jpg'),
    ('fanart', 'jpg'),
)
_SEASON_ART_FILES = (
    ('poster', 'jpg'),
    ('fanart', 'jpg'),
)
_EPISODE_ART_FILES = (
    ('thumb', 'jpg'),
)


def sync_show_art(title, year, artwork, location=None):
    """Writes the show's own poster.jpg/fanart.jpg into its root folder from
    Chronicle's current pick -- except when art_sync_cache already confirms
    that exact URL is what's sitting there right now, in which case the
    download is skipped entirely.

    location, if given, is a pre-resolved (folder, tvshowid) tuple -- pass
    this when the caller already looked the show up for another reason
    (e.g. also writing tvshow.nfo) so this doesn't repeat the same
    VideoLibrary/source-browsing lookup a second time."""
    if not artwork:
        return
    if location:
        folder, _tvshowid = location
    else:
        folder, _tvshowid = find_show_location(title, year)
    if not folder:
        return
    _sync_art_files(folder, '', artwork, _SHOW_ART_FILES,
                     log_label='sync_show_art: "{0}" ({1})'.format(title, year))


def sync_season_art(show_title, folder, season_number, artwork):
    """Writes seasonNN-poster.jpg/seasonNN-fanart.jpg (or season-specials-
    for season 0) into the show's own root folder -- the SAME folder
    sync_show_art writes to, since Kodi has no per-season subfolder
    convention. Takes folder directly rather than re-resolving it: every
    caller already has it from the show-level lookup that just ran a
    moment before this."""
    if not artwork or not folder:
        return
    prefix = 'season-specials' if season_number == 0 else 'season{0:02d}'.format(season_number)
    _sync_art_files(folder, prefix + '-', artwork, _SEASON_ART_FILES,
                     log_label='sync_season_art: "{0}" season {1}'.format(show_title, season_number))


def sync_episode_art(show_title, folder, video_basename, artwork):
    """Writes <video-basename>-thumb.jpg next to the episode's own video
    file -- the same basename-prefixed convention movie local art already
    uses. Takes folder+video_basename directly, same reasoning as
    sync_season_art: get_episode_details() has already resolved these via
    lib.tvshow_location.get_episode() by the time this is called."""
    if not artwork or not folder or not video_basename:
        return
    _sync_art_files(folder, video_basename + '-', artwork, _EPISODE_ART_FILES,
                     log_label='sync_episode_art: "{0}"'.format(show_title))


def _sync_art_files(folder, prefix, artwork, art_files, log_label):
    for art_type, ext in art_files:
        candidates = artwork.get(art_type)
        if not candidates:
            continue
        url = candidates[0]['url']
        dest = '{0}{1}{2}.{3}'.format(folder, prefix, art_type, ext)

        if art_sync_cache.already_synced(dest, url):
            log.info('{0} -- {1} already matches Chronicle\'s current pick, skipping '
                     'download'.format(log_label, art_type))
            continue

        log.info('{0} -- writing {1} from {2} to {3}'.format(log_label, art_type, url, dest))
        if write_remote_file(dest, url) == 'ok':
            art_sync_cache.remember(dest, url)
            log.info('{0} -- synced local {1} from Chronicle'.format(log_label, art_type))
