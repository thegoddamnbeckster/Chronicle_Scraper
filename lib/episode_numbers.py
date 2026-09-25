# -*- coding: utf-8 -*-
"""Matching a Kodi episode to Chronicle's episode for the SAME file.

Kodi's own season/episode numbers are only as good as the scrape that set them, and a file's own
name is the one independent witness to which episode it really is. But NUMBERS alone are not
reliable either -- confirmed live (2026-09-26) on a real library:

  * multi-episode files ("Enterprise - S01E01-E02 - Broken Bow.mkv") legitimately appear in Kodi as
    TWO episodes sharing one file, so a name that says E01 must not renumber the entry that is E02;
  * some shows' files are numbered differently from Chronicle's episode list (a two-part pilot the
    files count as E01+E02 and Chronicle as one episode): "Voyager - S01E03 - Parallax.mkv" got
    Chronicle's S01E03, "Time and Again", so the wrong title, plot and thumb landed on the file.

So the match is: (1) the TITLE in the file name, when it identifies exactly one Chronicle episode of
that season; (2) otherwise the numbers -- Kodi's own for a multi-episode file, the name's for a
single-episode file, Kodi's when the name has none. Anything ambiguous falls back to the numbers
the addon always used, never to a guess.
"""
import re

# S01E03, s1e3, S01.E03, S01_E03, S01 E03 -- followed by any further episodes: S01E03E04, S01E03-E04.
_SXXEXX = re.compile(
    r'(?<![A-Za-z0-9])[Ss](\d{1,3})[ ._-]{0,2}[Ee](\d{1,4})((?:[ ._-]{0,2}[Ee]\d{1,4})*)(?!\d)')
_EXTRA_EPISODE = re.compile(r'[Ee](\d{1,4})')
# 1x03 -- only with a two-or-three digit episode so "1x1" style noise and resolutions like 1920x1080
# (four-digit season) are not mistaken for one.
_NXNN = re.compile(r'(?<![A-Za-z0-9])(\d{1,2})[xX](\d{2,3})(?![A-Za-z0-9])')
_EXTENSION = re.compile(r'\.[A-Za-z0-9]{2,4}$')
_BRACKETED = re.compile(r'[\[\(][^\]\)]*[\]\)]')
_PART_MARKER = re.compile(r'\b(?:part|pt)[ .]*(?:\d+|i{1,3}|iv|v)\b', re.IGNORECASE)


def _base(name):
    return name.replace('\\', '/').rsplit('/', 1)[-1]


def parse(name):
    """(season, [episodes], title) read from a file name or path; (None, [], None) when it has no
    season/episode. title is the text after the numbering with the extension and bracketed tags
    removed, or None when nothing is left."""
    if not name:
        return None, [], None
    base = _base(name)
    m = _SXXEXX.search(base)
    if m:
        season = int(m.group(1))
        episodes = [int(m.group(2))] + [int(x) for x in _EXTRA_EPISODE.findall(m.group(3) or '')]
        rest = base[m.end():]
    else:
        m = _NXNN.search(base)
        if not m:
            return None, [], None
        season, episodes, rest = int(m.group(1)), [int(m.group(2))], base[m.end():]
    rest = _EXTENSION.sub('', rest)
    rest = _BRACKETED.sub(' ', rest)
    rest = rest.strip(' ._-')
    return season, episodes, (rest or None)


def from_file_name(name):
    """(season, first episode) parsed from a file name or path, or (None, None)."""
    season, episodes, _ = parse(name)
    return (season, episodes[0]) if episodes else (None, None)


def normalize_title(title):
    """Lowercase, part markers and punctuation removed -- 'Caretaker (1)', 'Caretaker, Part I' and
    'Caretaker' all compare equal, since a two-part episode is one title split across files."""
    if not title:
        return ''
    t = _PART_MARKER.sub(' ', title.lower())
    t = re.sub(r'\(\s*\d+\s*\)\s*$', ' ', t)
    t = re.sub(r'[^a-z0-9]+', ' ', t)
    return t.strip()


def resolve(kodi_episode):
    """The (season, episode) to look up by number: the file name's for a single-episode file, else
    Kodi's own (a multi-episode file's entries each keep their own number)."""
    season, episodes, _ = parse(kodi_episode.get('file'))
    kodi_number = kodi_episode.get('episode')
    if season is not None and len(episodes) == 1:
        return season, episodes[0]
    if season is not None and len(episodes) > 1 and kodi_number in episodes:
        return season, kodi_number
    return kodi_episode.get('season'), kodi_number


def match(kodi_episode, chronicle_episodes):
    """The Chronicle episode summary ({'id','season','episode','title'}) this Kodi episode's FILE is
    really about, or None. Title first (see module doc), numbers as the fallback."""
    season, episodes, file_title = parse(kodi_episode.get('file'))
    wanted = normalize_title(file_title)
    if wanted and len(episodes) <= 1:
        in_season = [e for e in chronicle_episodes
                     if season is None or e.get('season') == season]
        hits = [e for e in in_season if normalize_title(e.get('title')) == wanted]
        if len(hits) == 1:
            return hits[0]

    key = resolve(kodi_episode)
    by_number = [e for e in chronicle_episodes if (e.get('season'), e.get('episode')) == key]
    return by_number[-1] if by_number else None
