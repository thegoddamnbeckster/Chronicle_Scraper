# -*- coding: utf-8 -*-
"""Shared helpers for populating Kodi's InfoTagVideo from Chronicle's scraper API
responses. Used by both python/scraper.py (movies) and python/tvshow_scraper.py.

Every setter call here is ground-truthed against Team Kodi's own bundled
metadata.tvshows.themoviedb.org.python addon -- the real, current InfoTagVideo-
based scraper contract for Kodi 20+ -- rather than copied from the older movies
scraper's deprecated setInfo('video', {...}) dict style.
"""

import re

from lib.logger import Logger

log = Logger('video_info')

_YOUTUBE_RE = re.compile(r'(?:v=|youtu\.be/)([\w-]{6,})')


def youtube_trailer_uri(trailer_url):
    """Convert a plain https://youtube.com/watch?v=ID URL (as Chronicle stores it,
    read from Trakt's own metadata) into the plugin:// URI Kodi's trailer player
    expects -- same convention as Team Kodi's own TMDB scraper's _parse_trailer()."""
    if not trailer_url:
        return None
    match = _YOUTUBE_RE.search(trailer_url)
    if not match:
        return None
    return 'plugin://plugin.video.youtube/?action=play_video&videoid=' + match.group(1)


def apply_common_video_info(vtag, details):
    """Fields shared by movies and TV shows: title/plot/year/premiered/mpaa/
    country/studio/genres/tags/external IDs. Only sets what's actually present --
    Chronicle omits fields no configured provider currently supplies rather than
    sending a placeholder."""
    if details.get('title'):
        vtag.setTitle(details['title'])
    if details.get('overview'):
        vtag.setPlot(details['overview'])
        vtag.setPlotOutline(details['overview'])
    if details.get('year'):
        vtag.setYear(details['year'])
    if details.get('premiered'):
        # Chronicle passes through whatever ISO date/datetime string the provider
        # gave it; Kodi's setPremiered wants just the date portion.
        vtag.setPremiered(details['premiered'][:10])
    if details.get('mpaa'):
        vtag.setMpaa(details['mpaa'])
    if details.get('country'):
        vtag.setCountries([details['country']])
    if details.get('studio'):
        vtag.setStudios([details['studio']])
    if details.get('genres'):
        vtag.setGenres(details['genres'])
    if details.get('tags'):
        vtag.setTags(details['tags'])

    ids = details.get('externalIds') or {}
    unique_ids = {k: v for k, v in (
        ('imdb', ids.get('imdb')), ('tmdb', ids.get('tmdb')),
        ('tvdb', ids.get('tvdb')), ('trakt', ids.get('trakt')),
    ) if v}
    if unique_ids:
        default_source = 'imdb' if 'imdb' in unique_ids else next(iter(unique_ids))
        vtag.setUniqueIDs(unique_ids, default_source)


def apply_ratings(vtag, ratings):
    """One InfoTagVideo.setRating() call per provider source Chronicle has a rating
    from -- ground-truthed against the real TV scraper's own _set_rating() loop,
    which calls setRating() repeatedly with isdefault=True on the first one only."""
    if not ratings:
        return
    first = True
    for source, info in ratings.items():
        rating = info.get('rating')
        if not rating:
            continue
        vtag.setRating(rating, votes=info.get('votes') or 0, type=source, isdefault=first)
        first = False


def apply_artwork(listitem, artwork):
    """Pins Chronicle's own pick (always candidates[0] -- see ScraperController's
    CollectArtwork, which adds Chronicle's authoritative choice before any
    provider partition) as the actively-displayed art via ListItem.setArt(),
    then additionally offers every candidate (including that same pick) via
    addAvailableArtwork() so "choose art" has real alternates.

    addAvailableArtwork() alone is NOT enough to make Chronicle's pick the one
    Kodi actually shows -- it only populates the candidate list; Kodi's own
    selection among multiple candidates doesn't reliably favor whichever was
    added first. setArt() is what actually pins the active image, exactly as
    the original (pre-multi-candidate) version of this addon did.

    'fanart' is still ListItem.setAvailableFanart()-only for the alternates
    list -- confirmed against the real TV scraper, since InfoTagVideo has no
    fanart-list setter as of Kodi 21."""
    if not artwork:
        log.warning('apply_artwork: called with no artwork at all -- setArt() will not be called, '
                    'Kodi keeps whatever art (if any) it already had for this item')
        return

    primary = {art_type: candidates[0]['url'] for art_type, candidates in artwork.items() if candidates}
    if primary:
        log.info('apply_artwork: pinning via setArt(): {0}'.format(
            ', '.join('{0}={1}'.format(k, v) for k, v in primary.items())))
        listitem.setArt(primary)
    else:
        log.warning('apply_artwork: artwork dict was non-empty but every art type had an empty '
                    'candidate list -- nothing to pin via setArt()')

    vtag = listitem.getVideoInfoTag()
    for art_type, candidates in artwork.items():
        if art_type == 'fanart':
            fanart_list = [{'image': c['url'], 'preview': c['url']} for c in candidates]
            if fanart_list:
                listitem.setAvailableFanart(fanart_list)
            continue
        for candidate in candidates:
            vtag.addAvailableArtwork(candidate['url'], art_type)
