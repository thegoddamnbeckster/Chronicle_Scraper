# -*- coding: utf-8 -*-
"""Lightweight, non-destructive watch-history/rating sync for everything ALREADY in this
device's own Kodi VideoLibrary -- reconciles resume position, fully-watched status, and rating
against Chronicle in both directions where applicable (see lib/progress_sync.py for the actual
direction logic), for movies, TV shows, and episodes, in one combined pass.

Why this exists as its OWN pass, separate from nfo_rebuild.py: reconciliation used to happen
purely as a side effect of get_details()/get_episode_details() being invoked during a real
scrape (per-user request, 2026-08-30: "I don't want a separate sync task in Kodi for ratings...
this needs to happen with the scraper automatically as part of the scrape process"). That held
up as long as every item's own scrape ran often enough for it to matter. It stopped holding once
nfo_rebuild.py's 2026-09-06 rework introduced a cross-device rebuild QUEUE that deliberately
never re-claims an already-completed item (see NfoRebuildQueueItem's own doc) -- rating/resume/
watched changes made directly in Kodi (not through Chronicle's own API, which already reaches
Kodi live via NfoPushService) would otherwise never be picked up again after an item's first
rebuild. Per-user correction (2026-09-06): make this its own automatic, scheduled thing instead.

Deliberately does NOT touch local NFO files, rebuild_state, or the rebuild queue at all --
resume/rating/watched are set directly via VideoLibrary.Set*Details, a lightweight, purely
Kodi-database write with no local file I/O and no local-NFO-wins gate to fight. That also means
this needs no cross-device coordination the way the destructive NFO rebuild does: it's
idempotent and non-destructive, so every device can run it independently on its own schedule
without ever racing another device over a shared file.

Resolves each Kodi item to its own Chronicle MediaItemId via the same search_movie()/
search_show() "resolve or create" calls find()/get_details() already use during an ordinary
scrape -- no new Chronicle endpoints needed for any of this. Only ever calls that resolve-OR-
CREATE search once per item, though, not once per PASS: lib/media_id_cache.py persists the
result across runs, so a periodic sync against a library that hasn't changed since last time
skips straight to get_movie_details()/get_show_details() with an already-known id. Without
this, every ~2-hour tick would re-run Chronicle's full multi-provider resolve search (confirmed
elsewhere in this codebase to take up to 90s in a slow case) for every single item, forever --
wasted provider-facing load, and a real (if small) risk of a title/year drift ever matching a
different item on a later resolve.
"""

import json
import posixpath

import xbmc

from lib import episode_numbers
from lib import media_id_cache
from lib.full_sync_check import year_from_path
from lib import progress_sync
from lib.chronicle_client import ChronicleClient
from lib.logger import Logger

log = Logger('watch_rating_sync')

_MIN_ITEM_SPACING_SECONDS = 0.2

# Extends progress_sync.STATE_PROPERTIES (userrating/resume/playcount/lastplayed) with the
# identity fields this module's own bulk listing calls need on top -- rather than redeclaring
# the shared part separately, so a future change to what reconciliation depends on can't
# silently drift between the per-scrape lookup path (progress_sync.lookup_movie_state) and
# this periodic-sync path.
_MOVIE_STATE_PROPERTIES = ['title', 'year', 'file'] + progress_sync.STATE_PROPERTIES
_SHOW_PROPERTIES = ['title', 'year', 'userrating', 'plot', 'premiered', 'mpaa', 'genre', 'studio', 'uniqueid']
_EPISODE_STATE_PROPERTIES = (['season', 'episode', 'file', 'title', 'plot', 'firstaired', 'art']
                             + progress_sync.STATE_PROPERTIES)


