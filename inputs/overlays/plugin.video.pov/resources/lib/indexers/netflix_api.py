"""TV genre rows with Trakt's exact genre vocabulary and POV's existing cache; R10.11a: the genre views (Movies and Shows)."""
import xbmc
import xbmcgui
from caches.main_cache import cache_object
from indexers.trakt_api import EXPIRES_2_DAYS, call_trakt

TV_GENRES = frozenset((
    'action', 'comedy', 'drama', 'thriller', 'horror', 'science-fiction',
    'animation', 'documentary', 'mystery', 'romance',
))


def netflix_tv_genres(genre_id, page_no):
    if genre_id not in TV_GENRES:
        raise ValueError('Unsupported TV genre')
    if isinstance(page_no, bool):
        raise ValueError('Invalid page')
    page_no = int(page_no)
    if not 1 <= page_no <= 1000:
        raise ValueError('Invalid page')
    params = {'limit': 20, 'page': page_no, 'genres': genre_id}
    request = {'path': '/shows/trending', 'params': params, 'with_auth': False, 'pagination': True}
    key = 'netflix_tv_genres_v1_%s_%s' % (genre_id, page_no)
    return cache_object(call_trakt, key, request, expiration=EXPIRES_2_DAYS)


# ------------------------------------------------------------------------------------------------------------------------------------
# R10.11a genre views (staged/anime-genre-design/DESIGN.md, owner decision 4). ONE route per tab, with fixed text the skin can store as is:
#   plugin://plugin.video.pov/?mode=build_movie_list&action=netflix_movie_views&view=<view>&genre_id=<id>&name=<label>&netflix_tab_genre=movies
#   plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_views&view=<view>&genre_id=<id>&name=<label>&netflix_tab_genre=shows
# (+ accumulate_to=N&netflix_grid_shell=1 for the grid, exactly like today's genre grid). genre_id is the value today's rows already use:
# the TMDb movie genre id (28 ...), company-41077 (A24, as R10.10), the Trakt show slug (action ...), or anime on either tab.
# POV builds every TMDb query here (view_query), from today's date: the skin's paths never change, nothing is encoded in XML and no date
# property can go stale when Kodi runs past midnight. The dates are in the query, so the cache key changes once a day (first request of a
# day = one cache miss, the same single call). Each request is one POV call, through entry.py Router.__call__ (Netflix.Pov.Running).
MOVIE_VIEWS = ('trending', 'new_releases', 'blockbusters', 'most_watched', 'top_rated')
TV_VIEWS = ('trending', 'new_series', 'most_watched', 'top_rated', 'complete_series')
DEFAULT_VIEW = 'trending'
TRAKT_VIEWS = {'trending': 'trending', 'most_watched': 'watched/weekly'}
ANIME_KEYWORD = ('with_keywords', '210024')   # the keyword POV's own anime lists use
# genre_id -> (Trakt slug, views served by Trakt, TMDb discover filter). A view not served by Trakt goes to TMDb discover.
MOVIE_GENRES = {
    '28': ('action', ('trending', 'most_watched'), ('with_genres', '28')),
    '35': ('comedy', ('trending', 'most_watched'), ('with_genres', '35')),
    '18': ('drama', ('trending', 'most_watched'), ('with_genres', '18')),
    '53': ('thriller', ('trending', 'most_watched'), ('with_genres', '53')),
    '27': ('horror', ('trending', 'most_watched'), ('with_genres', '27')),
    '878': ('science-fiction', ('trending', 'most_watched'), ('with_genres', '878')),
    '16': ('animation', ('trending', 'most_watched'), ('with_genres', '16')),
    '99': ('documentary', ('trending', 'most_watched'), ('with_genres', '99')),
    '9648': ('mystery', ('trending', 'most_watched'), ('with_genres', '9648')),
    '10749': ('romance', ('trending', 'most_watched'), ('with_genres', '10749')),
    # Trakt has no studio filter (studio_ids= is ignored: checked 2026-11-13), so A24 is TMDb only (company 41077, as R10.10's row).
    'company-41077': ('', (), ('with_companies', '41077')),
    # Trakt /movies/trending?genres=anime is empty (0 items, checked 2026-11-13); watched/weekly?genres=anime has ~1000.
    'anime': ('anime', ('most_watched',), ANIME_KEYWORD),
}
# TMDb TV has no Thriller / Horror / Romance genre: their discover views use TMDb keywords (DESIGN.md D, option b; counts checked
# 2026-11-13: New Series 23 / 14 / 76, Top Rated 43 / 63 / 116, Complete Series 534 / 375 / 6599). Trakt has every slug.
TV_GENRE_MAP = {
    'action': ('action', ('trending', 'most_watched'), ('with_genres', '10759')),
    'comedy': ('comedy', ('trending', 'most_watched'), ('with_genres', '35')),
    'drama': ('drama', ('trending', 'most_watched'), ('with_genres', '18')),
    'thriller': ('thriller', ('trending', 'most_watched'), ('with_keywords', '316362')),
    'horror': ('horror', ('trending', 'most_watched'), ('with_keywords', '315058')),
    'science-fiction': ('science-fiction', ('trending', 'most_watched'), ('with_genres', '10765')),
    'animation': ('animation', ('trending', 'most_watched'), ('with_genres', '16')),
    'documentary': ('documentary', ('trending', 'most_watched'), ('with_genres', '99')),
    'mystery': ('mystery', ('trending', 'most_watched'), ('with_genres', '9648')),
    'romance': ('romance', ('trending', 'most_watched'), ('with_keywords', '9840')),
    'anime': ('anime', ('trending', 'most_watched'), ANIME_KEYWORD),
}
PAGE_SIZE = 20
TRENDING_MIN = 10          # a Trakt Trending list that ends below this is topped up (to this many) from the genre's Most Watched
BLOCKBUSTER_PAGES = 2      # revenue order turns into zero-revenue filler after ~40 titles (DESIGN.md "quality trap")
TMDB_MAX_PAGES = 500       # TMDb discover refuses page > 500
THIN_TITLES = 10           # fewer titles than this, and no further page: state "thin" (the skin shows "Only N titles")
VIEW_CACHE_HOURS = 24
_view_started = [0.0]


