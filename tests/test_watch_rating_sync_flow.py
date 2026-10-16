# -*- coding: utf-8 -*-
"""watch_rating_sync's per-item flows: id resolution and self-healing, the title/year cross-link guards, and the run() totals."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import media_id_cache, watch_rating_sync as wrs


def _movie(**kw):
    movie = {'movieid': 5, 'title': 'Heat', 'year': 1995, 'file': 'smb://nas/Movies/Heat (1995)/Heat (1995).mkv'}
    movie.update(kw)
    return movie


def _details(**kw):
    details = {'title': 'Heat', 'year': 1995, 'artwork': {}}
    details.update(kw)
    return details


class _MovieBase(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.cache = {}
        self.patches = {}
        for name, new in (
                ('_build_state_updates', mock.Mock(return_value={})),
                ('_set_movie_details', mock.Mock()),
                ('refresh_movie', mock.Mock()),
                ('diff_movie', mock.Mock(return_value={})),
                ('needs_cast_refresh', mock.Mock(return_value=False)),
                ('set_differs', mock.Mock(return_value=False))):
            p = mock.patch.object(wrs, name, new)
            self.patches[name] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(wrs.movie_art_sync, 'sync_movie_art', return_value=False)
        self.art = p.start()
        self.addCleanup(p.stop)

    def _sync(self, movie=None):
        return wrs._sync_one_movie(self.client, self.cache, movie or _movie())


class TestResolveMovieId(_MovieBase):
    def test_cached_id_is_used_without_searching(self):
        self.cache['movie:5'] = 99
        self.assertEqual(wrs._resolve_movie_id(self.client, self.cache, _movie()), 99)
        self.client.search_movie.assert_not_called()

    def test_search_uses_the_files_year_and_basename_then_caches(self):
        self.client.search_movie.return_value = {'id': 42}
        movie = _movie(year=2012, file='smb://nas/Total Recall (1990)/Total Recall (1990).mkv')
        self.assertEqual(wrs._resolve_movie_id(self.client, self.cache, movie), 42)
        args, kwargs = self.client.search_movie.call_args
        self.assertEqual((args[1], kwargs['filename']), (1990, 'Total Recall (1990).mkv'))
        self.assertEqual(self.cache['movie:5'], 42)

    def test_unresolvable_gives_none_and_caches_nothing(self):
        for result in (None, {}, {'id': 0}):
            self.client.search_movie.return_value = result
            self.assertIsNone(wrs._resolve_movie_id(self.client, self.cache, _movie()))
        self.assertEqual(self.cache, {})


class TestSyncOneMovie(_MovieBase):
    def test_unresolvable_movie_writes_nothing(self):
        self.client.search_movie.return_value = None
        self.assertFalse(self._sync())
        self.patches['_set_movie_details'].assert_not_called()

    def test_applies_updates_reports_the_kodi_id_and_says_changed(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.return_value = _details()
        self.patches['_build_state_updates'].return_value = {'userrating': 8}
        self.assertTrue(self._sync())
        self.client.report_kodi_id.assert_called_once_with(42, 'movie', 5)
        self.patches['_set_movie_details'].assert_called_once_with(5, {'userrating': 8})

    def test_nothing_to_change_is_not_changed(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.return_value = _details()
        self.assertFalse(self._sync())
        self.patches['_set_movie_details'].assert_not_called()

    def test_art_change_alone_counts_as_changed(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.return_value = _details()
        self.art.return_value = True
        self.assertTrue(self._sync())

    def test_art_failure_does_not_fail_the_movie(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.return_value = _details()
        self.art.side_effect = OSError('share offline')
        self.assertFalse(self._sync())

    def test_stale_cached_id_is_dropped_and_re_resolved_once(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.side_effect = [None, _details()]
        self.client.search_movie.return_value = {'id': 77}
        self.patches['_build_state_updates'].return_value = {'userrating': 8}
        self.assertTrue(self._sync())
        self.assertEqual(self.cache['movie:5'], 77)
        self.client.report_kodi_id.assert_called_once_with(77, 'movie', 5)

    def test_uncached_id_with_no_details_is_skipped_without_a_retry(self):
        self.client.search_movie.return_value = {'id': 42}
        self.client.get_movie_details.return_value = None
        self.assertFalse(self._sync())
        self.assertEqual(self.client.get_movie_details.call_count, 1)

    def test_a_cached_id_naming_another_film_is_never_written_to(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.return_value = _details(title='Evil Bong 2')
        self.client.search_movie.return_value = {'id': 43}
        self.assertFalse(self._sync())
        self.patches['_set_movie_details'].assert_not_called()
        self.client.report_kodi_id.assert_not_called()
        self.assertNotIn('movie:5', self.cache)

    def test_a_title_mismatch_that_resolves_afresh_is_written(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.side_effect = [_details(title='Evil Bong 2'), _details()]
        self.client.search_movie.return_value = {'id': 43}
        self.patches['_build_state_updates'].return_value = {'userrating': 9}
        self.assertTrue(self._sync())
        self.client.report_kodi_id.assert_called_once_with(43, 'movie', 5)

    def test_a_year_contradicting_the_file_name_is_re_resolved_then_skipped_if_still_wrong(self):
        self.cache['movie:5'] = 42
        movie = _movie(file='smb://nas/Total Recall (2012)/Total Recall (2012).mkv', title='Total Recall', year=2012)
        self.client.get_movie_details.return_value = _details(title='Total Recall', year=1990)
        self.client.search_movie.return_value = {'id': 43}
        self.assertFalse(self._sync(movie))
        self.patches['_set_movie_details'].assert_not_called()
        self.assertNotIn('movie:5', self.cache)

    def test_a_year_contradiction_fixed_by_the_fresh_lookup_is_written(self):
        self.cache['movie:5'] = 42
        movie = _movie(file='smb://nas/Total Recall (2012)/Total Recall (2012).mkv', title='Total Recall', year=2012)
        self.client.get_movie_details.side_effect = [_details(title='Total Recall', year=1990),
                                                     _details(title='Total Recall', year=2012)]
        self.client.search_movie.return_value = {'id': 43}
        self.patches['_build_state_updates'].return_value = {'userrating': 6}
        self.assertTrue(self._sync(movie))
        self.client.report_kodi_id.assert_called_once_with(43, 'movie', 5)

    def test_one_year_off_is_not_a_contradiction(self):
        self.assertFalse(wrs._contradicts([1995], 1996))
        self.assertTrue(wrs._contradicts([1995], 1997))
        self.assertFalse(wrs._contradicts([], 1997))
        self.assertFalse(wrs._contradicts([1990, 1996], 1997))   # any near year is enough

    def test_cast_or_collection_difference_triggers_one_rescrape_per_collection(self):
        self.cache['movie:5'] = 42
        self.client.get_movie_details.return_value = _details(collection={'name': 'Heat Trilogy'})
        self.patches['set_differs'].return_value = True
        self.assertTrue(self._sync())
        self.patches['refresh_movie'].assert_called_once_with(5)
        self.assertEqual(self.cache['setrefresh:5'], 'Heat Trilogy')
        self.patches['refresh_movie'].reset_mock()
        self.assertFalse(self._sync())   # same collection name: not re-scraped again
        self.patches['refresh_movie'].assert_not_called()


class TestShowExternalIds(unittest.TestCase):
    def test_tmdb_is_prefixed_and_order_is_tmdb_imdb_tvdb(self):
        show = {'uniqueid': {'tvdb': '81189', 'imdb': 'tt0903747', 'tmdb': '1396'}}
        self.assertEqual(wrs._show_external_ids(show), [('tmdb', 'tv:1396'), ('imdb', 'tt0903747'), ('tvdb', '81189')])
        self.assertEqual(wrs._show_external_ids({}), [])
        self.assertEqual(wrs._show_external_ids({'uniqueid': None}), [])


class TestResolveShowId(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.cache = {}

    def test_provider_ids_are_tried_before_the_resolve_or_create_search(self):
        self.client.resolve_show_by_external_id.side_effect = [None, {'id': 9}]
        show = {'tvshowid': 7, 'title': 'S', 'year': 2020, 'uniqueid': {'tmdb': '1', 'imdb': 'tt1'}}
        self.assertEqual(wrs._resolve_show_id(self.client, self.cache, show), 9)
        self.client.search_show.assert_not_called()
        self.assertEqual(self.cache['tvshow:7'], 9)

    def test_falls_back_to_the_title_search_and_gives_none_when_unresolvable(self):
        self.client.resolve_show_by_external_id.return_value = None
        self.client.search_show.return_value = None
        self.assertIsNone(wrs._resolve_show_id(self.client, self.cache, {'tvshowid': 7, 'title': 'S', 'uniqueid': {'tmdb': '1'}}))
        self.client.search_show.assert_called_once_with('S', None)

    def test_cache_hit_skips_everything(self):
        self.cache['tvshow:7'] = 3
        self.assertEqual(wrs._resolve_show_id(self.client, self.cache, {'tvshowid': 7}), 3)
        self.client.search_show.assert_not_called()


class TestSyncOneShow(unittest.TestCase):
    def setUp(self):
        self.client = mock.MagicMock()
        self.cache = {'tvshow:7': 3}
        for name, new in (('_set_tvshow_rating', mock.Mock()), ('_set_tvshow_details', mock.Mock()),
                          ('diff_show_text', mock.Mock(return_value={})),
                          ('_get_episodes_for_show', mock.Mock(return_value=[])),
                          ('_sync_one_episode', mock.Mock(return_value=False))):
            p = mock.patch.object(wrs, name, new)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(wrs.xbmc, 'sleep')
        p.start()
        self.addCleanup(p.stop)
        self.show = {'tvshowid': 7, 'title': 'Show', 'year': 2020, 'userrating': 0}

    def _sync(self):
        return wrs._sync_one_show(self.client, self.cache, self.show, None, None, 0, 1)

    def test_unresolvable_show_skips_its_episodes_too(self):
        self.cache.clear()
        self.client.resolve_show_by_external_id.return_value = None
        self.client.search_show.return_value = None
        self.assertEqual(self._sync(), (0, False, 0))

    def test_a_show_with_another_shows_title_is_never_written_to(self):
        self.client.get_show_details.return_value = {'title': 'Star Trek: Discovery'}
        self.client.search_show.return_value = None
        self.assertEqual(self._sync(), (0, False, 0))
        self._set_tvshow_rating.assert_not_called()
        self.assertNotIn('tvshow:7', self.cache)

    def test_rating_and_text_corrections_are_applied(self):
        self.client.get_show_details.return_value = {'title': 'Show', 'userRating': 9}
        self.diff_show_text.return_value = {'plot': 'new'}
        self.client.get_episode_list.return_value = []
        self.assertEqual(self._sync(), (0, True, 0))
        self._set_tvshow_rating.assert_called_once_with(7, 9)
        self._set_tvshow_details.assert_called_once_with(7, {'plot': 'new'})
        self.client.report_kodi_id.assert_called_once_with(3, 'tvshow', 7)

    def test_matching_rating_is_not_rewritten(self):
        self.show['userrating'] = 9
        self.client.get_show_details.return_value = {'title': 'Show', 'userRating': 9}
        self.client.get_episode_list.return_value = []
        self.assertEqual(self._sync(), (0, False, 0))
        self._set_tvshow_rating.assert_not_called()

    def test_episodes_are_matched_synced_and_counted(self):
        self.client.get_show_details.return_value = {'title': 'Show'}
        self.client.get_episode_list.return_value = [{'id': 100, 'season': 1, 'episode': 1}, {'id': 101, 'season': 1, 'episode': 2}]
        self._get_episodes_for_show.return_value = [
            {'episodeid': 1, 'season': 1, 'episode': 1, 'title': 'A'}, {'episodeid': 2, 'season': 1, 'episode': 2, 'title': 'B'},
            {'episodeid': 3, 'season': 9, 'episode': 9, 'title': 'Unknown to Chronicle'}]
        self._sync_one_episode.side_effect = [True, RuntimeError('rpc down')]
        with mock.patch.object(wrs.episode_numbers, 'match',
                               side_effect=lambda kodi_ep, chronicle: next((e for e in chronicle if e['episode'] == kodi_ep['episode'] and e['season'] == kodi_ep['season']), None)):
            synced, show_changed, changed = self._sync()
        self.assertEqual((synced, show_changed, changed), (1, False, 1))   # the failing one is logged, not counted; the unknown skipped

    def test_cancellation_stops_the_episode_loop(self):
        self.client.get_show_details.return_value = {'title': 'Show'}
        self.client.get_episode_list.return_value = [{'id': 100, 'season': 1, 'episode': 1}]
        self._get_episodes_for_show.return_value = [{'episodeid': 1, 'season': 1, 'episode': 1}]
        result = wrs._sync_one_show(self.client, self.cache, self.show, lambda: True, None, 0, 1)
        self.assertEqual(result, (0, False, 0))
        self._sync_one_episode.assert_not_called()


class TestSyncOneEpisode(unittest.TestCase):
    def test_flow(self):
        client = mock.MagicMock()
        client.get_episode_details.return_value = {'title': 'Pilot'}
        with mock.patch.object(wrs, '_build_state_updates', return_value={'playcount': 1}), \
                mock.patch.object(wrs, 'diff_episode_text', return_value={'plot': 'x'}), \
                mock.patch.object(wrs, '_set_episode_details') as write:
            self.assertTrue(wrs._sync_one_episode(client, {'title': 'Show'}, {'episodeid': 4, 'season': 1, 'episode': 1}, 100))
        write.assert_called_once_with(4, {'playcount': 1, 'plot': 'x'})
        client.report_kodi_id.assert_called_once_with(100, 'episode', 4)

    def test_no_details_or_no_change(self):
        client = mock.MagicMock()
        client.get_episode_details.return_value = None
        self.assertFalse(wrs._sync_one_episode(client, {'title': 'S'}, {'episodeid': 4}, 100))
        client.report_kodi_id.assert_not_called()
        client.get_episode_details.return_value = {'title': 'Pilot'}
        with mock.patch.object(wrs, '_build_state_updates', return_value={}), mock.patch.object(wrs, 'diff_episode_text', return_value={}), \
                mock.patch.object(wrs, '_set_episode_details') as write:
            self.assertFalse(wrs._sync_one_episode(client, {'title': 'S'}, {'episodeid': 4}, 100))
        write.assert_not_called()


class TestRun(unittest.TestCase):
    def setUp(self):
        for name, new in (('ChronicleClient', mock.Mock()), ('_get_all_movies', mock.Mock(return_value=[_movie(), _movie(movieid=6, title='Ronin')])),
                          ('_get_all_tvshows', mock.Mock(return_value=[{'tvshowid': 7, 'title': 'Show'}])),
                          ('_sync_one_movie', mock.Mock(return_value=True)),
                          ('_sync_one_show', mock.Mock(return_value=(3, True, 2)))):
            p = mock.patch.object(wrs, name, new)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        for target in ('load', 'save'):
            p = mock.patch.object(media_id_cache, target, return_value={})
            setattr(self, 'cache_' + target, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(wrs.xbmc, 'sleep')
        p.start()
        self.addCleanup(p.stop)

    def test_totals_and_saving_the_cache(self):
        progress = mock.Mock()
        result = wrs.run(progress_callback=progress)
        self.assertEqual(result, {'movies': 2, 'shows': 1, 'episodes': 3, 'errors': 0, 'movies_changed': 2, 'shows_changed': 1,
                                  'episodes_changed': 2, 'cancelled': False})
        self.cache_save.assert_called_once()
        self.assertEqual(progress.call_args_list[0][0], (0, 3, 'Heat (1995)'))

    def test_one_failing_item_is_counted_and_the_pass_continues(self):
        self._sync_one_movie.side_effect = [RuntimeError('rpc'), False]
        result = wrs.run()
        self.assertEqual((result['errors'], result['movies'], result['movies_changed']), (1, 1, 0))
        self.assertEqual(result['shows'], 1)

    def test_cancelling_during_movies_skips_the_shows_but_still_saves(self):
        flags = iter([False, True])
        result = wrs.run(is_cancelled=lambda: next(flags))
        self.assertEqual((result['movies'], result['shows'], result['cancelled']), (1, 0, True))
        self.cache_save.assert_called_once()

    def test_describe_result(self):
        get = lambda i: {32135: 'Updated {0}', 32136: ' ({0} errors)', 32162: '{0} movies', 32163: '{0} shows',
                         32164: '{0} episodes', 32166: '{0} errors'}[i]
        self.assertEqual(wrs.describe_result({'movies_changed': 2, 'episodes_changed': 1, 'errors': 1}, get),
                         'Updated 2 movies, 1 episodes (1 errors)')
        self.assertEqual(wrs.describe_result({'errors': 3}, get), '3 errors')
        self.assertIsNone(wrs.describe_result({}, get))


if __name__ == '__main__':
    unittest.main()