def run(is_cancelled=None, progress_callback=None):
    """Runs one full pass over every movie, show, and episode already in this device's own
    Kodi VideoLibrary. progress_callback(index, total, label), if given, is called once per
    item. is_cancelled(), if given, is checked between items and stops the run early -- safe to
    interrupt at any point, since every item's own reconciliation is a single, self-contained
    JSON-RPC round trip with no multi-step local state to leave half-finished.

    Returns a dict: {'movies': N, 'shows': N, 'episodes': N, 'errors': N, 'cancelled': bool} --
    counts are "items visited", not "items changed" (most items reconcile to a no-op, which
    isn't separately worth counting here; see kodi.log for what actually changed on any given
    item).
    """
    client = ChronicleClient()
    cache = media_id_cache.load()
    movies = _get_all_movies()
    shows = _get_all_tvshows()
    total = len(movies) + len(shows)  # episodes are counted/logged per-show below, not known yet
    log.info('watch_rating_sync: starting -- {0} movie(s), {1} show(s) in this device\'s own '
             'VideoLibrary'.format(len(movies), len(shows)))

    counts = {'movies': 0, 'shows': 0, 'episodes': 0, 'errors': 0}
    processed = 0
    cancelled = False

    for movie in movies:
        if is_cancelled is not None and is_cancelled():
            cancelled = True
            break
        label = '{0} ({1})'.format(movie.get('title') or '?', movie.get('year') or '?')
        if progress_callback is not None:
            progress_callback(processed, total, label)
        try:
            _sync_one_movie(client, cache, movie)
            counts['movies'] += 1
        except Exception as exc:
            counts['errors'] += 1
            log.warning('watch_rating_sync: movie "{0}" failed: {1}'.format(label, exc))
        processed += 1
        xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))

    if not cancelled:
        for show in shows:
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                break
            label = '{0} ({1})'.format(show.get('title') or '?', show.get('year') or '?')
            if progress_callback is not None:
                progress_callback(processed, total, label)
            try:
                episode_count = _sync_one_show(
                    client, cache, show, is_cancelled, progress_callback, processed, total)
                counts['shows'] += 1
                counts['episodes'] += episode_count
            except Exception as exc:
                counts['errors'] += 1
                log.warning('watch_rating_sync: show "{0}" failed: {1}'.format(label, exc))
            processed += 1
            xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))

    media_id_cache.save(cache)

    log.info(
        'watch_rating_sync: done -- {0} movie(s), {1} show(s), {2} episode(s) visited, '
        '{3} error(s){4}'.format(
            counts['movies'], counts['shows'], counts['episodes'], counts['errors'],
            ' (cancelled)' if cancelled else ''))
    counts['cancelled'] = cancelled
    return counts


def _resolve_movie_id(client, cache, movie):
    """Returns this movie's Chronicle MediaItemId, preferring the persisted cache over a fresh
    (expensive, resolve-or-create) search_movie() call -- see media_id_cache.py's own doc. A
    cached id is verified by the caller's own subsequent get_movie_details() call; if that
    comes back empty (item deleted/merged in Chronicle since), the caller drops the cache entry
    and calls this again, which then falls through to a fresh search."""
    key = media_id_cache.movie_key(movie['movieid'])
    cached_id = cache.get(key)
    if cached_id is not None:
        return cached_id

    title = movie.get('title')
    file_path = movie.get('file') or ''
    filename = posixpath.basename(file_path) if file_path else None
    # The FILE's year, not Kodi's own: Kodi's is only as good as the scrape that set it, and a wrong
    # one ("Total Recall (2012).mkv" scraped as the 1990 film) made Chronicle's search agree with
    # the wrong item, cross-linking this file's watched state and rating to another film.
    year = year_from_path(file_path) or movie.get('year')

    result = client.search_movie(title, year, filename=filename)
    if not result or not result.get('id'):
        return None
    cache[key] = result['id']
    return result['id']