class ViewError(Exception):
    pass


def view_today():
    """The day every date window is computed from (the device's local date). Tests replace this function."""
    from datetime import date
    return date.today()


def view_spec(media, view, genre_id):
    """(source, trakt slug, TMDb filter) for a view of a genre; ValueError for anything not in the tables."""
    views, table = (MOVIE_VIEWS, MOVIE_GENRES) if media == 'movies' else (TV_VIEWS, TV_GENRE_MAP) if media == 'shows' else ((), {})
    if view == 'all' and genre_id == 'anime' and media == 'shows': views = views + ('all',)   # R10.15b: "All Anime" (Shows only) = every anime series by popularity
    if view not in views: raise ValueError('unknown view %r for %s' % (view, media))
    if genre_id not in table: raise ValueError('unknown genre %r for %s' % (genre_id, media))
    slug, trakt_views, genre_filter = table[genre_id]
    return ('trakt' if view in trakt_views else 'tmdb'), slug, genre_filter


def view_query(media, view, genre_filter, today, page_no):
    """The TMDb discover path (properly URL-encoded) for one page of a view. Every date window comes from `today`."""
    from datetime import timedelta
    from urllib.parse import urlencode
    end, start90, year = today.isoformat(), (today - timedelta(days=90)).isoformat(), today.year
    if media == 'movies':
        path, rows = 'discover/movie', {
            # TMDb popularity: Trending for genres Trakt cannot filter (A24, anime movies)
            'trending': (('sort_by', 'popularity.desc'),),
            # theatrical (2, 3) or digital / physical (4, 5) release in the US in the last 90 days, by popularity
            'new_releases': (('region', 'US'), ('with_release_type', '2|3|4|5'), ('release_date.gte', start90), ('release_date.lte', end),
                             ('sort_by', 'popularity.desc')),
            # released from the year before last through this year, by revenue; >= 100 votes keeps known films (grid: 2 pages)
            'blockbusters': (('primary_release_date.gte', '%d-01-01' % (year - 2)), ('primary_release_date.lte', '%d-12-31' % year),
                             ('vote_count.gte', '100'), ('sort_by', 'revenue.desc')),
            # Most Watched for A24 (no Trakt filter): the most-voted titles released since last year
            'most_watched': (('primary_release_date.gte', '%d-01-01' % (year - 1)), ('sort_by', 'vote_count.desc')),
            'top_rated': (('vote_count.gte', '1000'), ('sort_by', 'vote_average.desc')),
        }
    else:
        path, rows = 'discover/tv', {
            'trending': (('sort_by', 'popularity.desc'),),
            'all': (('sort_by', 'popularity.desc'),),   # R10.15b: Shows anime only (view_spec lets nothing else ask for it)
            'new_series': (('first_air_date.gte', start90), ('first_air_date.lte', end), ('include_null_first_air_dates', 'false'),
                           ('sort_by', 'popularity.desc')),
            'top_rated': (('vote_count.gte', '500'), ('sort_by', 'vote_average.desc')),
            'complete_series': (('with_status', '3'), ('sort_by', 'popularity.desc')),
        }
    if view not in rows: raise ValueError('no TMDb query for %s %s' % (media, view))
    params = (('language', 'en-US'), genre_filter) + rows[view] + (('page', int(page_no)),)
    return '/3/%s?%s' % (path, urlencode(params))


