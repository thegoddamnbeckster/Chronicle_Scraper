# -*- coding: utf-8 -*-
"""Season/episode numbers read from an episode's own video file name.

Kodi's own season/episode numbers for an episode are only as good as whatever answered its scrape
at the time; when they are wrong (a re-ordered season, a mis-scrape, Chronicle renumbering later),
matching a Kodi episode to Chronicle's by those numbers silently pairs it with a DIFFERENT
episode -- its title, plot, aired date, thumb, rating and even watched status then land on the
wrong file. The file name is the one independent witness to which episode a file really is, so the
periodic sync and the post-scan check match on it first and only fall back to Kodi's numbers when
the name carries none.
"""
import re

# S01E03, s1e3, S01.E03, S01_E03, S01 E03 -- the first episode when a file holds several (S01E03E04).
_SXXEXX = re.compile(r'(?<![A-Za-z0-9])[Ss](\d{1,3})[ ._-]{0,2}[Ee](\d{1,4})(?!\d)')
# 1x03 -- only accepted with a two-or-three digit episode so "1x1" style noise and resolutions
# like 1920x1080 (four-digit season) are not mistaken for one.
_NXNN = re.compile(r'(?<![A-Za-z0-9])(\d{1,2})[xX](\d{2,3})(?![A-Za-z0-9])')


def from_file_name(name):
    """(season, episode) parsed from a file name or path, or (None, None) when it has neither."""
    if not name:
        return None, None
    base = name.replace('\\', '/').rsplit('/', 1)[-1]
    for pattern in (_SXXEXX, _NXNN):
        m = pattern.search(base)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def resolve(kodi_episode):
    """The (season, episode) to match a Kodi episode to Chronicle's: the file name's numbers when it
    has them, otherwise Kodi's own."""
    season, episode = from_file_name(kodi_episode.get('file'))
    if season is not None and episode is not None:
        return season, episode
    return kodi_episode.get('season'), kodi_episode.get('episode')