def _sync_one_movie(client, cache, movie):
    title = movie.get('title')
    year = movie.get('year')
    label = '{0} ({1})'.format(title, year)
    key = media_id_cache.movie_key(movie['movieid'])

    media_item_id = _resolve_movie_id(client, cache, movie)
    if media_item_id is None:
        log.info('watch_rating_sync: "{0}" -- Chronicle could not resolve this movie, '
                 'skipping'.format(label))
        return

    details = client.get_movie_details(media_item_id)
    if not details:
        # Cached id no longer resolves (deleted/merged in Chronicle since it was cached) --
        # drop it and retry once via a fresh search, same self-healing reasoning as
        # media_id_cache.py's own doc describes.
        if cache.pop(key, None) is not None:
            log.info('watch_rating_sync: "{0}" -- cached id {1} no longer resolves, '
                     're-resolving'.format(label, media_item_id))
            media_item_id = _resolve_movie_id(client, cache, movie)
            details = client.get_movie_details(media_item_id) if media_item_id else None
        if not details:
            log.info('watch_rating_sync: "{0}" -- no details returned, skipping'.format(label))
            return

    # A cached id is keyed by Kodi's OWN id, which Kodi reassigns when a library is rebuilt -- so it
    # can name a completely different film (see media_id_cache.titles_agree). Never write anything
    # for an item whose Chronicle match doesn't even share its title: drop the cache entry, resolve
    # afresh, and skip the item if it still disagrees.
    if not media_id_cache.titles_agree(title, details.get('title')):
        log.info('watch_rating_sync: "{0}" -- cached Chronicle item {1} is "{2}", not this movie; '
                 're-resolving'.format(label, media_item_id, details.get('title')))
        cache.pop(key, None)
        media_item_id = _resolve_movie_id(client, cache, movie)
        details = client.get_movie_details(media_item_id) if media_item_id else None
        if not details or not media_id_cache.titles_agree(title, details.get('title')):
            cache.pop(key, None)
            log.warning('watch_rating_sync: "{0}" -- no Chronicle item with a matching title; '
                        'skipping (nothing written)'.format(label))
            return

    # A cached id (or an earlier wrong match) whose film contradicts the year in the file's own name
    # is a cross-link, not a match: drop it and resolve again, now by the file's year.
    file_year = year_from_path(movie.get('file'))
    if file_year and details.get('year') and abs(file_year - details['year']) >= 2:
        log.info('watch_rating_sync: "{0}" -- file name says {1} but Chronicle item {2} is {3}; '
                 're-resolving'.format(label, file_year, media_item_id, details['year']))
        cache.pop(key, None)
        media_item_id = _resolve_movie_id(client, cache, movie)
        details = client.get_movie_details(media_item_id) if media_item_id else None
        if not details:
            return

    client.report_kodi_id(media_item_id, 'movie', movie['movieid'])

    updates = _build_state_updates(details, movie, client, media_item_id, log_label=label)
    if updates:
        _set_movie_details(movie['movieid'], updates)
        log.info('watch_rating_sync: "{0}" -- applied {1}'.format(label, ', '.join(sorted(updates.keys()))))


def _show_external_ids(show):
    """(source, externalId) pairs to look a Kodi show up by, in Chronicle's own id formats: TMDB's
    are stored "tv:{id}", IMDb's and TVDB's bare."""
    unique = show.get('uniqueid') or {}
    pairs = []
    if unique.get('tmdb'):
        pairs.append(('tmdb', 'tv:{0}'.format(unique['tmdb'])))
    if unique.get('imdb'):
        pairs.append(('imdb', unique['imdb']))
    if unique.get('tvdb'):
        pairs.append(('tvdb', unique['tvdb']))
    return pairs


def _resolve_show_id(client, cache, show):
    """Sibling of _resolve_movie_id for shows -- see that function's own doc."""
    key = media_id_cache.tvshow_key(show['tvshowid'])
    cached_id = cache.get(key)
    if cached_id is not None:
        return cached_id

    # By the show's own provider ids first: Kodi's title and (especially) YEAR are only as good as the
    # scrape that set them, and the title+year search below is resolve-OR-CREATE -- with a wrong year
    # it mints a duplicate empty show instead of finding the real one (confirmed live 2026-09-26:
    # "A Knight of the Seven Kingdoms" carried another show's year, so it never resolved to itself).
    result = None
    for source, external_id in _show_external_ids(show):
        result = client.resolve_show_by_external_id(source, external_id)
        if result and result.get('id'):
            break
        result = None
    if result is None:
        result = client.search_show(show.get('title'), show.get('year'))
    if not result or not result.get('id'):
        return None
    cache[key] = result['id']
    return result['id']