TRAKT_DOWN = 'Netflix.View.TraktDown'   # Home property: epoch seconds of the last failed Trakt call of a fallback Trending row
TRAKT_DOWN_SECONDS = 120
_fallback_row = [False]                 # True while a fallback Trending row's Trakt page is being fetched


def _trakt_down():
    from time import time
    # Opus delta review: a stamp in the future (the wall clock was stepped back, e.g. NTP after boot) would read as "down" for as long as
    # the step; only 0 <= age < 120 s counts
    try: return 0 <= time() - float(xbmcgui.Window(10000).getProperty(TRAKT_DOWN) or 0) < TRAKT_DOWN_SECONDS
    except ValueError: return False


def _trakt_call(request):
    if _fallback_row[0] and _trakt_down(): return None   # Trakt just failed for another row: answer from the fallback at once
    _fail_fast()   # only on a cache miss: the bounded adapter needs the real HTTP stack, which a cache hit never loads
    result = call_trakt(request)
    if result is None and _fallback_row[0]:
        from time import time
        xbmcgui.Window(10000).setProperty(TRAKT_DOWN, str(time()))
    return result


def _trakt_page(media, view, slug, page_no):
    request = {'path': '/%s/%s' % (media, TRAKT_VIEWS[view]), 'params': {'limit': PAGE_SIZE, 'page': page_no, 'genres': slug},
               'with_auth': False, 'pagination': True}
    if media == 'shows' and view == 'trending' and slug in TV_GENRES:
        key = 'netflix_tv_genres_v1_%s_%s' % (slug, page_no)   # the same request and cache key as netflix_tv_genres (today's Shows rows)
    else:
        key = 'netflix_views_v1_%s_%s_%s_%s' % (media, view, slug, page_no)
    return cache_object(_trakt_call, key, request, expiration=EXPIRES_2_DAYS)


def _trakt_ids(media, result):
    if not result: raise ViewError('Trakt call failed')   # call_trakt answers None on any error (network, HTTP, missing key)
    data, total = result
    kind = 'movie' if media == 'movies' else 'show'
    return [i[kind]['ids'] for i in data], int(total or 0)


def _ids_key(ids):
    for name in ('trakt', 'tmdb', 'imdb', 'tvdb', 'slug'):
        if ids.get(name): return (name, ids[name])
    return ('ids', tuple(sorted((str(k), str(v)) for k, v in ids.items())))


_fail_fast_done = []
LIST_TIMEOUT = (3.05, 4)   # (connect, read) seconds for a view's list request


