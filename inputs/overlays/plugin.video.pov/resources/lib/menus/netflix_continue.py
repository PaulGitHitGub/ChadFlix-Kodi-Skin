"""Continue Watching, the top row of the Netflix-style skin's Movies and Shows tabs (R10.5, design B; route from the
cancelled R10.1).

One plugin call per tab, mode=netflix_continue_watching&kind=movies|shows. It reads POV's own progress store (the `progress` and `watched_status`
tables of the database POV writes when you stop or finish something) and lists, newest first, at most 20 titles of that kind:
  - kind=movies: movies stopped part-way and not finished since;
  - kind=shows: one tile per show: the episode stopped part-way, else the next aired episode after the last one watched.
    Shows last watched more than 60 days ago, shows marked dropped, finished shows and shows whose next episode
    has not aired are left out.
Each tile is the title itself (DBType movie / tvshow, built by POV's own list builders), so the skin's usual OK
opens the movie page or the show page. Extra properties for the skin's caption and bar:
  Netflix.CW.Kind     resume | next
  Netflix.CW.Caption  "[B]1h 19m left[/B]", "[B]S1: E3[/B] · 21m left", "Next · [B]S1: E4[/B]"
                      (the owner-approved A3 render)
  Netflix.CW.Percent  0-100 (0 = no bar)
  Netflix.CW.Season / Netflix.CW.Episode (shows)
The show's episode is chosen with exactly the rule the show page uses for its Play button
(menus/seasons.py Seasons.netflix_play_pointer and its aired-episode count), so the page opens on the tile's episode.
No network of its own: metadata comes from POV's cache (TMDb only the first time a title is seen); the dropped-shows
list is POV's own (local table, or its Trakt/MDbList cache) and is read for kind=shows only.
At the end it writes Netflix.ContinueWatching.Answer.<kind> = 'r:<revision>' ('e:<revision>' if it failed) so the skin's row coordinator settles an
EMPTY answer at once (instead of its 1.5 s "nothing arrived" wait).
"""
from datetime import datetime, timedelta, timezone
from threading import Thread
from time import monotonic

from modules import kodi_utils, settings

MAX_TILES = 20
KINDS = {'movies': ('movie',), 'shows': ('episode',)}
SHOW_DAYS = 60
GET_PROGRESS = 'SELECT db_type, media_id, season, episode, resume_point, curr_time, last_played, resume_id, title ' \
               'FROM progress WHERE db_type IN (?, ?) ORDER BY last_played DESC'
GET_WATCHED = 'SELECT db_type, media_id, season, episode, last_played FROM watched_status WHERE db_type IN (?, ?)'