def _sync_one_show(client, cache, show, is_cancelled, progress_callback, processed, total):
    """progress_callback/processed/total are the SAME values run()'s own outer loop is already
    using for the show-level step -- passed through here purely so a show with many episodes
    updates the progress dialog's message per-episode (e.g. "Lanterns - S1E5") instead of
    appearing frozen on the show's own label for however long its whole episode list takes.
    The percentage itself intentionally doesn't advance mid-show (episodes aren't part of the
    movies+shows `total` denominator at all -- see run()'s own doc) -- only the label does."""
    title = show.get('title')
    year = show.get('year')
    label = '{0} ({1})'.format(title, year)
    key = media_id_cache.tvshow_key(show['tvshowid'])

    show_id = _resolve_show_id(client, cache, show)
    if show_id is None:
        log.info('watch_rating_sync: "{0}" -- Chronicle could not resolve this show, '
                 'skipping episodes too'.format(label))
        return 0

    show_details = client.get_show_details(show_id)
    if not show_details:
        # Same cached-id self-heal as _sync_one_movie -- see that function's own comment.
        if cache.pop(key, None) is not None:
            log.info('watch_rating_sync: "{0}" -- cached id {1} no longer resolves, '
                     're-resolving'.format(label, show_id))
            show_id = _resolve_show_id(client, cache, show)
            show_details = client.get_show_details(show_id) if show_id else None
        if not show_details:
            log.info('watch_rating_sync: "{0}" -- no details returned, skipping episodes '
                     'too'.format(label))
            return 0

    # Same guard as _sync_one_movie: a cached show id keyed by Kodi's own (reassignable) tvshowid can
    # name a different show, and everything below would then write THAT show's text, ratings and
    # watched state onto this one (confirmed live 2026-09-26: "A Knight of the Seven Kingdoms" got
    # "Star Trek: Discovery"'s episode titles and plots).
    if not media_id_cache.titles_agree(title, show_details.get('title')):
        log.info('watch_rating_sync: "{0}" -- cached Chronicle show {1} is "{2}", not this show; '
                 're-resolving'.format(label, show_id, show_details.get('title')))
        cache.pop(key, None)
        show_id = _resolve_show_id(client, cache, show)
        show_details = client.get_show_details(show_id) if show_id else None
        if not show_details or not media_id_cache.titles_agree(title, show_details.get('title')):
            cache.pop(key, None)
            log.warning('watch_rating_sync: "{0}" -- no Chronicle show with a matching title; '
                        'skipping it and its episodes (nothing written)'.format(label))
            return 0

    # Lets Chronicle's own NfoPushService push a future show-level update straight to this
    # device -- same reasoning as the movie/episode report_kodi_id calls below. Confirmed
    # missing in an earlier draft of this function: a show first resolved through THIS pass
    # (rather than through a real scrape) never got its tvshowid reported at all.
    client.report_kodi_id(show_id, 'tvshow', show['tvshowid'])

    if show_details.get('userRating'):
        kodi_rating = show.get('userrating') or 0
        if kodi_rating != show_details['userRating']:
            _set_tvshow_rating(show['tvshowid'], show_details['userRating'])
            log.info('watch_rating_sync: "{0}" -- rating updated to {1}'.format(
                     label, show_details['userRating']))

    # Show-level TEXT metadata (plot/premiered/rating/genre/studio): a show scraped once and never
    # corrected kept its old data forever, while movies and episodes already had a post-scan
    # check. Same contract as those -- only a field Chronicle has a value for AND that
    # differs is ever written.
    text_updates = diff_show_text(show, show_details)
    if text_updates:
        _set_tvshow_details(show['tvshowid'], text_updates)
        log.info('watch_rating_sync: "{0}" -- corrected {1}'.format(label, ', '.join(sorted(text_updates))))

    chronicle_episodes = client.get_episode_list(show_id) or []
    if not chronicle_episodes:
        return 0
    chronicle_by_key = {}
    for e in chronicle_episodes:
        ep_key = (e.get('season'), e.get('episode'))
        if ep_key in chronicle_by_key:
            log.warning('watch_rating_sync: "{0}" -- Chronicle has more than one episode for '
                        'S{1}E{2}; using the last one returned, ignoring id {3}'.format(
                        label, ep_key[0], ep_key[1], chronicle_by_key[ep_key].get('id')))
        chronicle_by_key[ep_key] = e

    kodi_episodes = _get_episodes_for_show(show['tvshowid'])
    synced = 0
    for kodi_ep in kodi_episodes:
        if is_cancelled is not None and is_cancelled():
            break
        # Matched on what the FILE really is (its title, then its numbers) rather than on Kodi's own
        # numbers alone: when those are wrong, or a show's files are numbered differently from
        # Chronicle's list, the file was paired with a DIFFERENT episode -- so its title, plot,
        # thumb, rating and watched status all landed on the wrong one (see lib/episode_numbers.py).
        chronicle_ep = episode_numbers.match(kodi_ep, chronicle_episodes)
        ep_key = ((chronicle_ep.get('season'), chronicle_ep.get('episode')) if chronicle_ep
                  else (kodi_ep.get('season'), kodi_ep.get('episode')))
        episode_id = chronicle_ep.get('id') if chronicle_ep else None
        if episode_id is None:
            continue  # Chronicle doesn't know this episode yet (or its own record has no id,
                      # which shouldn't normally happen) -- the rebuild queue's own seeding/
                      # resolution handles bringing new episodes in, not this pass.
        if progress_callback is not None:
            progress_callback(processed, total, '{0} S{1}E{2}'.format(title, ep_key[0], ep_key[1]))
        try:
            _sync_one_episode(client, show, kodi_ep, episode_id)
            synced += 1
        except Exception as exc:
            log.warning('watch_rating_sync: "{0}" S{1}E{2} failed: {3}'.format(
                        title, ep_key[0], ep_key[1], exc))
        xbmc.sleep(int(_MIN_ITEM_SPACING_SECONDS * 1000))
    return synced