def _fail_fast():
    """POV's shared HTTP retry policy (Retry(total=None, ...)) retries a connection error without limit, so with the network off a POV
    call never returns (seen on the Mac: today's row 541 hung and Kodi could not quit). For the URLs the view routes fetch their LISTS
    from, mount a bounded adapter (requests picks the longest matching prefix): ONE attempt for connect and read errors, the timeout
    capped at LIST_TIMEOUT whatever the caller passes (TMDb 5 s, Trakt 10 s), one immediate status retry on 429/502/503/504 with any
    Retry-After header ignored. A dead network, a stalled server or a failing service answers within about 3-8 s and the route
    publishes state "error". Metadata calls and every other POV request keep POV's own policy (R10.14 bounds that). Called on a cache
    miss only, once per interpreter."""
    if _fail_fast_done: return
    _fail_fast_done.append(True)
    try:
        from modules import session as pov_session
        pov_session.requests   # the real HTTP stack (already needed: a request is about to be made)
        from requests.adapters import HTTPAdapter
        from urllib3.util import Retry
        from indexers import tmdb_api, trakt_api

        class ListAdapter(HTTPAdapter):
            def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
                return HTTPAdapter.send(self, request, stream=stream, timeout=LIST_TIMEOUT, verify=verify, cert=cert, proxies=proxies)

        retry = Retry(total=1, connect=0, read=0, status=1, status_forcelist=(429, 502, 503, 504), backoff_factor=0.5,
                      respect_retry_after_header=False)
        prefixes = [tmdb_api.base_url + '/3/discover/'] + [trakt_api.base_url + p for p in (
            '/movies/trending', '/movies/watched/', '/shows/trending', '/shows/watched/')]
        for prefix in prefixes: pov_session.session.mount(prefix, ListAdapter(max_retries=retry))
    except Exception as error:
        xbmc.log('Netflix View: bounded list requests not set: %s' % error, xbmc.LOGWARNING)


def _tmdb_ids(url):
    """A TMDb discover page reduced to what the list builders use (ids + paging), so the cache (POV's db and the Home-window memory
    copy) holds ~1 KB per page instead of the whole ~15 KB answer. None (not cached) when TMDb did not answer."""
    from indexers.tmdb_api import get_tmdb
    _fail_fast()
    data = get_tmdb(url)
    if not isinstance(data, dict) or 'results' not in data: return None
    return {'page': data.get('page'), 'total_pages': data.get('total_pages'), 'total_results': data.get('total_results'),
            'results': [{'id': i['id']} for i in data['results'] if i.get('id')]}


def view_page(media, view, genre_id, page_no, fallback=False):
    """One page of a view: (ids, total_pages, id_type). Raises ViewError when the source did not answer, ValueError on bad input.
    fallback=True (a row request): a Trending list whose Trakt call failed is answered from TMDb popularity instead (id type tmdb_id, no next page)."""
    if isinstance(page_no, bool): raise ValueError('Invalid page')
    page_no = int(page_no)
    if not 1 <= page_no <= TMDB_MAX_PAGES: raise ValueError('Invalid page')
    source, slug, genre_filter = view_spec(media, view, genre_id)
    fell_back = False
    if source == 'trakt':
        try:
            _fallback_row[0] = bool(fallback and view == 'trending' and page_no == 1)
            ids, total = _trakt_ids(media, _trakt_page(media, view, slug, page_no))
        except ViewError:
            if not (fallback and view == 'trending' and page_no == 1): raise
            xbmc.log('Netflix View: media=%s genre=%s Trakt trending failed, TMDb popularity used' % (media, genre_id), xbmc.LOGWARNING)
            ids, fell_back = None, True
        finally:
            _fallback_row[0] = False
        if ids is not None:
            if view == 'trending' and page_no == 1 and total <= 1 and len(ids) < TRENDING_MIN:
                # thin Trending (Trakt counts only what is being watched right now): top up from this week's Most Watched in the genre
                try: extra, _ = _trakt_ids(media, _trakt_page(media, 'most_watched', slug, 1))
                except Exception: extra = []
                seen = set(_ids_key(i) for i in ids)
                for i in extra:
                    if len(ids) >= TRENDING_MIN: break
                    if _ids_key(i) not in seen: seen.add(_ids_key(i)); ids.append(i)
                total = 1
            return ids, total, 'trakt_dict'
    url = view_query(media, view, genre_filter, view_today(), page_no)
    data = cache_object(_tmdb_ids, 'netflix_views_v1_' + url, url, expiration=VIEW_CACHE_HOURS)
    if not isinstance(data, dict) or 'results' not in data: raise ViewError('TMDb call failed')   # get_tmdb answers None on error
    total = min(int(data.get('total_pages') or 0), TMDB_MAX_PAGES)
    if view == 'blockbusters': total = min(total, BLOCKBUSTER_PAGES)
    if fell_back: total = 1   # the fallback list has no next page (its More tile would open the Trakt grid that just failed)
    if page_no > total: return [], total, 'tmdb_id'
    return [i['id'] for i in data['results']], total, 'tmdb_id'