def cutoff_value(watched_indicators, days, now=None):
    """The last_played text `days` ago, in the format the active database uses (see watched_cache.dt_formats)."""
    if watched_indicators == 1:
        return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    if watched_indicators == 2:
        return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ((now or datetime.now()) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')


def number(value):
    try: return float(value)
    except (TypeError, ValueError): return 0.0


def time_left(resume_point, curr_time, duration=0):
    """'1h 19m left' / '41m left' from a bookmark (POV stores the percent of (seconds - 5) and the seconds)."""
    percent, seconds = number(resume_point), number(curr_time)
    total = (seconds - 5) * 100 / percent if percent > 0 and seconds > 5 else number(duration)
    if total <= 0: return ''
    minutes = max(1, int(round((total - seconds) / 60.0)))
    hours, minutes = divmod(minutes, 60)
    if hours: return ('%dh %dm left' % (hours, minutes)) if minutes else '%dh left' % hours
    return '%dm left' % minutes


def aired_counts(meta, season_data, current_date, adjust_premiered_date):
    """Episodes per season that the show page lists as aired (menus/seasons.py Seasons.build_season_list)."""
    running = meta.get('total_aired_eps')
    counts = {}
    for item in season_data:
        number_, count = item.get('season_number'), item.get('episode_count')
        if not count: continue
        if number_ == meta.get('total_seasons'):
            episode_date, _ = adjust_premiered_date(item.get('air_date'), 0)
            unaired = not episode_date or current_date < episode_date
        else: unaired = count == 0
        if unaired: continue
        if number_ != 0:
            if isinstance(running, int):
                count = min(count, running)
                running -= count
        counts[number_] = count
    return counts


def show_pointer(meta, bookmarks, watched, show_specials, current_date, adjust_premiered_date, next_pointer):
    """(season, episode, kind, bookmark) the show page's Play button would start, or None when that episode is not
    listed as aired (finished show, next episode not out yet). `bookmarks`: this show's progress rows, newest first;
    `watched`: its watched_status rows. Same rule as Seasons.netflix_play_pointer."""
    season_data = [i for i in meta.get('season_data') or [] if show_specials or i.get('season_number') != 0]
    season_data.sort(key=lambda i: i.get('season_number') or 0)
    season_counts = {i['season_number']: i['episode_count'] for i in season_data}
    pointer = None
    for row in bookmarks:
        season, episode = row[2], row[3]
        if not isinstance(episode, int) or not 0 < episode <= (season_counts.get(season) or 0): continue
        try: progress = float(row[4])
        except (TypeError, ValueError): continue
        if 0 < progress < 100:
            pointer = (season, episode, 'resume', row)
            break
    if pointer is None:
        season, episode = max(((row[2], row[3]) for row in watched), default=(1, 0))
        season, episode, _ = next_pointer(meta, season, episode)
        pointer = (season, episode, 'next', None)
    counts = aired_counts(meta, season_data, current_date, adjust_premiered_date)
    if pointer[0] not in counts or not 0 < pointer[1] <= counts[pointer[0]]: return None
    return pointer


def candidates(progress_rows, watched_rows, dropped, show_cutoff):
    """Pure selection before metadata: [(last_played, kind, tmdb_id, rows...)], newest first.
    progress_rows: (db_type, media_id, season, episode, resume_point, curr_time, last_played, resume_id, title),
    newest first. watched_rows: (db_type, media_id, season, episode, last_played)."""
    movie_done = {}
    show_watched, show_bookmarks, show_last = {}, {}, {}
    for db_type, media_id, season, episode, last_played in watched_rows:
        if db_type == 'movie':
            movie_done[str(media_id)] = max(movie_done.get(str(media_id), ''), last_played or '')
        else:
            show_watched.setdefault(str(media_id), []).append((db_type, media_id, season, episode, last_played))
            show_last[str(media_id)] = max(show_last.get(str(media_id), ''), last_played or '')
    out, seen_movies = [], set()
    for row in progress_rows:
        media_id, last_played = str(row[1]), row[6] or ''
        if row[0] == 'movie':
            if media_id in seen_movies: continue
            seen_movies.add(media_id)
            if not 0 < number(row[4]) < 100: continue
            if movie_done.get(media_id, '') >= last_played: continue    # finished after this stop (or at it)
            out.append((last_played, 'movie', media_id, row))
        else:
            show_bookmarks.setdefault(media_id, []).append(row)
            show_last[media_id] = max(show_last.get(media_id, ''), last_played)
    dropped = {str(i) for i in dropped}
    for media_id, last_played in show_last.items():
        if media_id in dropped or last_played < show_cutoff: continue
        out.append((last_played, 'tvshow', media_id, (show_bookmarks.get(media_id, []), show_watched.get(media_id, []))))
    out.sort(key=lambda i: i[0], reverse=True)
    return out


def read_store(watched_indicators, kind=None):
    from caches.watched_cache import _database_connect, set_PRAGMAS
    dbcon = _database_connect(watched_indicators)
    try:
        dbcur = set_PRAGMAS(dbcon)
        types = KINDS.get(kind, ('movie', 'episode'))
        types = (types * 2)[:2]           # the queries take two db_type values
        progress = dbcur.execute(GET_PROGRESS, types).fetchall()
        watched = dbcur.execute(GET_WATCHED, types).fetchall()
    finally:
        try: dbcon.close()
        except Exception: pass
    return progress, watched


def build_items(kind=None):
    from caches.watched_cache import get_dropped_info_tv
    from indexers.metadata import tvshow_meta
    from menus.movies import Movies
    from menus.tvshows import TVShows
    from modules.utils import adjust_premiered_date, get_datetime, get_next_episode_pointer
    watched_indicators = settings.watched_indicators()
    progress, watched = read_store(watched_indicators, kind)
    has_shows = any(row[0] == 'episode' for row in progress) or any(row[0] == 'episode' for row in watched)
    dropped = get_dropped_info_tv(watched_indicators) if has_shows else ()
    chosen = candidates(progress, watched, dropped, cutoff_value(watched_indicators, SHOW_DAYS))
    if not chosen: return [], {'store': len(progress) + len(watched), 'movies': 0, 'shows': 0}
    current_date, user_info, specials = get_datetime(), settings.metadata_user_info(), settings.show_specials()
    params = {'action': 'netflix_continue_watching', 'exit_list_params': 'plugin://plugin.video.pov/'}
    movies = Movies(dict(params)) if any(i[1] == 'movie' for i in chosen) else None
    shows = TVShows(dict(params)) if any(i[1] == 'tvshow' for i in chosen) else None
    extra = {}

    def tile(position, entry):
        # Worker thread: an exception here would go to threading.excepthook (a traceback in kodi.log), not to the
        # route's own try. One bad title is dropped with one warning line; the rest of the row is unaffected.
        try: build_tile(position, entry)
        except Exception as error:
            extra.pop(position, None)
            __import__('xbmc').log('Netflix Continue Watching: skipped one title (%s)' % type(error).__name__, 2)

    def build_tile(position, entry):
        _, kind, media_id, data = entry
        if kind == 'movie':
            left = time_left(data[4], data[5])
            extra[position] = {'Netflix.CW.Kind': 'resume', 'Netflix.CW.Caption': '[B]%s[/B]' % left if left else '',
                               'Netflix.CW.Percent': str(max(1, min(99, int(number(data[4])))))}
            movies.build_movie_content(position, media_id)
            return
        meta = tvshow_meta('tmdb_id', media_id, user_info, current_date) or {}
        if not meta or meta.get('blank_entry'): return
        pointer = show_pointer(meta, data[0], data[1], specials, current_date, adjust_premiered_date,
                               get_next_episode_pointer)
        if not pointer: return
        season, episode, how, row = pointer
        code = 'S%d: E%d' % (season, episode)
        if how == 'resume':
            note, percent = time_left(row[4], row[5]) or 'Resume', str(max(1, min(99, int(number(row[4])))))
            caption = '[B]%s[/B] · %s' % (code, note)
        else: percent, caption = '0', 'Next · [B]%s[/B]' % code
        extra[position] = {'Netflix.CW.Kind': 'resume' if how == 'resume' else 'next', 'Netflix.CW.Percent': percent,
                           'Netflix.CW.Caption': caption,
                           'Netflix.CW.Season': str(season), 'Netflix.CW.Episode': str(episode)}
        shows.build_tvshow_content(position, media_id)

    # Shows past the cap are only looked at when an earlier one dropped out (finished, unaired, no metadata).
    position, start = 0, 0
    while start < len(chosen) and len(extra) < MAX_TILES:
        batch = chosen[start:start + MAX_TILES - len(extra)]
        threads = []
        for entry in batch:
            position += 1
            threads.append(Thread(target=tile, args=(position, entry)))
        start += len(batch)
        for thread in threads: thread.start()
        for thread in threads: thread.join()
    items = (movies.items if movies else []) + (shows.items if shows else [])
    items = [i for i in items if int(i[1].getProperty('pov_sort_order')) in extra]
    items.sort(key=lambda i: int(i[1].getProperty('pov_sort_order')))
    for _, listitem, _ in items:
        listitem.setProperties(extra[int(listitem.getProperty('pov_sort_order'))])
    counts = {'store': len(progress) + len(watched),
              'movies': sum(1 for i in items if extra[int(i[1].getProperty('pov_sort_order'))].get('Netflix.CW.Season') is None),
              'shows': sum(1 for i in items if extra[int(i[1].getProperty('pov_sort_order'))].get('Netflix.CW.Season') is not None)}
    return items[:MAX_TILES], counts


def continue_watching(params):
    started = monotonic()
    handle = int(kodi_utils.argv1())
    kind = params.get('kind') if params.get('kind') in KINDS else None
    items, counts, answer = [], {}, 'r'
    try: items, counts = build_items(kind)
    except Exception as error:
        answer = 'e'      # review 2: an error is not an empty row; the skin retries once instead of confirming "empty"
        __import__('xbmc').log('Netflix Continue Watching: failed: %s' % type(error).__name__, 2)
    kodi_utils.add_items(handle, items)
    kodi_utils.set_sort_method(handle, 'none')
    kodi_utils.set_content(handle, 'videos')
    kodi_utils.end_directory(handle, False)
    # Tells the skin's row coordinator this revision has been answered ('r:<revision>'; 'e:<revision>' when the route
    # failed), so an EMPTY answer settles at once instead of after its 1.5 s "nothing arrived" wait.
    kodi_utils.set_property('Netflix.ContinueWatching.Answer' + ('.' + kind if kind else ''), '%s:%s' % (answer, params.get('revision', '')))
    __import__('xbmc').log('Netflix Continue Watching (%s): %d tiles (%s movies, %s shows; %s store rows) in %.0f ms' % (
        kind or 'all', len(items), counts.get('movies', 0), counts.get('shows', 0), counts.get('store', 0),
        (monotonic() - started) * 1000), 1)