def _sync_one_episode(client, show, kodi_ep, episode_id):
    label = '{0} S{1}E{2}'.format(show.get('title'), kodi_ep.get('season'), kodi_ep.get('episode'))
    details = client.get_episode_details(episode_id)
    if not details:
        return

    client.report_kodi_id(episode_id, 'episode', kodi_ep['episodeid'])

    updates = _build_state_updates(details, kodi_ep, client, episode_id, log_label=label)
    updates.update(diff_episode_text(kodi_ep, details))
    if updates:
        _set_episode_details(kodi_ep['episodeid'], updates)
        log.info('watch_rating_sync: "{0}" -- applied {1}'.format(label, ', '.join(sorted(updates.keys()))))


def _build_state_updates(details, kodi_item, client, media_item_id, log_label):
    """Shared reconciliation for movies and episodes (shows only carry a rating, handled
    separately in _sync_one_show since there's no resume/watched concept at the show level).
    Returns a dict of VideoLibrary.Set*Details params to apply locally (possibly empty), after
    also firing any PULL direction (Kodi's own state reported into Chronicle) as a side effect.
    log_label is purely for resume_seconds()'s own warning line to stay identifiable in
    kodi.log (e.g. which movie/episode a missing-runtime warning is actually about)."""
    updates = {}

    if details.get('userRating'):
        kodi_rating = kodi_item.get('userrating') or 0
        if kodi_rating != details['userRating']:
            updates['userrating'] = details['userRating']

    kodi_lastplayed = kodi_item.get('lastplayed')

    direction, value = progress_sync.resolve_progress_direction(
        details.get('resumePositionPercent'), details.get('resumeUpdatedAt'), kodi_item)
    if direction == 'push':
        resume = progress_sync.resume_seconds(
            value, details.get('runtimeMinutes'), log_label='watch_rating_sync: ' + log_label)
        if resume is not None:
            position, total = resume
            updates['resume'] = {'position': position, 'total': total}
    elif direction == 'pull':
        client.push_resume(media_item_id, value, progress_sync.kodi_lastplayed_to_iso(kodi_lastplayed))

    watched_direction, watched_value = progress_sync.resolve_watched_direction(
        details.get('isWatched'), details.get('lastWatchedAt'), kodi_item,
        chronicle_reset_at=details.get('watchResetAt'))
    if watched_direction == 'push' and watched_value:
        updates['playcount'] = 1
        updates['lastplayed'] = watched_value.replace('T', ' ')[:19]
        # Explicitly zero the resume point -- see progress_sync.apply_watched_push's own doc
        # for the full root-cause writeup (2026-09-09): a device with its own stale local
        # resume point kept it forever once the resume direction above started returning
        # 'pull' (nothing to push, Chronicle's own side already cleared), even as this
        # watched-push independently set playcount=1 in the very same reconciliation pass.
        # Placed after the resume block on purpose so it always wins if the two ever
        # legitimately disagree here.
        updates['resume'] = {'position': 0, 'total': 0}
    elif watched_direction == 'reset':
        # The user reset this item in Chronicle after Kodi's last play -- clear Kodi's own stale
        # watched state instead of pulling it back (see progress_sync.resolve_watched_direction).
        # playcount 0 alone is what "unwatched" means (Kodi drops the last-played date itself when
        # a playcount is set to 0); lastplayed is deliberately not sent, since one rejected
        # property would fail the whole Set*Details call and lose the rating update with it.
        updates['playcount'] = 0
        updates['resume'] = {'position': 0, 'total': 0}
    elif watched_direction == 'pull':
        client.push_watched(media_item_id, progress_sync.kodi_lastplayed_to_iso(kodi_lastplayed))

    return updates