def netflix_movie_views(view, genre_id, page_no):
    return view_page('movies', view, genre_id, page_no)


def netflix_tv_views(view, genre_id, page_no):
    return view_page('shows', view, genre_id, page_no)


def view_list(media, params, page_no, parse_accumulate_to, accumulate, trakt_key):
    """The list builders' netflix_*_views branch: (ids, id_type, new_page, error). Never raises: a bad request or a source that did not
    answer gives an empty list and error text, which view_report publishes as state "error". Row requests (no accumulate_to) load one
    page; the grid (accumulate_to=N) loads pages 1..N in one directory, Blockbusters at most 2. The next-page link carries view, genre_id,
    netflix_tab_genre and netflix_grid_items, so grid paging keeps working in place."""
    from time import monotonic
    _view_started[0] = monotonic()
    view, genre_id = params.get('view') or DEFAULT_VIEW, str(params.get('genre_id') or '')
    id_type = 'tmdb_id'
    try:
        source = view_spec(media, view, genre_id)[0]
        id_type = 'trakt_dict' if source == 'trakt' else 'tmdb_id'
        key = trakt_key if source == 'trakt' else (lambda k: k)
        carry = {k: params[k] for k in ('genre_id', 'netflix_tab_genre', 'netflix_grid_items') if params.get(k)}
        carry['view'] = view
        accumulate_to = parse_accumulate_to(params.get('accumulate_to')) if page_no == 1 else None
        if accumulate_to:
            if view == 'blockbusters': accumulate_to = min(accumulate_to, BLOCKBUSTER_PAGES)
            def fetch(page):
                ids, total, _ = view_page(media, view, genre_id, page)
                return ids, total
            ids, more = accumulate(fetch, key, accumulate_to)
            new_page = dict(carry, **{more[0]: str(more[1])}) if more else {}
        else:
            ids, total, id_type = view_page(media, view, genre_id, page_no, fallback=True)
            new_page = dict(carry, new_page=str(page_no + 1)) if total > page_no else {}
        return ids, id_type, new_page, ''
    except Exception as error:
        xbmc.log('Netflix View: media=%s view=%s genre=%s page=%s failed: %s' % (media, view, genre_id, page_no, error), xbmc.LOGWARNING)
        return [], id_type, {}, error.__class__.__name__


def view_report(media, params, count, new_page, error='', listed=None):
    """After the titles are built (page 1 / a grid load only): Home properties Netflix.View.Count.<media>.<view>.<genre_id> (titles in
    this answer) and Netflix.View.State.<...> (ok | thin | empty | error), and one log line with the time spent in the route.
    The row and its grid share these two properties (same media / view / genre): whichever loaded last wrote them, so Count is the
    size of the LAST answer (20 for a row, up to 60+ for a grid), never "the row's size". listed = ids the list call returned: when it
    returned ids but none could be built (metadata failed), the state is error (error='meta'), not empty."""
    from time import monotonic
    try: page = int(params.get('new_page', '1'))
    except (TypeError, ValueError): page = 0
    if page != 1: return
    view, genre_id = params.get('view') or DEFAULT_VIEW, str(params.get('genre_id') or '')
    more = bool(new_page)
    if not error and not count and listed: error = 'meta'
    state = 'error' if error else 'empty' if not count else 'thin' if count < THIN_TITLES and not more else 'ok'
    name = '%s.%s.%s' % (media, view, genre_id)
    xbmcgui.Window(10000).setProperty('Netflix.View.Count.' + name, str(count))
    xbmcgui.Window(10000).setProperty('Netflix.View.State.' + name, state)
    xbmc.log('Netflix View: media=%s view=%s genre=%s accumulate_to=%s count=%d more=%d state=%s elapsed=%.3f%s' % (
        media, view, genre_id, params.get('accumulate_to', ''), count, more, state, monotonic() - _view_started[0],
        ' error=%s' % error if error else ''), xbmc.LOGINFO)
