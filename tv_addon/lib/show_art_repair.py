# -*- coding: utf-8 -*-
"""Removes a TV show's own local poster file so Chronicle's pick is what Kodi shows.

Why: Kodi uses a local "poster.jpg" sitting in a show's folder in preference to ANY scraper's art, with no setting to
change that (the same un-overridable behaviour movie_art_sync.py documents for movies). A wrong poster left in a show
folder by some earlier tool therefore comes back every time Kodi refreshes, however often it is corrected in Kodi or
Chronicle (reported 2026-10-12: Invasion showed the poster of a Latvian film; a download tool had written it, with
fanart, into the folder months earlier).

The movie addon solves that by OVERWRITING the local file with Chronicle's pick. Here the file is deleted instead:
nothing local can then override Chronicle's art, now or if the pick changes later, and no copy of an image is kept
on the share that could go stale. It only happens when Chronicle actually has a poster to put in its place, and only to
the show's own root folder, found through the same exact-match lookup the episode scraper uses. Controlled by the
"remove_local_show_poster" setting (on by default).
"""
import posixpath

import xbmcaddon
import xbmcvfs

from lib.logger import Logger
from lib.movie_art_sync import listdir_with_timeout, _invalidate_texture
from lib.tvshow_location import find_show_location

log = Logger('show_art_repair')

SETTING_ID = 'remove_local_show_poster'

# The names Kodi itself reads a show's poster from, in the show's root folder.
_POSTER_STEMS = ('poster', 'folder')
_POSTER_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tbn', '.webp')


def is_local_poster_name(filename):
    stem, ext = posixpath.splitext((filename or '').lower())
    return stem in _POSTER_STEMS and ext in _POSTER_EXTENSIONS


def remove_local_show_posters(title, year, artwork):
    """Deletes the show's local poster file(s). Returns the number removed. Never raises: a failure only means the local
    file stays, as it did before this existed."""
    try:
        if not xbmcaddon.Addon().getSettingBool(SETTING_ID):
            return 0
        if not (artwork or {}).get('poster'):
            return 0   # nothing from Chronicle to show instead: keep what is there

        folder, _tvshowid = find_show_location(title, year)
        if not folder:
            return 0

        _dirs, files = listdir_with_timeout(folder)
        removed = 0
        for name in files or []:
            if not is_local_poster_name(name):
                continue
            path = folder.rstrip('/') + '/' + name
            if xbmcvfs.delete(path):
                removed += 1
                log.info('Removed local poster {0} so Chronicle\'s poster is used'.format(path))
                _invalidate_texture(path)
            else:
                log.warning('Could not remove local poster {0}'.format(path))
        return removed
    except Exception as exc:
        log.warning('remove_local_show_posters({0!r}, {1}): {2}'.format(title, year, exc))
        return 0