# ── Kodi VideoLibrary reads/writes ──────────────────────────────────────────

def _get_all_movies():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetMovies',
        'params': {'properties': _MOVIE_STATE_PROPERTIES},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get movie list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetMovies rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('movies') or []


def _get_all_tvshows():
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetTVShows',
        'params': {'properties': _SHOW_PROPERTIES},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get TV show list: {0}".format(exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetTVShows rejected: {0}'.format(response['error']))
        return []
    return response.get('result', {}).get('tvshows') or []


def _get_episodes_for_show(tvshowid):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.GetEpisodes',
        'params': {'tvshowid': tvshowid, 'properties': _EPISODE_STATE_PROPERTIES},
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.error("Couldn't get episodes for tvshowid {0}: {1}".format(tvshowid, exc))
        return []
    if 'error' in response:
        log.error('VideoLibrary.GetEpisodes rejected tvshowid {0}: {1}'.format(tvshowid, response['error']))
        return []
    return response.get('result', {}).get('episodes') or []


def _set_movie_details(movieid, updates):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetMovieDetails',
        'params': dict(updates, movieid=movieid),
    }
    _execute_set(request, 'SetMovieDetails', movieid)


def _set_episode_details(episodeid, updates):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetEpisodeDetails',
        'params': dict(updates, episodeid=episodeid),
    }
    _execute_set(request, 'SetEpisodeDetails', episodeid)


def diff_episode_text(kodi_ep, details):
    """VideoLibrary.SetEpisodeDetails params for whatever descriptive fields of a Kodi episode
    (listed with _EPISODE_STATE_PROPERTIES) differ from Chronicle's details for the SAME episode --
    {} when everything already matches. Deliberately does NOT renumber: Kodi's numbering follows
    the files (a multi-episode file is two Kodi entries), which can legitimately differ from
    Chronicle's. Never blanks a field Chronicle has nothing for."""
    updates = {}
    if details.get('title') and kodi_ep.get('title') != details['title']:
        updates['title'] = details['title']
    if details.get('overview') and kodi_ep.get('plot') != details['overview']:
        updates['plot'] = details['overview']
    aired = details.get('aired')
    if aired and kodi_ep.get('firstaired') != aired[:10]:
        updates['firstaired'] = aired[:10]
    thumb = details.get('thumbUrl')
    if thumb and (kodi_ep.get('art') or {}).get('thumb') != thumb:
        art = dict(kodi_ep.get('art') or {})
        art['thumb'] = thumb
        updates['art'] = art
    return updates


def diff_show_text(kodi_show, details):
    """VideoLibrary.SetTVShowDetails params for whatever text fields of a Kodi show (listed with
    _SHOW_PROPERTIES) differ from Chronicle's show details -- {} when everything already matches.
    Never blanks a field Chronicle has nothing for, and compares list fields as sets since Kodi
    doesn't guarantee read-back order."""
    updates = {}
    if details.get('year') and kodi_show.get('year') != details['year']:
        updates['year'] = details['year']
    if details.get('overview') and kodi_show.get('plot') != details['overview']:
        updates['plot'] = details['overview']
    premiered = details.get('premiered')
    if premiered and kodi_show.get('premiered') != premiered[:10]:
        updates['premiered'] = premiered[:10]
    if details.get('mpaa') and kodi_show.get('mpaa') != details['mpaa']:
        updates['mpaa'] = details['mpaa']
    genres = details.get('genres') or []
    if genres and set(kodi_show.get('genre') or []) != set(genres):
        updates['genre'] = genres
    studio = details.get('studio')
    if studio and set(kodi_show.get('studio') or []) != {studio}:
        updates['studio'] = [studio]
    return updates


def _set_tvshow_details(tvshowid, updates):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetTVShowDetails',
        'params': dict(updates, tvshowid=tvshowid),
    }
    _execute_set(request, 'SetTVShowDetails', tvshowid)


def _set_tvshow_rating(tvshowid, rating):
    request = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'VideoLibrary.SetTVShowDetails',
        'params': {'tvshowid': tvshowid, 'userrating': rating},
    }
    _execute_set(request, 'SetTVShowDetails', tvshowid)


def _execute_set(request, method_label, item_id):
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except Exception as exc:
        log.warning("{0}({1}): failed: {2}".format(method_label, item_id, exc))
        return
    if 'error' in response:
        log.warning('{0}({1}) rejected: {2}'.format(method_label, item_id, response['error']))
