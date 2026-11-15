"""Keep first-page browse rows attached and activate one provider at a time."""
import sys
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import xbmc
import xbmcgui

MOVIES = (541, 542, 543, 544, 545, 546, 641, 643, 647, 648, 649)   # R10.10: 649 = the A24 row
SHOWS = tuple(range(561, 571))
# R10.5 Continue Watching: the first row of each tab, first in that tab's serial queue (Movies: right after Trending,
# which is the window's own list; Shows: before Shows Trending 592). For Customize Home only these lines name it.
CONTINUE_MOVIES, CONTINUE_SHOWS = 594, 595
CONTINUE_ROWS = {CONTINUE_MOVIES: 'movies', CONTINUE_SHOWS: 'shows'}
MOVIES = (CONTINUE_MOVIES,) + MOVIES
SHOWS = (CONTINUE_SHOWS,) + SHOWS
# Refetched (alone, through the serial refresh) only this long after a player has gone: POV writes the resume point
# up to ~1.5 s after the stop and then syncs it (R10.1 L4). The first load waits the same.
CONTINUE_AFTER_STOP_SECONDS = 3.0
# On showing a tab, a Continue Watching row loaded longer ago than this is refetched once (progress synced elsewhere).
CONTINUE_STALE_SECONDS = 1800.0
# POV's route writes 'r:<revision>' here when it has answered: an empty row settles at once (not after 1.5 s).
CONTINUE_ANSWER = 'Netflix.ContinueWatching.Answer.'
# Kodi applies the answered list a frame or two after POV writes the property: a REFETCH settles no sooner than this
# after the row was first seen present (R10.1 used .25 s for every load; on the Mac that alone added 0.27 s to the boot
# gate per empty row). A first load has no old tiles that could be mistaken for the answer, so it settles at once.
CONTINUE_ANSWER_HOLD = .1
# R10.5 review 1 / re-review F1: a row whose lease a tab/window switch dropped keeps its Kodi job running, and a hidden
# list never clears its IsUpdating flag by itself. Release only on proof: a Continue Watching row once POV answered its
# token, any other row once POV's own entry point says no call is running (POV_RUNNING, set/cleared by modules/entry.py).
# ORPHAN_CAP_SECONDS is a safety net only (a crashed interpreter). ORPHAN_START_GRACE: a just-dropped job may not
# have launched POV yet.
POV_RUNNING = 'Netflix.Pov.Running'
ORPHAN_CAP_SECONDS = 60.0
ORPHAN_START_GRACE = 3.0   # re-review: POV sets POV_RUNNING only once it reaches Router.__call__ (> 1 s on the Shield, reuse off)
# nit: a re-queued Continue Watching refresh step waits at most this long for its row to become present
CONTINUE_REQUEUE_SECONDS = 10.0
# R10.5.1: every add-on call this observer starts waits until no OTHER POV call has been running for this long (POV's
# Netflix.Pov.Running marker, set by every POV call incl. Kodi's own post-stop re-fetch burst). A marker held
# continuously for POV_QUIET_CAP_SECONDS is ignored (safety net, as ORPHAN_CAP_SECONDS).
POV_QUIET_SECONDS = 1.75
POV_QUIET_CAP_SECONDS = 60.0
# My Lists: Watchlists (533) first, then Saved Movies, then Saved Shows, one at a time.
SAVED = (533, 531, 532)
SAVED_READY_SECONDS = .25
# A changed My Lists row is refetched only after My Lists has been the front window, with no dialog and no
# player, for this long: the window's own restore-row script and a play/trailer started from a closing
# details page both launch right at that boundary and must not share it with a row refetch.
SAVED_QUIET_SECONDS = 1.0
SAVED_MARKER = 'Netflix.Browse.Window'
# Each My Lists row's content URL carries PREFIX+'Token.<row>'. The coordinator copies the revision the
# skin scripts bump into that token, one row at a time, so changed rows never refetch together.
SAVED_TOKENS = {533: 'Netflix.Watchlists.Revision', 531: 'Netflix.MyList.Revision', 532: 'Netflix.MyList.ShowsRevision'}
SAVED_FOCUS = (531, 532)  # rows whose focus is remembered/restored (unchanged: 533 has its own restore-row)
# R10.22 My Lists views: OK on a card opens that list as a grid inside the page (container 534), "See All" the tile grid (535). Each view's path is empty until
# this observer sets its gate (the skin's Netflix_MyLists_View_Path / _All_Path), so the user's click never starts an add-on call beside another one: the gate
# opens once nothing of this observer is in flight or updating, no foreign POV call is running or ran for the quiet window (the POV marker, pov_quiet, whose
# own 60 s cap is the only limit), and no dropped job is still running. NOTHING IS EVER FORCED (R10.21.1): a click that waits MYLISTS_FAIL_SECONDS shows
# "Couldn't Load ... Press Back to Try Again" (Netflix.MyLists.Failed) and keeps waiting; Back cancels it. While the gate is open and the view has not answered
# (its Loaded flag set by the plugin for THIS request, its container no longer updating, MYLISTS_GRACE seconds after the gate opened or the token changed so
# Kodi has started the job) every other start of this observer waits too: the next saved row and a saved refresh never run beside the view's call.
# The player's start parks an open list (534 holds video items: Kodi would refetch it at the post-stop re-init beside the rows, as R10.23 parks the Movies /
# Shows rows): its gate closes, the post-stop re-open goes through the same serial gate.
MYLISTS_VIEW = 'Netflix.MyLists.View'
MYLISTS_GO = {'list': 'Netflix.MyLists.Go.list', 'all': 'Netflix.MyLists.Go.all'}
MYLISTS_LOADED = {'list': 'Netflix.MyLists.Loaded.list', 'all': 'Netflix.MyLists.Loaded.all'}
MYLISTS_BOX = {'list': 534, 'all': 535}
MYLISTS_TOKEN = {'list': 'Netflix.MyLists.ViewToken', 'all': 'Netflix.MyLists.AllToken'}   # each view's own: a script reload of an open list never reloads the hidden See All grid (delta review 3, E1)
MYLISTS_FAILED = 'Netflix.MyLists.Failed'
MYLISTS_INDEX = 'Netflix.MyLists.ViewIndex'
MYLISTS_FAIL_SECONDS = 20.0
MYLISTS_GRACE = 1.0
MYLISTS_PENDING_CAP = 25.0
MYLISTS_OPEN = {'list': 'Netflix.MyLists.OpenSeq.list', 'all': 'Netflix.MyLists.OpenSeq.all'}   # each view's own path number, for the same reason (535 stays loaded and keeps re-reading its path while a list is open)
MYLISTS_STUCK_SECONDS = 3.0   # the fallback: a view whose container holds the requested list's items, not updating, this long after its gate opened, is answered (Kodi does not refetch an unchanged path)
MYLISTS_CANCEL_GRACE = 2.0   # after a Back / cancel of a view whose call had not answered: the abandoned call may not have set POV's marker yet (ORPHAN_START_GRACE's idea)
ROWS = {'movies': MOVIES, 'shows': SHOWS, 'saved': SAVED}
# Tab swap (staged/tabs-no-reload step 1). With skin bool Netflix.TabSwap on (CompactHome only) the
# Videos directory stays on the Movies Trending root, Window(Home).Property(Netflix.Tab) names the
# shown tab, and Shows Trending is the static list 592, loaded by this coordinator ahead of the Shows
# genre rows (same serial queue, so it never runs beside another add-on call). Off: nothing changes.
TAB = 'Netflix.Tab'
SWAP_CONDITION = 'Skin.HasSetting(Netflix.TabSwap) + Skin.HasSetting(Netflix.CompactHome)'
SHOWS_TRENDING = 592
SWAP_ROWS = {'shows': (CONTINUE_SHOWS, SHOWS_TRENDING) + SHOWS[1:]}
ALL_ROWS = MOVIES + SHOWS + SAVED + (SHOWS_TRENDING,)
ROW_MODE = {row: mode for mode, rows in ROWS.items() for row in rows}
ROW_MODE[SHOWS_TRENDING] = 'shows'
MODE_WINDOW = {'movies': 10025, 'shows': 10025, 'saved': 11165}
PREFIX = 'Netflix.Browse.'
SETTLE_SECONDS = 1.5
GENRE_ROWS = tuple(row for row in MOVIES + SHOWS if row not in CONTINUE_ROWS) + (SHOWS_TRENDING,)
MAX_RETRIES = 2
BOOT_PENDING = 'Netflix.MoviesBoot.Pending'
# R10.10 Customize Home. A SLOT is a row control id (a position on the page: 541... Movies, 561... Shows); what fills it is an ENTRY
# {id, label, path, media}: a stable id, the heading, the POV plugin path the row loads (used verbatim: nothing here derives a path from a
# genre id) and movie | show. The ten genre rows are only the default entries (HOME_TABS). The dialog (window 1179) writes five skin strings
# per tab code (M | S) and position k: Netflix.Home.Pos / Lbl / Path / Media / View / Off.<code>.<k> (all unset = today's page). This observer is
# the only code that turns them into rows: home_apply() publishes, per slot, the Home properties the skin reads (Netflix.Home.Shown.<slot> =
# the entry id, empty when hidden; .Label; .Path) and keeps rows(mode) down to the shown slots, so a hidden row is never enabled, loaded,
# waited for or refreshed. It runs at start and after the dialog closes (Netflix.Home.Changed), only while no row call is in flight, so
# loading stays serial through the same queue. Trending (591 / 592) and Continue Watching (594 / 595) are not slots: they never hide.
# Fallback hook: home_reject() hides an entry that cannot be a row (incomplete, duplicate, wrong media type, missing key), and for an
# entry that is not one of the defaults home_break() hides the row when its list comes back empty or does not answer within
# HOME_CUSTOM_SECONDS: such a row can never stay empty on the page or stall the serial queue / the boot gate (EXTENDING.md).
# A deferred entry (A24, and every custom entry) is not part of the boot gate: it is loaded by the same serial queue after the gate released.
HOME_CHANGED = 'Netflix.Home.Changed'
HOME_SHOWN, HOME_LABEL, HOME_PATH, HOME_APPLIED = 'Netflix.Home.Shown.', 'Netflix.Home.Label.', 'Netflix.Home.Path.', 'Netflix.Home.Applied.'
HOME_STALE = 'Netflix.Home.Stale.'    # a slot still holds the old entry's titles: the skin fades the row out until the new ones have loaded
HOME_BROKEN = 'Netflix.Home.Broken.'  # a custom entry (its signature) that came back empty or did not answer: hidden until it changes
HOME_FIELDS = ('Pos', 'Lbl', 'Path', 'Media', 'View', 'Off')
HOME_VIEW = 'Netflix.Home.View.'      # published per slot: the entry's view ('' = its own path); the hook for a per-row view tile (EXTENDING.md)
HOME_KIND = 'Netflix.Home.Views.'     # R10.15b, published per slot: '' (no view tile) | movies | shows | anime-movies | anime-shows (which picker the tile opens)
HOME_VIEWNAME = 'Netflix.Home.ViewName.'   # R10.15b, published per slot: the current view's Title Case name (tile value, header "Genre . View")
# R10.19: the genre drawer follows the Home layout. Published (only when the layout changes, from home_apply) per tab code (M | S) and drawer row n:
# Netflix.Menu.Label / Gid / Hid / Div .<code>.<n>, Netflix.Menu.DivAt.<code>. Rows shown at Home come first, in Customize Home's order; the genres hidden at Home
# follow (dimmer, after a hairline divider) and stay openable. Default entries only (a custom entry is not a genre).
MENU = 'Netflix.Menu.'
MENU_STATE = ('Netflix.GenreMenu.Genre',)   # the remembered drawer row is a slot id: meaningless once the order moved (ViewFocus is a views item id: unaffected)
MENU_ROWS_MAX = 11
MENU_SLOTS = (3751, 3752, 3753, 3754, 3755, 3756, 3757, 3758, 3759, 3760, 3765)   # drawer row n -> its button id
HOME_CUSTOM_SECONDS = 20.0
HOME_AFTER_GATE_SECONDS = 1.0   # review 5: deferred / re-pointed rows start no sooner than this after the boot gate released
HOME_RESET = ('Completed', 'NonEmpty', 'EmptyConfirmed', 'Failed', 'RetryAttempts')   # per-slot state that belonged to the old entry
HOME_TABS = {
    'movies': {'code': 'M', 'slots': (541, 542, 543, 544, 545, 546, 641, 643, 647, 648, 649), 'media': 'movie',
           'defaults': (
               ('Action', 'Action', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=28&name=Action&netflix_tab_genre=movies', 'movie', False),
               ('Comedy', 'Comedy', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=35&name=Comedy&netflix_tab_genre=movies', 'movie', False),
               ('Drama', 'Drama', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=18&name=Drama&netflix_tab_genre=movies', 'movie', False),
               ('Thriller', 'Thriller', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=53&name=Thriller&netflix_tab_genre=movies', 'movie', False),
               ('Horror', 'Horror', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=27&name=Horror&netflix_tab_genre=movies', 'movie', False),
               ('Sci-Fi', 'Sci-Fi', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=878&name=Sci-Fi&netflix_tab_genre=movies', 'movie', False),
               ('Anime', 'Anime', 'plugin://plugin.video.pov/?mode=build_movie_list&action=netflix_movie_views&genre_id=anime&name=Anime&netflix_tab_genre=movies', 'movie', False),
               ('Documentary', 'Documentary', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=99&name=Documentary&netflix_tab_genre=movies', 'movie', False),
               ('Mystery', 'Mystery', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=9648&name=Mystery&netflix_tab_genre=movies', 'movie', False),
               ('Romance', 'Romance', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=10749&name=Romance&netflix_tab_genre=movies', 'movie', False),
               ('A24', 'A24', 'plugin://plugin.video.pov/?mode=build_movie_list&action=tmdb_movies_genres&genre_id=company-41077&name=A24&netflix_tab_genre=movies', 'movie', True),
           )},
    'shows': {'code': 'S', 'slots': (561, 562, 563, 564, 565, 566, 567, 568, 569, 570), 'media': 'show',
           'defaults': (
               ('Action', 'Action', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=action&name=Action&netflix_tab_genre=shows', 'show', False),
               ('Comedy', 'Comedy', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=comedy&name=Comedy&netflix_tab_genre=shows', 'show', False),
               ('Drama', 'Drama', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=drama&name=Drama&netflix_tab_genre=shows', 'show', False),
               ('Thriller', 'Thriller', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=thriller&name=Thriller&netflix_tab_genre=shows', 'show', False),
               ('Horror', 'Horror', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=horror&name=Horror&netflix_tab_genre=shows', 'show', False),
               ('Sci-Fi', 'Sci-Fi', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=science-fiction&name=Sci-Fi&netflix_tab_genre=shows', 'show', False),
               ('Anime', 'Anime', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_views&genre_id=anime&name=Anime&netflix_tab_genre=shows', 'show', False),
               ('Documentary', 'Documentary', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=documentary&name=Documentary&netflix_tab_genre=shows', 'show', False),
               ('Mystery', 'Mystery', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=mystery&name=Mystery&netflix_tab_genre=shows', 'show', False),
               ('Romance', 'Romance', 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=netflix_tv_genres&genre_id=romance&name=Romance&netflix_tab_genre=shows', 'show', False),
           )},
}


# R10.15b: the view tile. The view of a slot is the skin string Netflix.Home.View.<code>.<k> (R10.10's sixth field; "0" / unset = the default, Trending).
# home_path() is the ONE place that turns (entry, view) into the route; everything else (publishing, signatures, the serial single-slot reload) is
# R10.10's. A custom entry has no views (kind ''), its path is used verbatim. The lists below are the picker's (Custom_1185) and the tile's names.
HOME_DEFAULT_VIEW = 'trending'
HOME_ANIME = 'Anime'
HOME_VIEW_NAMES = {
    'movies': (('trending', 'Trending'), ('new_releases', 'New Releases'), ('blockbusters', 'Blockbusters'), ('most_watched', 'Most Watched'),
               ('top_rated', 'Top Rated')),
    'shows': (('trending', 'Trending'), ('new_series', 'New Series'), ('most_watched', 'Most Watched'), ('top_rated', 'Top Rated'),
              ('complete_series', 'Complete Series')),
    'anime-movies': (('trending', 'Trending'), ('28', 'Action'), ('12', 'Adventure'), ('35', 'Comedy'), ('18', 'Drama'), ('14', 'Fantasy'),
                     ('27', 'Horror'), ('9648', 'Mystery'), ('10749', 'Romance'), ('878', 'Sci-Fi')),
    'anime-shows': (('all', 'All'), ('trending', 'Trending'), ('10759', 'Action & Adventure'), ('35', 'Comedy'), ('80', 'Crime'), ('18', 'Drama'),
                    ('10751', 'Family'), ('10762', 'Kids'), ('9648', 'Mystery'), ('10765', 'Sci-Fi & Fantasy')),
}


def home_kind(entry):
    """'movies' | 'shows' | 'anime-movies' | 'anime-shows' for an entry that has views (a default entry), '' for a custom one."""
    if entry.get('custom'):
        return ''
    movie = entry['media'] == 'movie'
    if entry['id'] == HOME_ANIME:
        return 'anime-movies' if movie else 'anime-shows'
    return 'movies' if movie else 'shows'


def home_valid_view(entry, view):
    """The stored view if this entry offers it and it is not the default ('' = default; '0' and unknown ids too, so a bad string can never break a row)."""
    view = (view or '').strip()
    if view == HOME_DEFAULT_VIEW or view not in dict(HOME_VIEW_NAMES.get(home_kind(entry), ())):
        return ''
    return view


def home_view_name(entry):
    """The Title Case name of the entry's current view ('' for an entry without views): the tile's value and the row header's second half."""
    return dict(HOME_VIEW_NAMES.get(home_kind(entry), ())).get(entry.get('view') or HOME_DEFAULT_VIEW, '')


def home_path(entry):
    """The route a slot loads: the entry's path, switched to its view (R10.11a's view routes; an anime genre uses POV's own anime genre routes).
    No view = Trending on every tab, Movies included."""
    kind = home_kind(entry)
    path = entry['path']
    if not kind:
        return path
    view = entry.get('view') or HOME_DEFAULT_VIEW
    movie = entry['media'] == 'movie'
    if kind.startswith('anime'):
        if view.isdigit():
            return (path.replace('action=%s&' % ('netflix_movie_views' if movie else 'netflix_tv_views'),
                                 'action=%s&' % ('tmdb_moviesanime_genres' if movie else 'tmdb_tvanime_genres'))
                    .replace('genre_id=anime', 'genre_id=' + view))
        return path.replace('&genre_id=anime', '&view=%s&genre_id=anime' % view)
    if movie:
        return path.replace('action=tmdb_movies_genres&', 'action=netflix_movie_views&view=%s&' % view)
    return path.replace('action=netflix_tv_genres&', 'action=netflix_tv_views&view=%s&' % view)


def home_missing(entry):
    """Hook for a future entry type that needs something this device may not have (an API key, an add-on): return a reason and the
    row is hidden instead of loading. The default entries need nothing."""
    return ''


def home_reject(tab, entry, used):
    """Why this entry cannot be a row on this tab ('' = it can)."""
    if not (entry['id'] and entry['label'] and entry['path']):
        return 'incomplete entry'
    if entry['id'] in used:
        return 'duplicate id'
    if entry['media'] != HOME_TABS[tab]['media']:
        return 'media %s on the %s tab' % (entry['media'], tab)
    if not entry['path'].startswith('plugin://'):
        return 'not a plugin path'
    return home_missing(entry)


def home_layout(tab, raw):
    """[(slot, entry, shown, reason)] for a tab. raw = one dict per position of the position's skin strings (keys id, label, path, media, off; ''
    = unset). Nothing set = today's page. An unset position inside a set list takes its default entry; a default entry with a string
    missing is completed from the table. An entry that home_reject() refuses is not shown; every other row keeps its place."""
    t = HOME_TABS[tab]
    defaults = t['defaults']
    if not any((r.get('id') or '').strip() for r in raw):
        raw = [{'view': (raw[i].get('view') if i < len(raw) else '')} for i in range(len(defaults))]   # R10.15b: keep the picker's views
    used, out = set(), []
    for i, slot in enumerate(t['slots']):
        r = raw[i] if i < len(raw) else {}
        d = defaults[i]
        entry = {'id': (r.get('id') or '').strip(), 'label': (r.get('label') or '').strip(), 'path': (r.get('path') or '').strip(),
                 'media': (r.get('media') or '').strip()}
        view = (r.get('view') or '').strip()
        view = '' if view == '0' else view
        if entry['id'] == 'Animation':   # R10.15b: the Animation slot is now Anime; a position saved by R10.10 carries the old id and label
            entry['id'], entry['label'], entry['path'] = HOME_ANIME, '', ''
        if not entry['id']:
            entry = {'id': d[0], 'label': d[1], 'path': d[2], 'media': d[3]}
        else:
            for e in defaults:   # a default entry with a string missing (damaged settings) is completed from the table
                if e[0] == entry['id']:
                    entry = {'id': e[0], 'label': entry['label'] or e[1], 'path': entry['path'] or e[2], 'media': entry['media'] or e[3]}
        entry['view'] = view
        # review 6: a default entry is matched on its id alone and always loads the table's path / media (a stored path from an older
        # release is migrated here), so a future change to a default path never turns every stored entry "custom"
        for e in defaults:
            if entry['id'] == e[0]:
                entry['path'], entry['media'] = e[2], e[3]
        entry['custom'] = not any(entry['id'] == e[0] for e in defaults)
        # not part of the boot gate: A24 (a default flagged so) and every custom entry load through the serial queue once the gate has released
        entry['defer'] = entry['custom'] or any(entry['id'] == e[0] and e[4] for e in defaults)
        entry['view'] = home_valid_view(entry, entry['view'])   # R10.15b: '' = default (Trending), so the signature of an unset row never changes
        reason = home_reject(tab, entry, used)
        used.add(entry['id'])
        out.append((slot, entry, not reason and (r.get('off') or '').strip() != '1', reason))
    return out


HOME_SLOTS = frozenset(s for t in HOME_TABS.values() for s in t['slots'])


def menu_rows(tab, layout):
    """R10.19: [(label, genre id, hidden at Home)] in drawer order: the shown default entries in layout order, then the hidden ones in layout order.
    A genre appears once (a damaged layout that names an id twice keeps the first), and a default no position names is appended as hidden (still openable)."""
    defaults = HOME_TABS[tab]['defaults']
    ids = {e[0] for e in defaults}
    gid = lambda path: (parse_qs(urlsplit(path).query).get('genre_id') or [''])[0]
    seen, rows = set(), []
    for slot, entry, shown, reason in layout:
        if entry.get('custom') or entry['id'] not in ids or entry['id'] in seen:
            continue
        seen.add(entry['id'])
        rows.append((entry['label'], gid(entry['path']), not shown))
    rows = [r for r in rows if not r[2]] + [r for r in rows if r[2]]
    return rows + [(e[1], gid(e[2]), True) for e in defaults if e[0] not in seen]



def home_focusable(api, row):
    """review 7: focus may only go to a slot that is shown and not stale (a hidden or re-pointed row draws nothing)."""
    if row not in HOME_SLOTS:
        return True
    return bool(api.prop(HOME_SHOWN + str(row))) and not api.prop(HOME_STALE + str(row))

# Grid page (staged/genre-grid): a genre opened from the genre menu or from a row's "More Titles" tile. The
# window directory is an empty POV shell (netflix_grid_shell=1) and the static list 593 holds the titles. This
# observer is the only thing that ever gives 593 a route: it builds it, applies it once the page is quiet,
# shows the list when the first load has arrived, lets it grow in place and recovers from empty answers.
GRID = 593
GRID_HOLD = 8593              # where focus rests while the grey first-load cards or "No titles" show
GRID_URL, GRID_FOR, GRID_READY, GRID_FAILED = 'Netflix.Grid.Url', 'Netflix.Grid.For', 'Netflix.Grid.Ready', 'Netflix.Grid.Failed'
GRID_RETRY = 'Netflix.Grid.Retry'        # ever-growing token; POV ignores it, Kodi sees a new route and fetches again
# staged/genre-over-root: the grid page is shown over the Movies/Shows root instead of being the window's directory.
# The skin puts the page path (the old shell route, netflix_grid_shell=1) in this Home property when a genre is
# picked and clears it on Back; the window stays on the root, so Back needs no add-on call and no Container.Update.
# Everything below that used to read the window's path reads Kodi.page() instead.
GRID_SHELL = 'Netflix.Grid.Shell'
VIDEOS_ROOT = 'Netflix.Videos.Root'      # movies | shows | '': the root the Videos window's directory is on
GRID_NAME = 'Netflix.Grid.Name'          # the page's name= (genre name fallback for the skin's grid title)
ROOT_ROUTES = (('build_movie_list', 'trakt_movies_trending'), ('build_tvshow_list', 'trakt_tv_trending'),
               ('build_tvshow_list', 'tmdb_tv_premieres'))
# Set on the Videos window itself. It survives leaving to My Lists/Search and coming back and is lost when the
# window is rebuilt (skin reload). The skin gives 593 no route without it, so a rebuilt window cannot start the
# grid load by itself beside its own directory load.
GRID_MARKER = 'Netflix.Grid.Window'
GRID_ROUTES = {('build_movie_list', 'tmdb_movies_genres'): 'movies', ('build_tvshow_list', 'netflix_tv_genres'): 'shows',
               ('build_movie_list', 'netflix_movie_views'): 'movies', ('build_tvshow_list', 'netflix_tv_views'): 'shows',   # R10.15a: R10.11a's view routes
               ('build_movie_list', 'tmdb_moviesanime_genres'): 'movies', ('build_tvshow_list', 'tmdb_tvanime_genres'): 'shows',   # R10.15b: the Anime row's genres
               ('build_movie_list', 'trakt_movies_trending'): 'movies', ('build_tvshow_list', 'trakt_tv_trending'): 'shows'}
# staged/trending-grid: the Trending rows' "More Titles" tile opens the same grid. These routes have no genre id.
GRID_NO_GENRE = (('build_movie_list', 'trakt_movies_trending'), ('build_tvshow_list', 'trakt_tv_trending'))
GRID_DROP = ('new_page', 'exit_list_params', 'netflix_grid_shell', 'netflix_grid_items', 'netflix_row_retry',
             'netflix_grid_retry', 'accumulate_to')
GRID_ORDER = ('mode', 'action', 'genre_id', 'name')
GRID_SAME_IGNORES = ('accumulate_to', 'netflix_grid_retry', 'name')   # the drawer and a row tile may name a genre differently
GRID_FIRST_PAGES = 3          # pages in the first load (about 60 titles); POV's own "more" tile names each next step
GRID_LAST_PAGES = 10          # POV's cap; after it the tile is a plain new_page one and the grid has ended
GRID_ROW_CAP = 400            # POV's row cap (ACCUMULATE_CAP): reaching it also ends the grid
GRID_AHEAD = 14               # start loading more when focus is within two rows (7 columns) of the end
GRID_FIRST_QUIET_SECONDS = .3 # quiet time after the shell directory arrived before the first load starts
GRID_QUIET_SECONDS = 1.0      # the page must be in front, with no dialog or player, this long before loading more
GRID_SETTLE_SECONDS = 1.0     # accept a first load whose "updating" flag was never observed after this long
GRID_EMPTY_SECONDS = 3.0      # an empty list this long after a load was asked for, and not loading, is an empty answer
GRID_FAIL_SECONDS = 25.0
# R10.21 (owner freeze 13:55): the first load of a page waits only for the tab under the grid (row_busy), for no running POV call and POV's
# quiet window. R10.21.1: nothing ever forces the start (the POV marker is a last-setter token: no marker does not prove a row's call ended).
# Safety net: after GRID_STUCK_SECONDS the page fails if nothing changed for GRID_STUCK_STILL_SECONDS (busy rows, marker token, wait reason) and no
# POV marker is up; while POV calls keep coming, or one call holds the marker, it waits on, up to POV_QUIET_CAP_SECONDS. GRID_STUCK names a
# failed page (the skin: "Couldn't Load Titles. Press Back to Try Again.").
GRID_STUCK_SECONDS = 20.0
GRID_STUCK_STILL_SECONDS = 8.0
GRID_STUCK = 'Netflix.Grid.Stuck'
GRID_STALE_SECONDS = 8.0      # a list that still held another genre is only trusted after it was seen reloading, or this long
GRID_RETRY_SECONDS = 30.0     # after a step that failed or brought nothing new, wait this long before asking again
# A load-more step is an add-on call; OK and Back start scripts through the keymaps, and this observer cannot
# see a key press coming. People press OK when the cursor has come to rest, so a step starts either while the
# cursor is still moving, or only after it has rested for a good while.
GRID_MOVING_SECONDS = .3
GRID_RESTED_SECONDS = 2.0
# Focus sent to a control that cannot take it leaves the window with no focused control at all (Kodi drops
# the old focus first). A SetFocus from this observer is carried out a moment later on Kodi's own thread, so
# the page can have changed in between (Back pressed on the grid: "Control 593 ... asked to focus, but it
# can't", Shield 2026-11-10 21:39:05). So the observer only corrects focus that has been wrong for a moment,
# checks the target is on screen first, and a safety net on the Movies/Shows page puts focus back when it is
# nowhere.
GRID_STRAY_SECONDS = .3       # focus on a control a grid page hides, this long, before the observer moves it
NOFOCUS_SECONDS = .3          # no focused control on the Movies/Shows page, this long, before it is restored
GRID_RETURN_SECONDS = 5.0     # how long after leaving a grid page the "where to land" note stays valid
GRID_MAX_RETRIES = 2          # reloads after an empty answer, and failed steps, per visit of the page
# Header tabs, the genre menu button and its drawer: focus there belongs to the viewer, never move it.
GRID_HEADER = frozenset((3730, 3732, 3733, 3734, 3735, 3736, 3761, 3765) + tuple(range(3750, 3761)) + tuple(range(3771, 3776)))   # R10.10: 3765 = the A24 entry of the drawer; R10.15a: 3771-3775 = the views column


def grid_query(url):
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def grid_url(path):
    """The POV route list 593 loads for a grid shell path, or '' when the path is not a grid genre route."""
    parts = urlsplit(path)
    if parts.scheme != 'plugin' or parts.netloc != 'plugin.video.pov':
        return ''
    query = parse_qsl(parts.query, keep_blank_values=True)
    values = dict(query)
    if 'netflix_grid_shell' not in values:
        return ''
    route = (values.get('mode'), values.get('action'))
    tab = GRID_ROUTES.get(route)
    if not tab or not (values.get('genre_id') or route in GRID_NO_GENRE):
        return ''
    # One spelling per genre, whichever way the page was opened (the genre menu and a "More Titles" tile order
    # their parameters differently): the same route means Kodi keeps the list instead of fetching it again.
    kept = sorted(((key, value) for key, value in query if key not in GRID_DROP and key != 'netflix_tab_genre'),
                  key=lambda pair: (GRID_ORDER.index(pair[0]) if pair[0] in GRID_ORDER else len(GRID_ORDER), pair[0]))
    # netflix_tab_genre selects POV's fast cached build; a movie "More Titles" tile does not carry it.
    kept += [('netflix_tab_genre', values.get('netflix_tab_genre') or tab), ('netflix_grid_items', '1'),
             ('accumulate_to', str(GRID_FIRST_PAGES))]
    return 'plugin://plugin.video.pov/?' + urlencode(kept)


def grid_with(url, **changes):
    """url with the given parameters replaced (or added at the end)."""
    query, seen = [], set()
    for key, value in grid_query(url):
        if key in changes:
            value = str(changes[key])
            seen.add(key)
        query.append((key, value))
    query += [(key, str(value)) for key, value in changes.items() if key not in seen]
    return 'plugin://plugin.video.pov/?' + urlencode(query)


def grid_same_genre(url, other):
    """True when two list routes show the same genre (page count, retry token and display name aside)."""
    strip = lambda value: sorted(pair for pair in grid_query(value) if pair[0] not in GRID_SAME_IGNORES)
    return bool(url) and bool(other) and strip(url) == strip(other)


def grid_pages(url):
    try:
        return int(dict(grid_query(url)).get('accumulate_to', ''))
    except ValueError:
        return 0


def grid_tile(url, tile_path):
    """What POV's last item is: ('more', pages) a larger accumulate step, ('page', 0) a plain next-page tile,
    ('', 0) a title (the genre has ended)."""
    values = dict(grid_query(tile_path))
    if 'accumulate_to' in values:
        try:
            step = int(values['accumulate_to'])
        except ValueError:
            return ('', 0)
        return ('more', step) if step > grid_pages(url) else ('page', 0)
    if 'new_page' in values and values.get('mode') in ('build_movie_list', 'build_tvshow_list'):
        return ('page', 0)
    return ('', 0)


def grid_tab(path):
    return 3732 if 'netflix_tab_genre=shows' in path else 3733


def is_root(path):
    """The Movies/Shows root (page 1) of the browse catalog: the only directory a grid is shown over."""
    parts = urlsplit(path)
    if parts.scheme != 'plugin' or parts.netloc != 'plugin.video.pov':
        return False
    query = parse_qs(parts.query)
    return (query.get('mode', [''])[0], query.get('action', [''])[0]) in ROOT_ROUTES and 'new_page' not in query


LOCK_DIR = 'special://temp/'
LOCK_NAME = 'netflix_browse.lock'
BOOT_FOCUS = 8990
BOOT_READY_SECONDS = .3
# Poll faster while the startup screen is up or a row is loading, so a finished
# row is noticed (and the next one started) within 50 ms instead of 150 ms.
FAST_TICK = .05
# TMDb Helper looks up ratings on every focus change and pushes them into the screen, which
# costs frames mid-scroll. Keep it paused on the Videos window while focus is moving and
# release it once focus has rested on one title for this long.
TMDB_PAUSE = 'TMDbHelper.ServicePause'
TMDB_REST_SECONDS = .8
# A title's details page needs that title's ratings, so the pause is released the moment one is open
# (and stays released while it is) even if focus has not rested yet, and whatever the override file
# says unless it says 'off'. The movie dialog is 11167; the series page is a Python dialog, whose id
# comes from the 13000-13099 range (13000 when it is the only one), with Videos still underneath.
TMDB_DETAILS = (11167,)
TMDB_PYTHON_WINDOWS = range(13000, 13100)
# Optional override for testing, read once a second: a number of seconds, 'never' (do not
# release on rows) or 'off' (do not pause at all). No file means the default above.
TMDB_REST_FILE = 'special://temp/netflix_tmdb_rest.txt'
IDLE_TICK = .15
BOOT_CATALOGS = ('movies', 'shows', 'movies')
BOOT_PATHS = {
    'movies': 'plugin://plugin.video.pov/?mode=build_movie_list&action=trakt_movies_trending&name=32458',
    'shows': 'plugin://plugin.video.pov/?mode=build_tvshow_list&action=trakt_tv_trending&name=32458',
}


class Kodi:
    def __init__(self):
        self.home = xbmcgui.Window(10000)
        self.hidden = frozenset()   # R10.10: slots turned off in Customize Home or taken off the page by home_break(); rows() leaves them out
        self.deferred = frozenset()   # R10.10: shown slots that are not part of the boot gate (A24, custom entries)

    def prop(self, key):
        return self.home.getProperty(key)

    def put(self, key, value):
        value = str(value)
        if self.prop(key) != value:
            self.home.setProperty(key, value)

    def clear(self, key):
        self.home.clearProperty(key)

    def visible(self, condition):
        return xbmc.getCondVisibility(condition)

    def number(self, label):
        try:
            return int(xbmc.getInfoLabel(label))
        except (TypeError, ValueError):
            return 0

    def count(self, row):
        return self.number('Container(%d).NumItems' % row)

    def updating(self, row):
        return self.visible('Container(%d).IsUpdating' % row)

    def window(self):
        return xbmcgui.getCurrentWindowId()

    def dialog(self):
        return xbmcgui.getCurrentWindowDialogId()

    def saved_marker(self, value=None):
        # A property on the My Lists window itself: kept across close/reopen, gone after a skin reload.
        try:
            window = xbmcgui.Window(MODE_WINDOW['saved'])
            if value is None:
                return window.getProperty(SAVED_MARKER)
            window.setProperty(SAVED_MARKER, value)
        except RuntimeError:
            pass
        return ''

    def grid_marker(self, value=None):
        # A property on the Videos window itself: kept while the window lives, gone after a skin reload.
        try:
            window = xbmcgui.Window(10025)
            if value is None:
                return window.getProperty(GRID_MARKER)
            window.setProperty(GRID_MARKER, value)
        except RuntimeError:
            pass
        return ''

    def videos_prop(self, key, value):
        try:
            window = xbmcgui.Window(10025)
            if value:
                window.setProperty(key, value)
            else:
                window.clearProperty(key)
        except RuntimeError:
            pass

    def cancel_boot_alarm(self):
        xbmc.executebuiltin('CancelAlarm(netflix_movies_boot_timeout,true)')

    def swap(self):
        return self.visible(SWAP_CONDITION)

    def rows(self, mode):
        # Rows owned by a mode right now. 592 only exists (is visible) while the swap is on.
        if mode in SWAP_ROWS and self.swap():
            rows = SWAP_ROWS[mode]
        else:
            rows = ROWS[mode]
        if mode in ('movies', 'shows') and not self.visible('Skin.HasSetting(Netflix.CompactHome)'):
            # R10.5 review 4: the Continue Watching include exists only with CompactHome; never wait on a missing row
            rows = tuple(row for row in rows if row not in CONTINUE_ROWS)
        # R10.10: minus the slots Customize Home turned off (they take no part in the boot gate, the serial queue,
        # retries or refreshes)
        return tuple(row for row in rows if row not in self.hidden) if self.hidden else rows

    def skin_string(self, name):
        return xbmc.getInfoLabel('Skin.String(%s)' % name)

    def gate_rows(self, mode):
        # R10.10: the rows the boot gate waits for: the shown rows minus the deferred ones (A24, custom entries); those load
        # through the same serial queue once the gate has released
        rows = self.rows(mode)
        return tuple(row for row in rows if row not in self.deferred) if self.deferred else rows

    def main_row(self, mode):
        if mode == 'saved':
            return 531
        if mode == 'shows' and self.swap():
            return SHOWS_TRENDING
        return 591 if self.visible('Skin.HasSetting(Netflix.CompactHome)') else 590

    def open_catalog(self, mode):
        if self.swap():
            # The directory stays on the Movies Trending root; only the shown tab changes.
            self.put(TAB, mode)
            return
        xbmc.executebuiltin('Container.Update("%s",replace)' % BOOT_PATHS[mode])

    def present(self, row):
        # Empty/loading rows use an explicit placeholder; a missing/inactive
        # control must never be mistaken for a successfully loaded empty list.
        return self.visible('Control.IsVisible(%d) | Control.IsVisible(%d)' % (row, row + 8000))

    def page(self):
        """The page the Videos window shows: a grid over the root (Netflix.Grid.Shell) or its own directory."""
        path = xbmc.getInfoLabel('Container.FolderPath')
        shell = self.prop(GRID_SHELL)
        return shell if shell and is_root(path) else path

    def mode(self):
        window = self.window()
        if window == 11165:
            return 'saved'
        if window != 10025:
            return ''
        path = urlsplit(self.page())
        query = parse_qs(path.query)
        if path.netloc != 'plugin.video.pov':
            return ''
        if 'netflix_grid_shell' in query:
            return ''   # a grid page (staged/trending-grid: a Trending grid has the root's own route)
        if query.get('mode') == ['build_movie_list'] and query.get('action') == ['trakt_movies_trending']:
            if 'new_page' not in query and self.prop(TAB) == 'shows' and self.swap():
                return 'shows'
            return 'movies'
        if query.get('mode') == ['build_tvshow_list'] and query.get('action') in (['trakt_tv_trending'], ['tmdb_tv_premieres']):
            return 'shows'
        return ''

    def enabled(self, row):
        if row in (531, 533):
            return self.prop(PREFIX + 'Enabled.%d' % row) == 'true'
        if row == 532:
            return self.prop(PREFIX + 'SavedShowsEnabled') == 'true'
        return self.prop('Netflix.CatalogGenre.Enabled.%d' % row) == 'true'

    def enable(self, row):
        key = PREFIX + ('SavedShowsEnabled' if row == 532 else 'Enabled.%d' % row) if row in SAVED else 'Netflix.CatalogGenre.Enabled.%d' % row
        self.put(key, 'true')

    def log(self, message):
        xbmc.log('Netflix Browse: ' + message, xbmc.LOGINFO)

    def focus(self, row, index):
        xbmc.executebuiltin('SetFocus(%d,%d,absolute)' % (row, index))


def restore(api, mode):
    if mode not in ROWS or api.mode() != mode:
        return
    try:
        selected = int(api.prop(PREFIX + mode + '.Row'))
    except ValueError:
        selected = 0
    rows = SAVED_FOCUS if mode == 'saved' else api.rows(mode)
    main = api.main_row(mode)
    valid = rows + (() if mode == 'saved' else (590, 591))
    # A remembered row can be hidden now (591 while the Shows tab is swapped in): focus on it would fail.
    def usable(row):
        return api.count(row) > 0 and api.visible('Control.IsVisible(%d)' % row) and home_focusable(api, row)
    if selected not in valid or not usable(selected):
        selected = next((row for row in (main,) + rows if usable(row)), 0)
    if not selected:
        first = next((row for row in rows if row != SHOWS_TRENDING and row not in CONTINUE_ROWS and home_focusable(api, row)), 0)
        if first:
            api.focus(8000 + first, 0)
        return
    try:
        index = int(api.prop(PREFIX + mode + '.Index.%d' % selected))
    except ValueError:
        index = 0
    api.focus(selected, max(0, min(index, api.count(selected) - 1)))


# Deferred serial refresh (R9.1-bursts). POV no longer runs UpdateLibrary(video,...) on this skin (it made Kodi
# re-run every loaded row in the same instant); after a part-way stop, a watched mark, a Trakt sync or "Reload
# widgets" it sets this property instead. Each page (movies, shows, My Lists) is then refreshed the next time it is
# the settled front page: the Videos list (when shown), then the loaded rows, focused row first, one plugin call at
# a time through the row lease, with a gap between calls.
REFRESH_REQUEST = 'Netflix.Browse.RefreshRequest'
REFRESH_DELAY_SECONDS = 2.0   # after the request (POV asks ~0.5-1.5 s after the stop, once the bookmark is written)
REFRESH_QUIET_SECONDS = 1.0   # the page must be settled (no loading, dialog, player, tab change) this long first
REFRESH_GAP_SECONDS = .3      # between the end of one refresh call and the start of the next
# The last request this observer took (Window(Home)), so a request sent while it restarts is still taken (review L3).
REFRESH_TAKEN = PREFIX + 'RefreshTaken'
# R10.23 post-stop park (see Coordinator.park): the rows still parked, space-separated (empty: none). Diagnostics only; the skin does not read it.
PARKED = PREFIX + 'Parked'

PROFILES_REPEAT_SECONDS = 5.0
_profiles_published = [float('-inf')]


def publish_profiles(api, now=None):
    """Publish Netflix.Profiles.* (netflix_profiles.py "list") in this interpreter instead of a RunScript from
    Home's onload, which started in the same instant as the skinvariables template job (R9.1-bursts). Called
    when the observer starts and when a boot gate opens (a profile switch reloads the skin without restarting
    the observer). Never raises: the profile header falls back to the Kodi profile name."""
    now = time.monotonic() if now is None else now
    if now - _profiles_published[0] < PROFILES_REPEAT_SECONDS:
        return
    _profiles_published[0] = now
    try:
        import netflix_profiles
        netflix_profiles.main(['netflix_profiles.py', 'list'])
    except Exception as error:
        api.log('profiles list failed: %s' % error)


class CatalogBootGate:
    """Visit both native catalogs behind the startup screen using the serial row loader."""
    def __init__(self, api):
        self.api = api
        self.started = None
        self.released_at = float('-inf')   # R10.10 review 5
        self.reset()

    def reset(self):
        # One observer can now live through several boots: a profile switch (LoadProfile) raises
        # Netflix.MoviesBoot.Pending again without restarting Python, so each boot starts fresh.
        self.phase = 0
        self.arrived = False
        self.switch_deadline = None
        self.ready_since = None
        self.empty_main_since = None

    def finish(self, reason, main=None):
        api = self.api
        api.clear(BOOT_PENDING)
        api.cancel_boot_alarm()
        elapsed = time.monotonic() - self.started
        self.started = None
        self.released_at = time.monotonic()   # R10.10 review 5
        api.log('catalog startup released reason=%s elapsed=%.3f' % (reason, elapsed))
        if (api.mode() == 'movies' and not api.visible('Player.HasMedia')
                and not api.visible('System.HasActiveModalDialog')):
            if main == 591 and api.count(CONTINUE_MOVIES) > 0:
                main = CONTINUE_MOVIES
            api.focus(main if main and api.count(main) > 0 else 3733, 0)

    def ready(self, mode, main, now):
        api = self.api
        if not api.present(main) or api.updating(main):
            self.empty_main_since = None
            return False
        if api.count(main) > 0:
            self.empty_main_since = None
        elif self.empty_main_since is None:
            self.empty_main_since = now
        if self.empty_main_since is not None and now - self.empty_main_since < SETTLE_SECONDS:
            return False
        for row in api.gate_rows(mode):   # R10.10: deferred rows (A24) are not waited for
            if api.prop(PREFIX + 'Completed.%d' % row) != 'true' or not api.present(row) or api.updating(row):
                return False
            if not (api.count(row) > 0
                    or api.prop(PREFIX + 'EmptyConfirmed.%d' % row) == 'true'
                    or api.prop(PREFIX + 'Failed.%d' % row) == 'true'):
                return False
        return True

    def tick(self, now):
        api = self.api
        if api.prop(BOOT_PENDING) != 'true':
            if self.started is not None:
                if api.number('System.CurrentControlID') == BOOT_FOCUS:
                    self.finish('external-release')
                else:
                    self.started = None
                    self.released_at = time.monotonic()   # R10.10 re-review: released without finish()
            return
        if self.started is None:
            self.started = now
            self.reset()
            publish_profiles(api, now)
            api.put(HOME_CHANGED, '1')   # R10.10 review 10: re-read Customize Home (another profile's skin strings)
            if api.swap():
                # A profile switch can raise the gate again with a stale tab; every boot starts on Movies.
                # (Not in reset(): that also runs when a watchdog restarts the observer mid-session.)
                api.put(TAB, 'movies')
        mode, window = api.mode(), api.window()
        if api.visible('Player.HasMedia'):
            self.finish('playback')
            return
        dialog = api.dialog()
        landing = api.visible('Window.IsVisible(1198)')
        busy = dialog in (10138, 10160) or window in (10138, 10160)
        if api.visible('System.HasActiveModalDialog') and not (
                busy or (landing and dialog in (0, 1198, 11198))):
            self.finish('modal')
            return
        if busy or landing or window == 10000 or api.visible('Container.IsUpdating'):
            self.ready_since = self.empty_main_since = None
            return
        expected = BOOT_CATALOGS[self.phase]
        if window != 10025:
            self.finish('left-catalog')
            return
        if mode != expected:
            self.ready_since = self.empty_main_since = None
            if self.arrived or (self.switch_deadline is not None and now >= self.switch_deadline):
                self.finish('left-catalog' if self.arrived else 'switch-timeout')
            elif not self.phase and mode:
                self.finish('left-catalog')
            return
        self.arrived = True
        if api.number('System.CurrentControlID') != BOOT_FOCUS:
            api.focus(BOOT_FOCUS, 0)
        main = api.main_row(mode)
        if not self.ready(mode, main, now):
            self.ready_since = None
        elif self.ready_since is None:
            self.ready_since = now
        elif now - self.ready_since >= BOOT_READY_SECONDS:
            api.log('catalog startup ready mode=%s main=%d rows=%d elapsed=%.3f' %
                    (mode, api.count(main), sum(api.count(row) > 0 for row in api.rows(mode)), now - self.started))
            if self.phase == len(BOOT_CATALOGS) - 1:
                self.finish('ready', main)
            else:
                self.phase += 1
                self.arrived = False
                self.switch_deadline = now + 15
                self.ready_since = self.empty_main_since = None
                api.open_catalog(BOOT_CATALOGS[self.phase])


class Coordinator:
    def __init__(self, api):
        self.api = api
        self.boot = CatalogBootGate(api)
        self.inflight = None
        self.pending = {}
        self.complete_modes = set()
        self.complete_rows = {}  # mode -> the row set that was complete (592 joins 'shows' with the swap)
        self.last_scan = float('-inf')
        self.any_busy = False
        self.last_mode = ''
        self.owner = None
        self.validation_due = 0
        self.validation_scan = 0
        self.validation_done = False
        self.zero_since = {}
        self.saved_ready = None
        self.saved_quiet = None
        self.view_since = None
        self.view_open_at = self.view_token = None
        self.view_player = False
        self.view_pending, self.view_pending_for = False, ''
        self.view_index = 0
        self.view_cancel_until = float('-inf')
        self.grid_return = None
        # The grid page last shown in the Videos window, '' once that window has shown another page. A grid
        # page that comes up with this still set was only covered for a while (details page, Search, playback)
        # and keeps its place; any other arrival is a new opening and starts on the first title.
        self.grid_seen = ''
        self.nofocus_since = None
        self.nofocus_tries = 0
        self.grid = None
        self.grid_armed = True   # a previous observer may have left a route armed: cleared on the first tick off a grid page
        self.rest_key = None
        self.rest_since = 0
        self.tmdb_paused = None
        self.tmdb_rest_seconds = TMDB_REST_SECONDS
        self.refresh_seen = None      # last RefreshRequest value seen (None until the first tick)
        self.refresh_pending = set()  # modes whose rows still owe a refresh
        self.refresh_titles = {}      # per mode: tmdb ids to refresh; None = every loaded row (review M3)
        self.refresh_focus = None     # (control, item) while waiting to start, and since when (review M4)
        self.refresh_moved = 0.0
        self.refresh_due = 0.0
        self.refresh_quiet = None
        self.refresh_queue = None     # (mode, [steps]) being worked through
        self.refresh_row = None       # row whose refresh lease is in flight
        self.refresh_step = None      # the Videos list refresh in flight
        self.refresh_last_end = float('-inf')
        self.player_last = float('-inf')  # R10.5: last tick a player was up (Continue Watching waits 3 s after it)
        self.continue_loaded = {}         # R10.5: row -> when it last finished loading (monotonic), for the 30-min rule
        self.home_custom = {}             # R10.10: slot -> True while it holds a custom (non-default) entry
        self.dropped = {}                 # R10.5 review 1: row -> when a tab/window switch dropped its lease
        self.requeued = {}                # R10.5: CW refresh step -> first time it was put back (row not present)
        self.pov_foreign_last = float('-inf')   # R10.5.1: last tick a POV call this observer did not start was seen
        self.pov_foreign_token = ''             # R10.5.1: that call's marker token, and since when it is held
        self.pov_foreign_since = 0.0
        self.pov_own_tokens = []                # R10.5.1: markers of the observer's own recent calls (ignored)
        self.pov_token_seen = ''                # R10.5.1 review 1: the marker value now, whoever owns it,
        self.pov_token_since = 0.0              # and since when it is held (any running interpreter blocks a start)
        self.pov_waiting = None                 # R10.5.1 review 3: when a gated start began to wait (for one log line)
        self.pov_last_foreign = ''              # R10.5.1 review 3: the last other POV call's marker (for that line)
        self.parked = set()                     # R10.23: rows parked when a player came up, not loaded again since
        self.park_player = False                # R10.23: a player was up on the previous tick
        self.park_at = 0.0                      # R10.23: when the rows were last parked
        self.park_started = {}                  # R10.23: mode -> when its first parked row was asked for again
        self.park_loaded = {}                   # R10.23: row -> when its parked row was last asked for again (refresh_steps)
        self.refresh_seen_at = float('-inf')    # R10.23: when the last refresh request was taken
        self.park_move = None                   # R10.23 review F8: (row, index) of a selection move waiting for the grid page to be in front

    def home_reset(self, tab, slot, stale=True):
        """A slot is about to hold another entry (or the same one coming back on): forget everything about its old content.
        Its Enabled flag goes, so the content URL is empty until the serial queue enables it again (the 'Loading titles...'
        state every row starts in). Retry tokens stay (monotonic). Nothing is called here."""
        api = self.api
        for key in HOME_RESET:
            api.clear(PREFIX + '%s.%d' % (key, slot))
        api.clear('Netflix.CatalogGenre.Enabled.%d' % slot)
        api.clear(PREFIX + '%s.Index.%d' % (tab, slot))
        self.zero_since.pop(slot, None)
        job = self.pending.get(tab)
        if job and job['row'] == slot:
            del self.pending[tab]
        api.clear(PREFIX + tab + '.Complete')
        self.complete_modes.discard(tab)
        if api.prop('Netflix.CatalogGenre.Loading') == str(slot):
            api.clear('Netflix.CatalogGenre.Loading')
        if stale:
            api.put(HOME_STALE + str(slot), '1')

    def home_forget(self, tab, slot):
        """Focus memory and the grid-Back target must not name a slot that is hidden or holds another entry now."""
        api = self.api
        if api.prop(PREFIX + tab + '.Row') == str(slot):
            api.clear(PREFIX + tab + '.Row')
        if api.prop('Netflix.Grid.Row') == str(slot):
            api.clear('Netflix.Grid.Row')

    def home_break(self, slot, why):
        """Fallback hook: a custom entry came back empty or did not answer. Take its row off the page (not visible, not in rows(), never
        refreshed) so it leaves no empty row and cannot stall the queue or the boot gate; it stays off until its entry changes."""
        api = self.api
        tab = ROW_MODE[slot]
        api.put(HOME_BROKEN + str(slot), api.prop(HOME_APPLIED + str(slot)) or '?')
        api.clear(HOME_SHOWN + str(slot))
        api.clear(HOME_STALE + str(slot))
        api.put(PREFIX + 'Completed.%d' % slot, 'true')
        api.hidden = frozenset(api.hidden | {slot})
        self.home_forget(tab, slot)
        api.clear(PREFIX + tab + '.Complete')
        self.complete_modes.discard(tab)
        api.log('home row %d hidden: %s' % (slot, why))

    def home_tick(self, now):
        """After the Customize Home dialog closed: apply its settings, but only while no row call is in flight (serial queue)."""
        api = self.api
        if not api.prop(HOME_CHANGED):
            self.home_wait = None
            return
        reason = ''
        if self.inflight or self.refresh_row is not None or self.refresh_step or self.grid_busy:
            reason = 'a row call is in flight (row %s)' % ((self.inflight or {}).get('row') or self.refresh_row or 'refresh/grid')
        elif self.orphan_running(now):
            # review 4: a job a switch dropped is still running in POV
            reason = 'a dropped job may still run in POV (dropped %s, marker %s)' % (sorted(self.dropped), api.prop(POV_RUNNING) or '-')
        else:
            busy = [row for row in ALL_ROWS if self.row_busy(row, api.mode(), now)]
            if busy:
                reason = 'rows still loading %s' % busy
            elif api.visible('System.HasActiveModalDialog'):
                reason = 'a dialog is open'
        if reason:
            if reason != getattr(self, 'home_wait', None):
                self.home_wait = reason
                api.log('home layout waits: %s' % reason)   # one line per new reason (the Shield check can see why)
            return
        self.home_wait = None
        self.home_apply(now)

    def menu_publish(self, tab, layout):
        """R10.19: publish the genre drawer's rows for this tab (see MENU). Runs from home_apply only (start, and after Customize Home closed); nothing runs per key press."""
        api = self.api
        code = HOME_TABS[tab]['code']
        rows = menu_rows(tab, layout)
        signature = repr(rows)
        known = getattr(self, 'menu_sig', None)
        if known is None:
            known = self.menu_sig = {}
        if known.get(tab) == signature:
            return
        first = known.get(tab) is None
        known[tab] = signature
        first_hidden = next((i for i, r in enumerate(rows) if r[2]), None)
        for n, (label, gid, hidden) in enumerate(rows, 1):
            api.put('%sLabel.%s.%d' % (MENU, code, n), label)
            api.put('%sGid.%s.%d' % (MENU, code, n), gid)
            for field, on in (('Hid', hidden), ('Div', first_hidden is not None and first_hidden > 0 and n - 1 == first_hidden)):
                key = '%s%s.%s.%d' % (MENU, field, code, n)
                api.put(key, '1') if on else api.clear(key)
        for n in range(len(rows) + 1, MENU_ROWS_MAX + 1):   # a shorter list leaves no stale row behind
            for field in ('Label', 'Gid', 'Hid', 'Div'):
                api.clear('%s%s.%s.%d' % (MENU, field, code, n))
        key = '%sDivAt.%s' % (MENU, code)
        if first_hidden is not None and first_hidden > 0:
            api.put(key, str(first_hidden + 1))
        else:
            api.clear(key)
        if not first:
            for key in MENU_STATE:   # the remembered row was a slot of the old order
                api.clear(key)
            # a view grid opened from the drawer reopens it on GridGenre (a slot id): follow the genre to its new slot, so Back highlights the right row
            if api.prop('Netflix.GenreMenu.Grid') == tab and api.prop('Netflix.GenreMenu.GridGenre'):
                new_slot = next((MENU_SLOTS[n] for n, r in enumerate(rows) if r[1] == api.prop('Netflix.GenreMenu.GridViewGenre')), None)
                api.put('Netflix.GenreMenu.GridGenre', new_slot) if new_slot else api.clear('Netflix.GenreMenu.GridGenre')
        api.log('genre menu order %s: %s' % (tab, ', '.join(('(%s)' if r[2] else '%s') % r[0] for r in rows)))

    def home_apply(self, now, startup=False):
        """Read the Customize Home skin strings, publish the layout and reset the slots whose entry changed or came back on."""
        api = self.api
        hidden, deferred, reloaded, dropped, lost_focus, refused = set(), set(), [], [], [], []
        focus = api.number('System.CurrentControlID')
        for tab, t in HOME_TABS.items():
            raw = []
            for k in range(1, len(t['slots']) + 1):
                strings = [api.skin_string('Netflix.Home.%s.%s.%d' % (f, t['code'], k)) for f in HOME_FIELDS]
                raw.append(dict(zip(('id', 'label', 'path', 'media', 'view', 'off'), strings)))
            layout = home_layout(tab, raw)
            self.menu_publish(tab, layout)   # R10.19: the genre drawer's order (a no-op unless it changed)
            for slot, entry, shown, reason in layout:
                signature = entry['id'] + '\t' + home_path(entry) + '\t' + entry['view']
                broken = api.prop(HOME_BROKEN + str(slot))
                if broken:
                    if broken == signature:
                        shown, reason = False, reason or 'came back empty earlier'
                    else:
                        api.clear(HOME_BROKEN + str(slot))
                self.home_custom[slot] = entry['custom']
                if entry['defer']:
                    deferred.add(slot)
                if reason:
                    refused.append((slot, reason))
                was = api.prop(HOME_SHOWN + str(slot))
                if shown:
                    if was and api.prop(HOME_APPLIED + str(slot)) == signature:
                        api.put(HOME_LABEL + str(slot), entry['label'])
                        continue   # nothing changed for this slot: whatever it holds (loaded or loading) stays
                    if api.updating(slot):
                        self.dropped[slot] = now   # review 4: its Kodi job keeps running (row_busy / orphan_running hold the queue)
                    self.home_reset(tab, slot, stale=not startup)
                    self.home_forget(tab, slot)
                    api.put(HOME_APPLIED + str(slot), signature)
                    api.put(HOME_LABEL + str(slot), entry['label'])
                    api.put(HOME_PATH + str(slot), home_path(entry))
                    api.put(HOME_VIEW + str(slot), entry['view'])
                    api.put(HOME_KIND + str(slot), home_kind(entry))
                    api.put(HOME_VIEWNAME + str(slot), home_view_name(entry))
                    api.put(HOME_SHOWN + str(slot), entry['id'])
                    reloaded.append(slot)
                    if focus in (slot, 8000 + slot):
                        lost_focus.append((tab, slot))
                else:
                    hidden.add(slot)
                    if was and api.updating(slot):
                        self.dropped[slot] = now   # review 4
                    if was:
                        # Turned off: it just disappears (its content is not touched, not refreshed while hidden).
                        api.clear(HOME_SHOWN + str(slot))
                        api.clear(HOME_STALE + str(slot))
                        self.home_forget(tab, slot)
                        dropped.append(slot)
                        if focus in (slot, 8000 + slot):
                            lost_focus.append((tab, slot))
        api.hidden = frozenset(hidden)
        api.deferred = frozenset(deferred)
        api.clear(HOME_CHANGED)
        if lost_focus and not startup:
            tab = lost_focus[0][0]
            trending = SHOWS_TRENDING if tab == 'shows' and api.swap() else 591
            if api.count(trending) > 0 and api.visible('Control.IsVisible(%d)' % trending):
                api.focus(trending, 0)
            else:
                xbmc.executebuiltin('SetFocus(%d)' % (3732 if tab == 'shows' else 3733))
        api.log('home layout: hidden=%s reloaded=%s dropped=%s refused=%s' % (sorted(hidden), reloaded, dropped, refused))

    def tmdb_details_open(self, window):
        dialog = self.api.dialog()
        return (window in TMDB_DETAILS or dialog in TMDB_DETAILS
                or window in TMDB_PYTHON_WINDOWS or dialog in TMDB_PYTHON_WINDOWS)

    def tmdb_rest(self, window, now):
        api = self.api
        if self.tmdb_details_open(window):
            # Release for the open details page. rest_key is kept: when the page closes over the same
            # title nothing re-pauses, and the first focus move after that pauses again.
            if self.tmdb_paused is not False:
                api.videos_prop(TMDB_PAUSE, '')
                self.tmdb_paused = False
            return
        if window != 10025:
            self.rest_key = None
            return
        control = api.number('System.CurrentControlID')
        key = (control, api.number('Container(%d).CurrentItem' % control))
        if self.tmdb_rest_seconds is None:
            if self.tmdb_paused is not False:
                api.videos_prop(TMDB_PAUSE, '')
                self.tmdb_paused = False
            return
        if key != self.rest_key:
            self.rest_key, self.rest_since = key, now
            if self.tmdb_paused is not True:
                api.videos_prop(TMDB_PAUSE, '1')
                self.tmdb_paused = True
        elif self.tmdb_paused and now - self.rest_since >= self.tmdb_rest_seconds:
            api.videos_prop(TMDB_PAUSE, '')
            self.tmdb_paused = False

    def tmdb_override(self, text):
        text = text.strip().lower()
        if text == 'off':
            self.tmdb_rest_seconds = None
        elif text == 'never':
            self.tmdb_rest_seconds = float('inf')
        else:
            try:
                self.tmdb_rest_seconds = max(.2, float(text))
            except ValueError:
                self.tmdb_rest_seconds = TMDB_REST_SECONDS

    def remember(self, mode):
        if not mode or self.api.visible('Container.IsUpdating'):
            return
        row = self.api.number('System.CurrentControlID')
        if row not in (SAVED_FOCUS if mode == 'saved' else self.api.rows(mode) + (590, 591)):
            return
        if row in (590, 591) and mode == 'shows' and self.api.swap():
            return  # hidden while the Shows tab is swapped in; the skin focuses the remembered row directly
        if row in self.parked and (self.api.count(row) == 0 or (self.inflight and self.inflight['row'] == row and not self.inflight.get('refresh'))):
            return  # R10.23: empty, or reloading from empty (Kodi then selects tile 1): keep the tile it had (park_settled goes back to it)
        self.api.put(PREFIX + mode + '.Row', row)
        self.api.put(PREFIX + mode + '.Index.%d' % row,
                     max(0, self.api.number('Container(%d).CurrentItem' % row) - 1))

    def view_busy(self, now):
        """R10.22 (delta review D1): True while an open My Lists view's own call is still to answer (gate open, its request-tied Loaded flag not set, its container
        still updating, MYLISTS_GRACE s after the gate opened or its token changed, MYLISTS_PENDING_CAP at most), or for MYLISTS_CANCEL_GRACE s after a view that had not
        answered was left. Read-only. Everything that can start a POV call in My Lists respects it: the saved rows and saved refresh (after view_tick in the tick) and
        refresh_settled (before it: a refresh must not start in the tick after the gate opened)."""
        api = self.api
        view = api.prop(MYLISTS_VIEW)
        gate = MYLISTS_GO.get(view)
        if gate and api.prop(gate):
            if self.view_open_at is None or api.prop(MYLISTS_TOKEN[view]) != self.view_token:
                return True    # opened before this observer looked, or just reloaded: view_tick has not stamped it yet
            waited = now - self.view_open_at
            answered = bool(api.prop(MYLISTS_LOADED[view])) and not api.updating(MYLISTS_BOX[view]) and waited >= MYLISTS_GRACE
            return not (answered or waited >= MYLISTS_PENDING_CAP)
        return now < self.view_cancel_until

    def view_stuck_answer(self, view, now):
        """Fallback for the case Kodi serves the open view from its old items without a fetch (nothing is updating, the plugin is never called, its loaded flag never
        comes): the view counts as answered when its container is not updating and its first item belongs to the list that was asked for (the plugin stamps every item with
        Netflix.Watchlist.ID), MYLISTS_STUCK_SECONDS after the gate opened. Sets only the flag the plugin would set: no call is made. A different list's items never match."""
        api = self.api
        if api.prop(MYLISTS_LOADED[view]) or self.view_open_at is None or now - self.view_open_at < MYLISTS_STUCK_SECONDS:
            return
        box = MYLISTS_BOX[view]
        if api.updating(box) or api.count(box) <= 0:
            return
        first = xbmc.getInfoLabel('Container(%d).ListItemAbsolute(0).Property(Netflix.Watchlist.ID)' % box)
        if first and (view == 'all' or first == api.prop('Netflix.MyLists.ViewID')):
            api.put(MYLISTS_LOADED[view], 'true')
            api.log('mylists view=%s: not fetched again (unchanged items of %s); counted as answered' % (view, first))

    def view_tick(self, now):
        """R10.22: open the load gate of the My Lists view that was asked for (Netflix.MyLists.View = list | all) when it is safe, and hold every other start of
        this observer while an open view's own call runs or an abandoned one may still be starting (view_busy). True = nothing else may start in this tick.
        Never forces (R10.21.1)."""
        api = self.api
        view = api.prop(MYLISTS_VIEW)
        gate = MYLISTS_GO.get(view)
        if self.view_pending and (not gate or view != self.view_pending_for or not api.prop(gate)):
            self.view_cancel_until = now + MYLISTS_CANCEL_GRACE    # a Back / cancel (or a park) while the view's call had not answered
            self.view_pending = False
        if not gate:
            self.view_since = self.view_open_at = None
            if api.prop(MYLISTS_FAILED):
                api.clear(MYLISTS_FAILED)
            return now < self.view_cancel_until
        box, token = MYLISTS_BOX[view], api.prop(MYLISTS_TOKEN[view])
        if api.prop(gate):
            self.view_since = None
            if self.view_open_at is None or token != self.view_token:
                self.view_open_at, self.view_token = now, token   # opened before this observer looked, or a script reloaded the open view (new token)
            self.view_stuck_answer(view, now)
            busy = self.view_busy(now)
            self.view_pending, self.view_pending_for = busy, view
            if view == 'list' and not busy:
                self.view_index = max(0, api.number('Container(534).CurrentItem') - 1)   # the selection, kept while the list is up: read after a player came up it is gone (the window changed)
            return busy
        if self.view_since is None:
            self.view_since = now
        own = bool(self.inflight) or any(api.updating(row) for row in SAVED)
        foreign = (self.orphan_running(now) or not self.pov_quiet(now) or now < self.view_cancel_until
                   or any(self.row_busy(row, 'saved', now) for row in ALL_ROWS))
        if own or foreign:
            if now - self.view_since >= MYLISTS_FAIL_SECONDS and api.prop(MYLISTS_FAILED) != view:
                api.put(MYLISTS_FAILED, view)
                api.log('mylists view=%s still waiting after %.0f s (%s)' % (view, now - self.view_since, 'own job' if own else 'POV busy'))
            return True
        api.clear(MYLISTS_FAILED)
        api.clear(MYLISTS_LOADED[view])      # (delta review D3) an answer that arrived while the gate was closed never counts
        # (Shield verify) a path that equals the last one is never fetched again by Kodi (a quick Back + reopen sets '' and the same path within a frame): every open has its
        # own path, by this value (an unknown parameter POV ignores), set BEFORE the gate; the plugin's answer must carry it
        api.put(MYLISTS_OPEN[view], str(time.time_ns()))
        api.put(gate, 'true')
        self.view_open_at, self.view_token = now, token
        self.view_pending, self.view_pending_for = True, view
        api.log('mylists view=%s gate opened after %.2fs' % (view, now - self.view_since))
        self.view_since = None
        return True

    def view_park(self, now):
        """R10.22 (review F5): a video player comes up while a list is open in My Lists: that list holds video items, so Kodi would fetch it again at the
        post-stop re-init beside the saved rows. Its gate closes now (an empty path: no job for it, the post-stop refetch is an empty-path one); the selection is
        remembered, and the serial gate opens again on the return (View stays), the timer puts focus back on that poster."""
        api = self.api
        playing = api.visible('Player.HasVideo')
        if playing and not self.view_player and api.prop(MYLISTS_VIEW) == 'list' and api.prop(MYLISTS_GO['list']):
            api.put(MYLISTS_INDEX, self.view_index)
            api.clear(MYLISTS_GO['list'])
            api.clear(MYLISTS_LOADED['list'])
            api.log('mylists: the open list is parked while a player is up')
        self.view_player = playing

    def saved_refresh(self, now):
        """Refetch one loaded My Lists row whose content changed. True while a refresh is running or was started."""
        api = self.api
        if self.orphan_running(now):
            return True   # R10.5 re-review F2: a job dropped by a switch is still running in POV
        if not self.pov_quiet(now):
            return True   # R10.5.1: wait for a quiet window (another POV call ran just now)
        for row in SAVED:
            if not (api.enabled(row) and self.completed(row)):
                continue
            want = api.prop(SAVED_TOKENS[row])
            if api.prop(PREFIX + 'Token.%d' % row) == want:
                continue
            if not api.present(row) or any(api.updating(other) for other in SAVED):
                return True
            job = self.lease(row, now)
            job['refresh'] = True
            self.inflight = job
            api.put(PREFIX + 'Token.%d' % row, want)
            api.log('refresh mode=saved row=%d (content changed)' % row)
            return True
        return False

    def refresh_settled(self, mode, window, now=None):
        now = time.monotonic() if now is None else now
        api = self.api
        if self.view_busy(now):
            return False   # R10.22 (delta review D1): never beside an open My Lists view's own call
        if not mode or window != MODE_WINDOW[mode] or self.inflight or self.grid_busy:
            return False
        if api.prop(BOOT_PENDING) == 'true' or api.prop(GRID_SHELL) or api.prop('Netflix.TabDwellTarget') \
                or api.prop('Netflix.TabArrival'):
            return False
        if api.visible('Player.HasMedia') or api.visible('System.HasActiveModalDialog') \
                or api.visible('Container.IsUpdating'):
            return False
        if api.prop(PREFIX + mode + '.Complete') != 'true':
            return False
        if api.prop('Netflix.Play.Loading') or api.visible(
                'System.HasAlarm(netflix_playback_cover) + !String.IsEmpty(Window(Home).Property(Netflix.PlaybackCover))'):
            return False   # R10.5 review 5: the transition cover or a play start is up
        if mode == 'saved' and self.orphan_running(now):
            return False   # R10.5 re-review F2: IsUpdating reads False outside Videos
        # R10.5 review 1: every row of every page (a refetch dropped by a tab switch is still running in Kodi)
        return not any(self.row_busy(row, mode, now) for row in ALL_ROWS)

    def refresh_steps(self, mode, window):
        api = self.api
        titles = self.refresh_titles.get(mode)
        steps = []
        # The Videos list: the Movies root, or the Shows root's own list with the tab swap off (review L1; with the
        # swap on the Videos window keeps the Movies root and Shows Trending is row 592).
        path = xbmc.getInfoLabel('Container.FolderPath')
        if (window == 10025 and api.visible('Control.IsVisible(591)') and is_root(path)
                and ('build_movie_list' if mode == 'movies' else 'build_tvshow_list') in path
                and self.holds(591, titles)):
            steps.append('window')
        rows = [row for row in api.rows(mode) if self.completed(row) and api.present(row)
                and row not in CONTINUE_ROWS
                and (api.count(row) > 0 if titles else (mode == 'saved' or api.count(row) > 0))
                and self.holds(row, titles)]
        focused = api.number('System.CurrentControlID')
        if focused in rows:
            rows.remove(focused)
            rows.insert(0, focused)
        first = [row for row in api.rows(mode) if row in CONTINUE_ROWS and self.completed(row) and api.present(row)]
        fresh = [row for row in first + rows if self.park_loaded.get(row, float('-inf')) >= self.refresh_seen_at]
        if fresh:
            # R10.23: these rows were parked and asked for again after the request (the post-stop return): they already show it
            first, rows = [row for row in first if row not in fresh], [row for row in rows if row not in fresh]
            api.log('park: the refresh skips rows loaded after the request (%s)' % ' '.join(str(row) for row in fresh))
        return first + steps + rows

    def holds(self, row, titles):
        """True when the list shows one of these tmdb ids (or when every row is wanted). In-process info labels only."""
        if not titles:
            return True
        if titles == {'continue-watching'}:
            return False   # R10.5: the 30-minute rule's marker; only the Continue Watching row (always included) runs
        for index in range(min(self.api.count(row), 60)):
            item = 'Container(%d).ListItemAbsolute(%d).' % (row, index)
            if (xbmc.getInfoLabel(item + 'UniqueID(tmdb)') in titles
                    or xbmc.getInfoLabel(item + 'Property(tmdb_id)') in titles):
                return True
        return False

    def refresh_tick(self, now, mode, window):
        """Deferred serial refresh. True while it owns the tick (a refresh call is starting or in flight)."""
        api = self.api
        request = api.prop(REFRESH_REQUEST)
        if self.refresh_seen is None:
            # First tick: the last request an earlier observer took ('=' + value); nothing yet = Kodi just started.
            taken = api.prop(REFRESH_TAKEN)
            self.refresh_seen = taken[1:] if taken else request
            if not taken:
                api.put(REFRESH_TAKEN, '=' + request)
        if request != self.refresh_seen:
            self.refresh_seen = request
            api.put(REFRESH_TAKEN, '=' + request)
            title = request.partition('|')[2].strip()
            for page in ROWS:
                if page not in self.refresh_pending:
                    self.refresh_titles[page] = set()
                if not title:
                    self.refresh_titles[page] = None
                elif self.refresh_titles[page] is not None:
                    self.refresh_titles[page].add(title)
            self.refresh_pending = set(ROWS)
            self.refresh_seen_at = now      # R10.23: parked rows asked for again after this are not refreshed for it (refresh_steps)
            self.refresh_due = now + REFRESH_DELAY_SECONDS
            self.refresh_queue = None
            self.refresh_quiet = None
            api.log('refresh requested: pages refresh one row at a time once settled (%s)' %
                    ('rows holding the title' if title else 'every loaded row'))
        playing = api.visible('Player.HasMedia')
        if playing:
            self.player_last = now
        if self.refresh_pending and playing:
            # A watched mark is sent during playback (past 80 %): start no sooner than the delay after the player.
            self.refresh_due = max(self.refresh_due, now + REFRESH_DELAY_SECONDS)
        if self.refresh_row is not None and not (self.inflight and self.inflight.get('serial_refresh')):
            api.log('refresh end row=%d' % self.refresh_row)
            self.refresh_row = None
            self.refresh_last_end = now
        step = self.refresh_step
        if step:
            updating = api.visible('Container.IsUpdating')
            step['seen'] = step['seen'] or updating
            if window == 10025 and (updating or (not step['seen'] and now - step['started'] < SETTLE_SECONDS)):
                return True
            # Container.IsUpdating does not follow the window's own fetch, so this normally ends on the 1.5 s timer
            # (observed_update=False). Safe: a longer fetch shows the modal busy dialog, which unsettles the page and
            # restarts the quiet time before the next call (review L2). Do not "tighten" it.
            self.refresh_step = None
            self.refresh_last_end = now
            api.log('refresh end videos list elapsed=%.3f observed_update=%s' % (now - step['started'], step['seen']))
            return True
        if self.refresh_row is not None or not self.refresh_pending or now < self.refresh_due:
            return False
        if mode not in self.refresh_pending or not self.refresh_settled(mode, window, now):
            self.refresh_quiet = None
            return False
        if mode in ('movies', 'shows') and now - self.player_last < CONTINUE_AFTER_STOP_SECONDS:
            self.refresh_quiet = None
            return False
        if self.refresh_quiet is None:
            self.refresh_quiet, self.refresh_focus = now, None
        # Like the grid's load-more step (review M4): OK/Back start keymap scripts this observer cannot see coming,
        # and people press OK once the cursor has come to rest. Start while it moves, or after it has rested a while.
        control = api.number('System.CurrentControlID')
        focus = (control, api.number('Container(%d).CurrentItem' % control))
        if focus != self.refresh_focus:
            self.refresh_focus, self.refresh_moved = focus, now
        if (now - self.refresh_quiet < REFRESH_QUIET_SECONDS or now - self.refresh_last_end < REFRESH_GAP_SECONDS
                or GRID_MOVING_SECONDS < now - self.refresh_moved < GRID_RESTED_SECONDS):
            return False
        if not self.pov_quiet(now):
            return False   # R10.5.1: another POV call (Kodi's post-stop re-fetch burst) ran in the last POV_QUIET_SECONDS
        if not self.refresh_queue or self.refresh_queue[0] != mode:
            self.refresh_queue = (mode, self.refresh_steps(mode, window))
        steps = self.refresh_queue[1]
        if not steps:
            self.refresh_pending.discard(mode)
            self.refresh_titles.pop(mode, None)
            for other in api.rows(mode):
                self.park_loaded.pop(other, None)   # R10.23 review F1: these reload times served this request only
            self.refresh_queue = None
            api.log('refresh finished mode=%s' % mode)
            return False
        item = steps.pop(0)
        gap = now - self.refresh_last_end
        if item == 'window':
            self.refresh_step = {'started': now, 'seen': False}
            xbmc.executebuiltin('Container.Refresh')
            api.log('refresh start mode=%s videos list gap=%.3f rested=%.1f (serial, %d left)' %
                    (mode, min(gap, 999), now - self.refresh_moved, len(steps)))
            return True
        if not self.completed(item) or not api.present(item):
            if item in CONTINUE_ROWS and self.completed(item):
                first = self.requeued.setdefault(item, now)
                if now - first < CONTINUE_REQUEUE_SECONDS:
                    steps.insert(0, item)   # R10.5: try again on a later tick (the queue is rebuilt if the page changes)
                else:
                    self.requeued.pop(item, None)
                    api.log('refresh skipped row=%d (not present for %d s)' % (item, CONTINUE_REQUEUE_SECONDS))
            return False
        self.requeued.pop(item, None)
        job = self.lease(item, now)
        job['refresh'] = job['serial_refresh'] = True
        self.inflight = job
        self.refresh_row = item
        if item in CONTINUE_ROWS:
            api.clear(CONTINUE_ANSWER + CONTINUE_ROWS[item])
            api.put(PREFIX + 'Token.%d' % item, str(time.time()))
            job['focus'] = api.number('System.CurrentControlID')
            job['first'] = self.first_title(item)   # review 3: focus moves to tile 1 only if this changes
        elif item in GENRE_ROWS:
            api.put(PREFIX + 'Retry.%d' % item, self.integer_prop('Retry', item) + 1)
        else:
            # My Lists: the row path carries Token.<row>; keep it equal to the revision so saved_refresh stays idle.
            value = str(time.time())
            api.put(SAVED_TOKENS[item], value)
            api.put(PREFIX + 'Token.%d' % item, value)
        api.log('refresh start mode=%s row=%d gap=%.3f rested=%.1f (serial, %d left)' %
                (mode, item, min(gap, 999), now - self.refresh_moved, len(steps)))
        return True

    def continue_settled(self, row, job, now):
        """R10.5: a Continue Watching row has (re)loaded. After a refetch with focus in the row, focus goes to the first
        tile when the front title changed (the title just watched is now first). If the refetch emptied the row under focus, Trending takes it
        (Timers.xml netflix_continue_gone is the backstop for any other way the row empties)."""
        api = self.api
        self.continue_loaded[row] = now
        if not job.get('refresh'):
            return
        focused = api.number('System.CurrentControlID')
        if focused == row and api.count(row) > 0 and self.first_title(row) != job.get('first'):
            api.focus(row, 0)   # review 3: only when the front title changed (a plain refetch keeps the cursor)
        elif job.get('focus') == row and api.count(row) == 0:
            trending = SHOWS_TRENDING if row == CONTINUE_SHOWS and api.swap() else 591
            if api.count(trending) > 0 and api.visible('Control.IsVisible(%d)' % trending):
                api.focus(trending, 0)
            else:
                xbmc.executebuiltin('SetFocus(%d)' % (3732 if row == CONTINUE_SHOWS else 3733))
            api.log('continue watching emptied under focus: focus to Trending')

    def park_tick(self, now):
        """R10.23: park the Movies / Shows rows on the tick a video player comes up (a title or a trailer, started from any window). Video only
        (review F9): audio or a player that saves no video resume point invalidates nothing, so parking for it would only cost calls."""
        playing = self.api.visible('Player.HasVideo')
        if playing and not self.park_player and self.api.prop(BOOT_PENDING) != 'true':
            self.park(now)
        self.park_player = playing

    def park(self, now):
        """R10.23 (notes/owner-y2k-back-2026-11-14 fix 1): after a stop Kodi saves the resume point and announces a library update; every list
        holding video items is invalidated and fetched again at the Videos window's next init, hidden ones included (22-24 POV calls, 2-3 at a
        time). Parked now, a row's content path is empty: Kodi starts no job for it while the title page / player is in front (the titles stay
        listed), and its post-stop refetch is an empty-path one (no POV call; the list empties to the loading look). The serial queue loads the
        shown tab's parked rows again, one at a time, once the page is back and settled (tick: park_pick; park_settled). Only Enabled and
        Completed go: retry tokens, the focus memory and the per-row answer state stay (unlike home_reset). Nothing is called here."""
        api = self.api
        parked = []
        for row in dict.fromkeys(api.rows('movies') + api.rows('shows')):
            if not api.enabled(row):
                continue   # never asked for, or parked already: its path is empty
            api.clear('Netflix.CatalogGenre.Enabled.%d' % row)
            api.clear(PREFIX + 'Completed.%d' % row)
            parked.append(row)
        if not parked:
            return
        job = self.inflight
        if job and job['row'] in parked:
            # its settle must not mark the row Completed with an empty path: drop the lease (its Kodi job may still run: the orphan rule)
            self.dropped[job['row']] = now
            if api.prop('Netflix.CatalogGenre.Loading') == str(job['row']):
                api.clear('Netflix.CatalogGenre.Loading')
            self.inflight = None
            api.log('park: dropped the job of row %d' % job['row'])
        self.parked.update(parked)
        self.park_at = now
        self.refresh_queue = None   # a deferred refresh builds its steps again once the page is back (refresh_steps skips rows loaded since)
        for mode in ('movies', 'shows'):
            api.clear(PREFIX + mode + '.Complete')
            self.complete_modes.discard(mode)
            self.park_started.pop(mode, None)
        api.put(PARKED, ' '.join(str(row) for row in sorted(self.parked)))
        api.log('park: %d rows parked while a player is up (%s); they load again one at a time on return' %
                (len(parked), ' '.join(str(row) for row in parked)))

    def park_pick(self, mode, remaining, early):
        """R10.23: the next parked row to load: the focused row (or its loading stand-in 8xxx / view tile 7xxx), the row a grid page was opened
        from (Back lands there), the remembered row, then Continue Watching, then page order. Continue Watching is skipped while `early` (under
        CONTINUE_AFTER_STOP_SECONDS after the player; POV writes the resume point then); the other rows do not wait for it. None: nothing yet."""
        api = self.api
        allowed = [row for row in remaining if not (early and row in CONTINUE_ROWS)]
        if not allowed:
            return None
        focused = api.number('System.CurrentControlID')
        wanted = [focused, focused - 8000, focused - 7000]
        ret = self.grid_return
        if ret and ret.get('origin') and ret['origin'][0] == mode:
            wanted.append(ret['origin'][1])
        try:
            wanted.append(int(api.prop(PREFIX + mode + '.Row')))
        except ValueError:
            pass
        wanted += [row for row in allowed if row in CONTINUE_ROWS]
        return next((row for row in wanted if row in allowed), allowed[0])

    def park_settled(self, row, now, job=None):
        """R10.23: a parked row is loaded again. Kodi lost its selection (the list was empty): put it back on the tile it had, as before the park
        (Continue Watching: the first, the title just watched; the row a grid page was opened from: that page's More Titles tile).
        - loaded under the grid page (park_under_grid): only the selection moves (Control.Move, no focus): Back focuses the row (the skin's onback);
        - focus on the row or its loading stand-in: focus to that tile;
        - Back from the grid page came before the row was back (focus on the tab or nowhere, within GRID_RETURN_SECONDS): focus to that tile."""
        if row not in self.parked:
            return
        api = self.api
        self.parked.discard(row)
        api.put(PARKED, ' '.join(str(r) for r in sorted(self.parked))) if self.parked else api.clear(PARKED)
        if api.count(row) == 0 or (job or {}).get('refresh'):
            return   # review F3: titles were still listed during the reload: Kodi kept the selection, and the viewer may have moved since
        mode = ROW_MODE[row]
        try:
            index = int(api.prop(PREFIX + mode + '.Index.%d' % row))
        except ValueError:
            index = 0
        ret = self.grid_return
        origin = (self.grid or {}).get('origin') or (ret or {}).get('origin')
        if origin and origin[0] == mode and origin[1] == row:
            index = origin[2]
        index = 0 if row in CONTINUE_ROWS else max(0, min(index, api.count(row) - 1))
        focused = api.number('System.CurrentControlID')
        current = api.number('Container(%d).CurrentItem' % row) - 1
        if (job or {}).get('under_grid') and self.grid:
            if current != index:
                self.park_move = (row, index)   # review F8: Control.Move goes to the focused window: applied by park_move_apply on a dialog-free tick
                self.park_move_apply()
            return
        back = bool(ret and ret.get('origin') and ret['origin'][1] == row) and focused in (0, 590, 591, 592, 3732 if mode == 'shows' else 3733)
        if focused in (row, row + 8000) or back:
            if focused != row or current != index:
                api.focus(row, index)
                api.log('park: row %d back with focus, item %d%s' % (row, index + 1, ' (Back from the grid page)' if back else ''))

    def park_move_apply(self):
        """R10.23 review F8: move the origin row's selection under the grid page, only while that page is in front (no dialog): Kodi sends
        Control.Move to the focused window. Dropped once the grid page is gone."""
        move, api = self.park_move, self.api
        if not move:
            return
        if not self.grid:
            self.park_move = None
            return
        if api.window() != 10025 or api.visible('System.HasActiveModalDialog'):
            return
        row, index = move
        self.park_move = None
        current = api.number('Container(%d).CurrentItem' % row) - 1
        if api.count(row) > index and current != index:
            xbmc.executebuiltin('Control.Move(%d,%d)' % (row, index - current))
            api.log('park: row %d back under the grid page, selection to item %d' % (row, index + 1))

    def park_under_grid(self, now, window):
        """R10.23: a grid page is in front and the row it was opened from is parked (the page was covered by a title / a player): load that row
        now, under the page (the rows stay "visible" to Kodi there), so Back lands on its tile as before. Only once the page has loaded, with no
        dialog, no player, no load-more step, no row busy and POV quiet (the page's load-more steps wait while a row job runs: grid_shown).
        One row; the others wait for the Movies / Shows page as usual."""
        self.park_move_apply()
        grid = self.grid
        if self.inflight or window != 10025 or not grid or grid['phase'] != 'ready' or grid['step'] or not grid.get('origin'):
            return
        api = self.api
        tab, row = grid['origin'][0], grid['origin'][1]
        if row not in self.parked or row not in api.rows(tab) or self.completed(row) or api.prop(BOOT_PENDING) == 'true':
            return
        if (api.visible('Player.HasMedia') or api.visible('System.HasActiveModalDialog') or api.visible('Container.IsUpdating')
                or api.updating(GRID) or api.prop('Netflix.GenreMenu.Open')):
            return
        if any(self.row_busy(other, tab, now) for other in ALL_ROWS) or self.orphan_running(now) or not self.pov_quiet(now):
            return
        job = self.lease(row, now)
        job['under_grid'] = True
        job['refresh'] = api.count(row) > 0   # review F2: titles still listed (no library update): settle on the refetch itself, not at once
        self.inflight = job
        self.park_started.setdefault(tab, now)
        self.park_loaded[row] = now
        api.put('Netflix.CatalogGenre.Loading', row)
        api.enable(row)
        api.log('park: row %d (where the grid page was opened) loads under the page' % row)

    def park_done(self, mode, now):
        """R10.23: every row of a page is loaded again (one log line; rows Customize Home took off the page are not waited for)."""
        started = self.park_started.pop(mode, None)
        if started is None:
            return
        api = self.api
        shown = set(api.rows('movies') + api.rows('shows'))
        self.parked &= shown   # a row Customize Home took off the page is not waited for
        api.put(PARKED, ' '.join(str(r) for r in sorted(self.parked))) if self.parked else api.clear(PARKED)
        api.log('park: %s rows back, %.1f s after the park (the first asked for after %.1f s)' % (mode, now - self.park_at, started - self.park_at))

    def row_busy(self, row, mode, now):
        """R10.5 (re-review F1/F3): is a row's directory job still running in Kodi?
        - not updating: no;
        - a row of the shown page: its IsUpdating flag is reliable (checked first, so a stale same-token answer cannot
          hide a refetch Kodi started by itself);
        - otherwise only a row whose lease a switch dropped (self.dropped) counts, at most ORPHAN_CAP_SECONDS: a
          Continue Watching row until POV answered its token (r:/e:), any other row while POV's entry point reports a
          running call (POV_RUNNING), or for ORPHAN_START_GRACE after the drop (POV may not have started yet)."""
        api = self.api
        if not api.updating(row):
            return False
        if mode and row in api.rows(mode):
            return True
        since = self.dropped.get(row)
        if since is None or now - since >= ORPHAN_CAP_SECONDS:
            return False
        if row in CONTINUE_ROWS:
            token = api.prop(PREFIX + 'Token.%d' % row)
            return api.prop(CONTINUE_ANSWER + CONTINUE_ROWS[row]) not in ('r:' + token, 'e:' + token)
        return bool(api.prop(POV_RUNNING)) or now - since < ORPHAN_START_GRACE

    def pov_watch(self, now):
        """R10.5.1: follow POV's Netflix.Pov.Running marker. The first marker seen while one of this observer's jobs is
        in flight (or a Videos list refresh step) is that job's own call; any other marker is a call Kodi or someone
        else started (the post-stop re-fetch burst) and restarts the quiet window."""
        token = self.api.prop(POV_RUNNING)
        if token != self.pov_token_seen:
            self.pov_token_seen, self.pov_token_since = token, now   # review 1: any token, own or not
        if not token:
            self.pov_foreign_token = ''
            return
        job = self.inflight
        if (job is not None and not job.get('own_token') and token not in self.pov_own_tokens
                and token != self.pov_foreign_token):
            job['own_token'] = token
        elif (self.refresh_step is not None and not self.refresh_step.get('own_token')
              and token not in self.pov_own_tokens and token != self.pov_foreign_token):
            self.refresh_step['own_token'] = token
        else:
            if token in self.pov_own_tokens or token == (job or {}).get('own_token') \
                    or token == (self.refresh_step or {}).get('own_token'):
                return
            if token != self.pov_foreign_token:
                self.pov_foreign_token, self.pov_foreign_since = token, now
            self.pov_foreign_last, self.pov_last_foreign = now, token
            return
        self.pov_own_tokens = (self.pov_own_tokens + [token])[-8:]

    def pov_quiet(self, now):
        """R10.5.1: True when no POV call other than this observer's own has been seen for POV_QUIET_SECONDS. Not during
        the boot gate. A marker held for POV_QUIET_CAP_SECONDS without a break is ignored (a crashed interpreter)."""
        if self.api.prop(BOOT_PENDING) == 'true':
            return True
        # review 1: the gate sites run with nothing of ours in flight, so ANY marker is a running interpreter (an "own"
        # token included: a job that never made its own call, an abandoned one); the cap still applies
        token = self.api.prop(POV_RUNNING)
        if token and now - self.pov_token_since < POV_QUIET_CAP_SECONDS:
            quiet = False
        elif self.pov_foreign_token and now - self.pov_foreign_since >= POV_QUIET_CAP_SECONDS:
            quiet = True
        else:
            quiet = now - self.pov_foreign_last >= POV_QUIET_SECONDS
        if not quiet:
            if self.pov_waiting is None:
                self.pov_waiting = now
        elif self.pov_waiting is not None:
            # review 3: one line when a gated start goes ahead after waiting (the Shield check can see the gate engage)
            self.api.log('quiet window: waited %.2f s (last other POV call %s)' % (now - self.pov_waiting,
                                                                                    self.pov_last_foreign or '-'))
            self.pov_waiting = None
        return quiet

    def orphan_running(self, now):
        """R10.5 re-review F2: with My Lists / Search in front a dropped job's IsUpdating reads False, so the saved paths
        wait while any drop is recent AND POV reports a running call (or the drop is under ORPHAN_START_GRACE old)."""
        recent = [since for since in self.dropped.values() if now - since < ORPHAN_CAP_SECONDS]
        if not recent:
            return False
        if self.api.prop(POV_RUNNING):
            return True
        return any(now - since < ORPHAN_START_GRACE for since in recent)

    @staticmethod
    def first_title(row):
        """R10.5: the tmdb id of the row's first tile ('' when empty)."""
        item = 'Container(%d).ListItemAbsolute(0).' % row
        return xbmc.getInfoLabel(item + 'UniqueID(tmdb)') or xbmc.getInfoLabel(item + 'Property(tmdb_id)')

    def continue_stale(self, mode, now):
        """R10.5: a tab was just shown. A Continue Watching row loaded more than 30 minutes ago is refetched once,
        alone, through the serial refresh (progress POV synced from another device). No call here."""
        row = CONTINUE_MOVIES if mode == 'movies' else CONTINUE_SHOWS
        if not self.completed(row) or row not in self.api.rows(mode):
            return
        loaded = self.continue_loaded.get(row)
        if loaded is None:      # loaded before this observer started: count from now
            self.continue_loaded[row] = now
            return
        if now - loaded < CONTINUE_STALE_SECONDS or mode in self.refresh_pending:
            return
        self.continue_loaded[row] = now
        for other in self.api.rows(mode):
            self.park_loaded.pop(other, None)   # R10.23 review F1: no request is pending here: an older park reload must not cancel this refetch
        self.refresh_pending.add(mode)
        self.refresh_titles[mode] = {'continue-watching'}   # no row holds this: only the Continue Watching row runs
        self.refresh_due = now
        self.refresh_queue = None
        self.refresh_quiet = None
        self.api.log('refresh requested: continue watching (%s) older than %d min' % (mode, CONTINUE_STALE_SECONDS // 60))

    @property
    def grid_busy(self):
        grid = self.grid
        return bool(grid) and (grid['phase'] in ('wait', 'loading') or bool(grid['step']))

    def grid_origin(self, path):
        """Where a "More Titles" page was opened from: (tab, row, index of the tile), read while the row
        observer's own memory of the Movies/Shows page is still untouched. None for a genre-menu page."""
        api = self.api
        if 'new_page=' not in path:
            return None
        tab = 'shows' if (api.swap() and api.prop(TAB) == 'shows') or 'mode=build_tvshow_list' in path else 'movies'
        try:
            row = int(api.prop(PREFIX + tab + '.Row'))
            index = int(api.prop(PREFIX + tab + '.Index.%d' % row))
        except ValueError:
            return None
        # staged/trending-grid: a Trending tile is in 592 (Shows, tab swap) or the window's own list 591.
        return (tab, row, index) if row in GENRE_ROWS + (590, 591) else None

    def grid_off(self, now=None):
        """Not on a grid page (or a player is up): 593 gets no route, so Kodi cannot load it by itself."""
        if self.grid is not None and now is not None:
            self.grid_return = {'left': now, 'origin': self.grid['origin'], 'tab': grid_tab(self.grid['path'])}
        if self.grid is not None and self.grid.get('stuck'):
            self.api.clear(GRID_STUCK)   # R10.21 (review F3): no stale "Couldn't Load Titles." when the page is opened again
        self.grid = None
        if self.grid_armed:
            self.api.clear(GRID_FOR)
            self.grid_armed = False

    def grid_token(self):
        api = self.api
        try:
            token = int(api.prop(GRID_RETRY)) + 1
        except ValueError:
            token = 1
        api.put(GRID_RETRY, token)
        return token

    def grid_tick(self, now, window):
        """Drive list 593 on a grid shell page. True when the window is on such a page."""
        api = self.api
        path = api.page() if window == 10025 else ''
        if 'netflix_grid_shell=' not in path:
            if (path and self.grid_seen and not api.visible('System.HasActiveModalDialog')
                    and not api.visible('Container.IsUpdating')):
                # The Videos window itself shows another page: the grid page was left, not just covered.
                # Its next opening starts again from the grey cards and the first title.
                self.grid_seen = ''
                api.clear(GRID_READY)
            self.grid_off(now)   # also when a dialog (busy, drawer) is in front at that moment
            return False
        if api.visible('Player.HasMedia'):
            # Kodi marks static lists for a refresh when playback starts and stops, and re-enters this window
            # afterwards (its own shell load). With no route the list keeps its titles and loads nothing.
            self.grid_off()
            return True
        grid = self.grid
        if api.visible('System.HasActiveModalDialog') or api.visible('Container.IsUpdating'):
            if grid:
                grid['quiet'] = None
            return True
        if not grid or grid['path'] != path:
            grid = self.grid = self.grid_new(path, now)
        if grid['quiet'] is None:
            grid['quiet'] = now
        quiet = now - grid['quiet']
        if grid['phase'] == 'none':
            self.grid_hold_focus(grid)
            return True
        updating = api.updating(GRID)
        grid['seen_updating'] = grid['seen_updating'] or updating
        count = api.count(GRID)
        if grid['phase'] == 'wait':
            if not grid['kept'] or grid['fresh']:
                self.grid_hold_focus(grid)
            # The shell directory has arrived and the page has been quiet for a moment, no row job is running
            # and the list is not loading: the route set now is the only add-on call starting.
            # Review F4: a row job the grid paused keeps loading (the rows stay "visible" to Kodi under the grid), so
            # also wait until no row list is loading: two add-on launches together is the invoker race (HANDOFF 12.18).
            # R10.21: only the rows of the tab under the grid (row_busy), plus POV's marker and quiet window; and a watchdog (grid_wait).
            self.grid_wait(grid, now, quiet, count, updating)
            return True
        if grid.get('stuck'):
            # R10.21: the first load never started (watchdog): no route was set and the marker is not ours to check; stay failed until Back.
            self.grid_hold_focus(grid)
            return True
        if not api.grid_marker():
            # The Videos window was rebuilt (skin reload): its lists are new and empty and it is loading its
            # own directory. Start this page over from the beginning, in order.
            api.log('grid: window rebuilt, starting the page again')
            api.clear(GRID_READY)
            self.grid_off()
            return True
        if updating:
            return True   # never change the route of a list that is loading
        if grid['phase'] == 'loading':
            self.grid_loading(grid, now, count)
        elif grid['phase'] == 'failed':
            self.grid_hold_focus(grid)
        else:
            self.grid_shown(grid, now, count, quiet)
        return True

    def grid_under(self):
        """R10.21: the tab under the grid page: Netflix.Tab with the tab swap on, otherwise the root the window's own directory is on
        (build_tvshow_list = Shows). Not Control.IsVisible(row): a genre row's visibility sits on its parent group."""
        api = self.api
        if api.swap():
            return 'shows' if api.prop(TAB) == 'shows' else 'movies'
        return 'shows' if 'build_tvshow_list' in xbmc.getInfoLabel('Container.FolderPath') else 'movies'

    def grid_wait(self, grid, now, quiet, count, updating):
        """R10.21: start the first load of a grid page once it may (the serial loading guard as before: no row job in flight, the list
        not loading), counting only the rows of the tab under the grid: a shown row's IsUpdating counts, the other tab's rows only while
        the observer dropped their job (row_busy's orphan rule; a hidden list Kodi refetched after a playback stop never clears its flag).
        Never while POV's marker is up (POV_QUIET_CAP_SECONDS cap), and only in POV's quiet window.
        Watchdog, on `quiet` (the time the page has been in front with no dialog and the window not loading; a dialog RESTARTS it): one log
        line per wait reason (the first time it is seen). R10.21.1: no forced start; the grid starts only through the gate above. Failure
        (grid_stuck): after GRID_STUCK_SECONDS with nothing changing for GRID_STUCK_STILL_SECONDS and no POV marker up, or at
        POV_QUIET_CAP_SECONDS whatever happens."""
        api = self.api
        if quiet < GRID_FIRST_QUIET_SECONDS:
            return
        token = api.prop(POV_RUNNING)
        marker = bool(token) and now - self.pov_token_since < POV_QUIET_CAP_SECONDS
        under = self.grid_under()
        why = detail = ''
        busy = []
        if self.inflight:
            why, detail = 'row job', 'row %s in flight' % self.inflight.get('row')
        elif updating:
            why = detail = 'grid list loading'
        else:
            # the same rows as row_busy(row, under, now), with api.rows(under) read once (review F8): mode '' goes straight to the orphan rule
            shown = set(api.rows(under))
            busy = [row for row in ALL_ROWS if api.updating(row) and (row in shown or self.row_busy(row, '', now))]
            if busy:
                why, detail = 'rows', 'rows %s loading (tab under the grid: %s)' % (', '.join(str(row) for row in busy), under)
            elif marker:
                why, detail = 'pov', 'a POV call is running'
            elif not self.pov_quiet(now):
                why, detail = 'quiet', "POV's quiet window"
        seen = grid.setdefault('whys', set())
        if not why:
            if seen:
                api.log('grid: wait over after %.2f s' % quiet)
            self.grid_apply(grid, now, count)
            return
        if why not in seen:
            seen.add(why)
            api.log('grid: waiting (%s)' % detail)
        progress = (why, tuple(busy), token)
        if progress != grid.get('progress') or 'progress_at' not in grid:
            grid['progress'], grid['progress_at'] = progress, now
        # R10.21.1: no forced start (fail closed). A held marker is a call in progress (Astra finding 2): only the 60 s cap fails the page then.
        if ((quiet >= GRID_STUCK_SECONDS and now - grid['progress_at'] >= GRID_STUCK_STILL_SECONDS and not marker)
                or quiet >= POV_QUIET_CAP_SECONDS):
            self.grid_stuck(grid, quiet, detail)

    def grid_stuck(self, grid, waited, detail):
        """R10.21: the first load could not start in GRID_STUCK_SECONDS: the page fails (no route is set). Grid.Failed shows the failed line
        and parks focus on the tab (the skin and grid_hold_focus); Grid.Stuck picks its text. Back closes the page as on any failed page."""
        api = self.api
        grid.update(phase='failed', stuck=True)
        api.clear(GRID_READY)
        api.put(GRID_STUCK, grid['path'])
        api.put(GRID_FAILED, grid['path'])
        api.log('grid: first load never started in %.1f s (%s): failed until the page is opened again' % (waited, detail))
        self.grid_hold_focus(grid)

    def grid_new(self, path, now):
        api = self.api
        url = grid_url(path)
        grid = {'path': path, 'url': url, 'phase': 'wait' if url else 'none', 'quiet': None, 'since': now,
                'seen_updating': False, 'stale': '', 'step': None, 'retry_at': 0, 'retries': 0, 'step_failures': 0,
                'start': 0, 'index': 0, 'moved': now, 'kept': False, 'fresh': self.grid_seen != path,
                'stray': None, 'origin': self.grid_origin(path)}
        self.grid_seen = path
        api.clear(GRID_STUCK)   # R10.21
        if api.prop(GRID_FAILED) == path and grid_url(path):
            api.clear(GRID_FAILED)   # R10.21 (review F3): a reopened failed page waits behind grey cards, not the old failed line
        api.put(GRID_NAME, dict(grid_query(path)).get('name', ''))
        if not url:
            api.clear(GRID_READY)
            api.put(GRID_FAILED, path)
            api.log('grid: not a genre route, nothing to load')
            return grid
        held = api.prop(GRID_URL)
        if grid_same_genre(held, url):
            if api.count(GRID) > 0:
                # The list already holds this genre, maybe with more pages, maybe still loading them: keep its
                # route exactly as it is. Never shrink it and never restart a load that is running.
                grid['url'], grid['kept'] = held, True
            else:
                # Same genre and nothing listed: its last answer was empty. An unchanged route is not fetched
                # again, so ask with a new token.
                grid['url'] = grid_with(url, netflix_grid_retry=self.grid_token())
        if not grid['kept'] or api.prop(GRID_READY) != path:
            api.clear(GRID_READY)
        return grid

    def grid_apply(self, grid, now, count):
        api = self.api
        stale = '' if grid['kept'] or count == 0 else self.grid_first()
        api.clear(GRID_FAILED)
        api.clear(GRID_STUCK)   # R10.21
        api.put(GRID_URL, grid['url'])
        api.put(GRID_FOR, grid['path'])
        api.grid_marker('true')
        self.grid_armed = True
        grid.update(since=now, seen_updating=False, stale=stale)
        if grid['kept']:
            # Kodi may refresh the kept list in the background (its titles stay listed meanwhile).
            self.grid_ready(grid, 'kept')
        else:
            grid['phase'] = 'loading'
            api.log('grid: load %s' % grid['url'].split('?', 1)[-1][:120])

    def grid_loading(self, grid, now, count):
        waited = now - grid['since']
        arrived = grid['seen_updating'] or (bool(grid['stale']) and self.grid_first() != grid['stale'])
        if count > 0 and (arrived or waited >= (GRID_STALE_SECONDS if grid['stale'] else GRID_SETTLE_SECONDS)):
            self.grid_ready(grid, 'loaded in %.2f s' % waited)
        elif (count == 0 and (grid['seen_updating'] or waited >= GRID_EMPTY_SECONDS)) or waited >= GRID_FAIL_SECONDS:
            # POV swallows its own errors and answers with an empty directory.
            self.grid_reload(grid, now, 'empty answer')
        else:
            self.grid_hold_focus(grid)

    def grid_reload(self, grid, now, why):
        """Ask for the current route again (new token), or give up quietly after GRID_MAX_RETRIES."""
        api = self.api
        api.clear(GRID_READY)
        if grid['retries'] >= GRID_MAX_RETRIES:
            grid['phase'] = 'failed'
            api.put(GRID_FAILED, grid['path'])
            api.log('grid: %s, giving up until the page is opened again' % why)
            self.grid_hold_focus(grid)
            return
        grid['retries'] += 1
        grid['url'] = grid_with(grid['url'], netflix_grid_retry=self.grid_token())
        grid.update(phase='loading', since=now, seen_updating=False, stale='', step=None)
        api.put(GRID_URL, grid['url'])
        api.log('grid: %s, asking again (%d of %d)' % (why, grid['retries'], GRID_MAX_RETRIES))
        self.grid_hold_focus(grid)

    def grid_shown(self, grid, now, count, quiet):
        """The list is shown and not loading."""
        api = self.api
        step = grid['step']
        if count == 0:
            # Kodi empties a static list whose load failed: a load-more step, or a refresh Kodi did itself.
            # Go back to the route that worked, grey cards meanwhile, and return to the same title.
            if step:
                grid.update(url=step['good'], start=step['index'], step_failures=grid['step_failures'] + 1,
                            retry_at=now + GRID_RETRY_SECONDS)
            else:
                grid['start'] = grid['index']
            self.grid_reload(grid, now, 'step failed' if step else 'list emptied')
            return
        current = api.number('Container(%d).CurrentItem' % GRID)
        tile = grid_tile(grid['url'], xbmc.getInfoLabel('Container(%d).ListItemAbsolute(%d).FileNameAndPath' % (GRID, count - 1)))
        if step and (grid['seen_updating'] or count != step['count'] or now - step['since'] >= GRID_STALE_SECONDS):
            # A load-more step has finished. While it ran the old titles stayed listed and usable.
            grid['step'] = None
            short = (tile[0] == 'page' and grid_pages(grid['url']) < GRID_LAST_PAGES and count < GRID_ROW_CAP)
            if count <= step['count'] or short:
                # Nothing new, or POV could not fetch one of the later pages (it then answers with what it
                # has and a plain next-page tile). Try that step again later, a limited number of times.
                grid['step_failures'] += 1
                grid['retry_at'] = now + GRID_RETRY_SECONDS
                api.log('grid: step incomplete (%d titles), %d of %d' % (count, grid['step_failures'], GRID_MAX_RETRIES))
            else:
                grid['step_failures'] = 0
                api.log('grid: step done in %.2f s, %d titles' % (now - step['since'], count))
        focus = api.number('System.CurrentControlID')
        retry = grid.get('focus_retry')
        if retry:
            # The first-title focus of grid_ready could not be carried out: repeat it a few ticks, only while focus is
            # still exactly where it was then (the viewer has not moved) and the drawer is closed.
            grid['focus_retry'] = None
            if focus == retry[0] and not api.prop('Netflix.GenreMenu.Open'):
                if not self.grid_focus(grid, GRID, max(0, min(grid['index'], count - 1))) and retry[1] > 1:
                    grid['focus_retry'] = (focus, retry[1] - 1)
                return
        if focus != GRID:
            if (focus == GRID_HOLD or focus not in GRID_HEADER) and not api.prop('Netflix.GenreMenu.Open'):
                # e.g. Kodi handed focus to the empty window list after a details page closed (the skin
                # forwards that at once; this is the slower safety net behind it).
                if self.grid_stray(grid, now):
                    self.grid_focus(grid, GRID, max(0, min(grid['index'], count - 1)))
            else:
                grid['stray'] = None
            return
        grid['stray'] = None
        if tile[0] and current == count and count > 1:
            # POV's "more" tile is not a title: the cursor never rests on it.
            self.grid_focus(grid, GRID, count - 2)
            return
        if current - 1 != grid['index']:
            grid['index'], grid['moved'] = max(0, current - 1), now
        rested = now - grid['moved']
        if GRID_MOVING_SECONDS < rested < GRID_RESTED_SECONDS:
            return
        if (grid['step'] or self.inflight or now < grid['retry_at'] or quiet < GRID_QUIET_SECONDS
                or grid['step_failures'] >= GRID_MAX_RETRIES or count - current > GRID_AHEAD):
            return
        if tile[0] == 'more':
            more = grid_with(grid['url'], accumulate_to=tile[1])
        elif tile[0] == 'page' and grid_pages(grid['url']) < GRID_LAST_PAGES and count < GRID_ROW_CAP:
            more = grid_with(grid['url'], netflix_grid_retry=self.grid_token())   # the same step once more
        else:
            return
        # Only the route changes: Kodi keeps the listed titles and the selected one while the longer list
        # loads, then swaps it in. One add-on call, started while nothing else is.
        grid['step'] = {'good': grid['url'], 'since': now, 'count': count, 'index': grid['index']}
        grid['url'], grid['quiet'], grid['seen_updating'] = more, now, False
        api.put(GRID_URL, more)
        api.log('grid: more, %d titles listed, next %s' % (count, grid_pages(more)))

    def grid_hold_focus(self, grid):
        """Grey cards or "No titles": keep focus on the visible rest button, never on a control this page hides."""
        api = self.api
        focus = api.number('System.CurrentControlID')
        if focus in GRID_HEADER or api.prop('Netflix.GenreMenu.Open'):
            return
        # While loading: the rest button under the grey cards. On a failed page there are no cards, so the tab.
        target = GRID_HOLD if grid['phase'] in ('wait', 'loading') else grid_tab(grid['path'])
        if focus != target:
            self.grid_focus(grid, target)

    def grid_stray(self, grid, now):
        """True once focus has been on a control this page hides for GRID_STRAY_SECONDS."""
        if grid.get('stray') is None:
            grid['stray'] = now
        return now - grid['stray'] >= GRID_STRAY_SECONDS

    def grid_focus(self, grid, control, index=None):
        """SetFocus on a grid page, only while that page is still the one in front and the target is on screen.
        (A SetFocus that cannot be carried out leaves the window with no focus at all.)"""
        api = self.api
        if (api.page() != grid['path'] or api.visible('Container.IsUpdating')
                or not api.visible('Control.IsVisible(%d)' % control)):
            return False
        if index is None:
            xbmc.executebuiltin('SetFocus(%d)' % control)
        else:
            api.focus(control, index)
        grid['stray'] = None
        return True

    @staticmethod
    def grid_first():
        # Count and first three titles: two genres can share their most popular title, rarely all three.
        return '|'.join([xbmc.getInfoLabel('Container(%d).NumItems' % GRID)] + [
            xbmc.getInfoLabel('Container(%d).ListItemAbsolute(%d).FileNameAndPath' % (GRID, index)) for index in range(3)])

    def grid_ready(self, grid, how):
        api = self.api
        grid['phase'] = 'ready'
        api.clear(GRID_FAILED)
        api.put(GRID_READY, grid['path'])
        count = api.count(GRID)
        current = api.number('Container(%d).CurrentItem' % GRID)
        # A page that was only covered (details page, Search, playback) returns to where it was. A page that is
        # opened, from the genre menu or a "More Titles" tile, starts on its first title, also when the list
        # was kept from an earlier visit (Kodi keeps a list's selected title with it).
        returning = how == 'kept' and not grid['fresh']
        grid['index'] = max(0, min((current - 1) if returning else grid['start'], count - 1))
        focus = api.number('System.CurrentControlID')
        if count > 0 and not api.prop('Netflix.GenreMenu.Open') and (focus in (3736, GRID_HOLD) or focus not in GRID_HEADER):
            # 3736: the genre menu parks focus on its button until the page arrives. 8593: the rest button
            # under the grey cards. Anything else outside the header is a control this page hides.
            if not self.grid_focus(grid, GRID, grid['index']):
                # Not on screen yet / page still settling: try again on the next ticks while focus stays where it
                # was (Mac 2026-11-11 10:40: a genre-menu page whose list was kept stayed on 3736 for good).
                grid['focus_retry'] = (focus, 10)
                api.log('grid: focus to the grid deferred (focus %d)' % focus)
        api.log('grid: ready (%s), %d titles' % (how, count))

    def focus_net(self, mode, window, now):
        """Movies/Shows page: land where the viewer came from after a grid page, and never leave focus nowhere."""
        api = self.api
        if mode not in ('movies', 'shows') or window != 10025:
            self.nofocus_since = None
            return
        ret = self.grid_return
        if ret and now - ret['left'] > GRID_RETURN_SECONDS:
            ret = self.grid_return = None
        if (api.visible('Player.HasMedia') or api.visible('System.HasActiveModalDialog') or api.visible('Container.IsUpdating')
                or api.prop('Netflix.TabDwellTarget') or api.prop('Netflix.GenreMenu.Open')
                or api.prop(BOOT_PENDING) == 'true'):
            self.nofocus_since = None   # something else owns focus right now (the drawer, a dialog, a page load)
            return
        focus = api.number('System.CurrentControlID')
        arrival = api.prop('Netflix.TabArrival')
        if arrival:
            # A tab arrival is pending (Back from the genre menu over a grid sets it): the skin's own hand-off
            # runs on a 500 ms timer, and until it fires nothing on the page is highlighted. Once the page it
            # is meant for is here and focus has been nowhere for a moment, put it on that tab now; the
            # hand-off then finds it already there.
            if focus == 0 and arrival == ('3732' if mode == 'shows' else '3733'):
                if self.nofocus_since is None:
                    self.nofocus_since = now
                elif now - self.nofocus_since >= NOFOCUS_SECONDS:
                    self.nofocus_since = None
                    xbmc.executebuiltin('SetFocus(%s)' % arrival)
                    api.log('tab arrival pending and focus nowhere: tab %s' % arrival)
            else:
                self.nofocus_since = None
            return
        if ret and ret['origin'] and ret['origin'][0] == mode and focus in (0, 590, 591, 592):
            # Back from a "More Titles" page: Kodi lands on the window's own list (or nowhere). Go back to the
            # tile it was opened from, if that row is still there.
            tab, row, index = ret['origin']
            self.grid_return = None
            if focus == row:
                # staged/trending-grid: the Trending tile's row is the window's own list 591, which the skin's Back
                # already focused, on the tile. A SetFocus by position here would move the cursor (Mac: item 21 -> 20).
                self.nofocus_since = None
                return
            if api.count(row) > 0 and api.visible('Control.IsVisible(%d)' % row) and home_focusable(api, row):   # R10.10 review 7
                api.focus(row, max(0, min(index, api.count(row) - 1)))
                api.log('back from a grid page: focus to row %d, item %d' % (row, index + 1))
                self.nofocus_since = None
                return
        if ret and not ret['origin'] and focus in (3736, ret['tab']):
            # Back from a genre-menu page belongs on the tab its drawer hangs from. The skin's hand-off (the
            # tab arrival above) gives up after 3 seconds; when the Movies/Shows page took longer to come back,
            # focus is still on the genre menu button. Once it is on the tab the return is over.
            self.grid_return = None
            if focus == 3736:
                xbmc.executebuiltin('SetFocus(%d)' % ret['tab'])
                api.log('back from a grid page: tab %d (the hand-off had timed out)' % ret['tab'])
            self.nofocus_since, self.nofocus_tries = None, 0
            return
        if focus != 0:
            self.nofocus_since, self.nofocus_tries = None, 0
            return
        if self.nofocus_since is None:
            self.nofocus_since = now
            return
        if now - self.nofocus_since < NOFOCUS_SECONDS:
            return
        # No focused control: a SetFocus (ours, the skin's or Kodi's) reached a control that could not take it.
        self.nofocus_since = None
        self.nofocus_tries += 1
        tab = 3732 if mode == 'shows' else 3733
        if (ret and not ret['origin']) or self.nofocus_tries > 1:
            # Back from a genre-menu page belongs on the tab its drawer hangs from (as before); also the
            # fallback when the remembered row did not take focus.
            self.grid_return = None
            xbmc.executebuiltin('SetFocus(%d)' % tab)
            api.log('focus was nowhere: tab %d' % tab)
        else:
            restore(api, mode)
            api.log('focus was nowhere: restored to the remembered row')

    def rescue_hidden_focus(self, mode, window):
        # With the Shows tab swapped in, the window's own list (591) is hidden. Kodi still hands it
        # focus whenever the directory is (re)entered, e.g. Back from page 2 of Shows Trending, and a
        # hidden control swallows every key. Move focus to the visible Shows list (or its tab).
        api = self.api
        if mode != 'shows' or window != 10025 or not api.swap():
            return
        if api.number('System.CurrentControlID') != 591 or api.visible('Container.IsUpdating'):
            return
        if api.visible('Control.IsVisible(591)') or api.visible('System.HasActiveModalDialog'):
            return
        if api.count(SHOWS_TRENDING) > 0 and api.visible('Control.IsVisible(%d)' % SHOWS_TRENDING):
            try:
                index = int(api.prop(PREFIX + 'shows.Index.%d' % SHOWS_TRENDING))
            except ValueError:
                index = 0
            api.focus(SHOWS_TRENDING, max(0, min(index, api.count(SHOWS_TRENDING) - 1)))
        else:
            xbmc.executebuiltin('SetFocus(3732)')
        api.log('moved focus off hidden 591 (shows tab)')

    @staticmethod
    def lease(row, now):
        mode = ROW_MODE[row]
        return {'row': row, 'mode': mode, 'window': MODE_WINDOW[mode],
                'started': now, 'seen_updating': False, 'present_since': None}

    def completed(self, row):
        return self.api.prop(PREFIX + 'Completed.%d' % row) == 'true'

    def recover(self, row, now):
        if row in ROW_MODE and not self.completed(row):
            self.pending[ROW_MODE[row]] = self.lease(row, now)

    def integer_prop(self, name, row):
        try:
            return max(0, int(self.api.prop(PREFIX + '%s.%d' % (name, row))))
        except (ValueError, TypeError):
            return 0

    def positive(self, row):
        if row not in GENRE_ROWS:
            return
        self.api.put(PREFIX + 'NonEmpty.%d' % row, 'true')
        # Keep the URL token monotonic, but a successful provider rearms its
        # failure budget for a later, independent loss of the same widget.
        for name in ('RetryAttempts', 'Failed', 'EmptyConfirmed'):
            key = PREFIX + '%s.%d' % (name, row)
            if self.api.prop(key):
                self.api.clear(key)

    def retry_reason(self, row):
        if row not in GENRE_ROWS:
            return ''
        known_nonempty = self.api.prop(PREFIX + 'NonEmpty.%d' % row) == 'true'
        limit = MAX_RETRIES if known_nonempty else 1
        if self.integer_prop('RetryAttempts', row) >= limit:
            return ''
        if known_nonempty:
            return 'lost-nonempty'
        if self.api.prop(PREFIX + 'EmptyConfirmed.%d' % row) != 'true':
            return 'confirm-empty'
        return ''

    def request_retry(self, row, now, reason):
        mode = ROW_MODE[row]
        job = self.lease(row, now)
        job['retry_reason'] = reason
        self.pending[mode] = job
        self.api.clear(PREFIX + 'Completed.%d' % row)
        self.api.clear(PREFIX + mode + '.Complete')
        self.complete_modes.discard(mode)

    def validate_retained(self, mode, now):
        # Reentry/native directory rebuilds can destroy a previously completed
        # widget. A completion flag alone says nothing about its live contents.
        # Check only during a bounded owner-entry grace, never every warm tick.
        if mode == 'saved' or self.validation_done or now < self.validation_scan:
            return
        self.validation_scan = now + .5
        unresolved = False
        for row in self.api.rows(mode):
            if self.api.count(row) > 0:
                if not self.api.present(row) or self.api.updating(row):
                    # Kodi retains old items while a replacement directory is
                    # loading. Those are not proof of a successful recovery.
                    unresolved = True
                    self.zero_since.pop(row, None)
                    continue
                self.positive(row)
                self.zero_since.pop(row, None)
                continue
            reason = self.retry_reason(row)
            if not reason:
                self.zero_since.pop(row, None)
                continue
            unresolved = True
            if not self.api.present(row) or self.api.updating(row):
                self.zero_since.pop(row, None)
                continue
            since = self.zero_since.setdefault(row, now)
            if now >= self.validation_due and now - since >= SETTLE_SECONDS:
                self.request_retry(row, now, reason)
                return
        if now >= self.validation_due and not unresolved:
            self.validation_done = True

    def tick(self, now):
        self.home_tick(now)  # R10.10: Customize Home changes, applied between row calls
        self.boot.tick(now)  # Also observe the expected busy/Home landing bridge.
        api = self.api
        self.park_tick(now)  # R10.23: park the rows when a player comes up
        self.view_park(now)  # R10.22: and an open My Lists list
        self.pov_watch(now)
        mode, window = api.mode(), api.window()
        owner = (mode, window)
        if owner != self.owner:
            self.owner = owner
            if mode in ('movies', 'shows') and window == MODE_WINDOW[mode]:
                self.continue_stale(mode, now)
            self.saved_ready = None
            self.saved_quiet = None
            self.validation_done = False
            self.validation_due = now + SETTLE_SECONDS
            self.validation_scan = now
            self.zero_since.clear()
        if window == 10025 and not api.visible('Container.IsUpdating'):
            # Which root the Videos window's own directory is on ('' for any other page). The Movies/Shows tabs in
            # Search / My Lists read it: when Videos already holds that root they reopen it without a path, so Kodi
            # does not force a refresh of the same folder (review F3). Written only when it changes.
            path = xbmc.getInfoLabel('Container.FolderPath')
            api.put(VIDEOS_ROOT, ('shows' if 'build_tvshow_list' in path else 'movies') if is_root(path) else '')
        if window == 10000 and api.prop(GRID_SHELL):
            # Home: the browse page was left for good. A grid must not come back over the next Movies/Shows visit
            # (Search / My Lists keep it: their tabs clear it themselves when they open the Movies/Shows root).
            api.clear(GRID_SHELL)
        self.tmdb_rest(window, now)
        self.remember(mode)
        self.rescue_hidden_focus(mode, window)
        self.grid_tick(now, window)
        self.focus_net(mode, window, now)
        self.park_under_grid(now, window)   # R10.23
        if self.inflight:
            job = self.inflight
            row = job['row']
            if row not in api.rows(job['mode']):
                # The swap was switched off while 592 was loading: its control is gone for good,
                # so drop the lease instead of waiting forever for it to become present.
                if api.prop('Netflix.CatalogGenre.Loading') == str(row):
                    api.clear('Netflix.CatalogGenre.Loading')
                api.log('dropped mode=%s row=%d (tab swap off)' % (job['mode'], row))
                self.inflight = None
            elif (mode != job['mode'] and not (job.get('under_grid') and not mode and self.grid)) or window != job['window']:   # R10.23: under_grid
                # A window/tab switch can cancel a widget job. Its absent
                # controls report zero items and not-updating; neither is a
                # completion signal. Keep its enabled URL and retry ownership
                # on return without resetting any loaded row.
                self.pending[job['mode']] = self.lease(row, now)
                self.dropped[row] = now   # R10.5 review 1: its Kodi job keeps running (row_busy)
                if api.prop('Netflix.CatalogGenre.Loading') == str(row):
                    api.clear('Netflix.CatalogGenre.Loading')
                api.log('paused mode=%s row=%d owner_window=%d' %
                        (job['mode'], row, job['window']))
                self.inflight = None
            elif not api.present(row):
                job['present_since'] = None
                job['seen_updating'] = False
                return
            else:
                if job['present_since'] is None:
                    job['present_since'] = now
                updating = api.updating(row)
                job['seen_updating'] = job['seen_updating'] or updating
                if updating:
                    if self.home_custom.get(row) and now - job['started'] > HOME_CUSTOM_SECONDS:
                        # R10.10 hook: a custom list that does not answer is taken off the page; the queue and the boot gate go on
                        api.put(PREFIX + 'Completed.%d' % row, 'true')
                        if api.prop('Netflix.CatalogGenre.Loading') == str(row):
                            api.clear('Netflix.CatalogGenre.Loading')
                        self.inflight = None
                        self.dropped[row] = now   # review 3: POV may still run: the orphan rule (row_busy) holds the queue
                        self.home_break(row, 'no answer in %d s' % HOME_CUSTOM_SECONDS)
                    return
                active_for = now - job['present_since']
                # (not sooner than CONTINUE_ANSWER_HOLD after the row was first seen present by this job)
                # (a first load has no old items to mistake for the answer: no hold; a refetch holds CONTINUE_ANSWER_HOLD)
                answer = api.prop(CONTINUE_ANSWER + CONTINUE_ROWS[row]) if row in CONTINUE_ROWS else ''
                token = api.prop(PREFIX + 'Token.%d' % row)
                held = active_for >= CONTINUE_ANSWER_HOLD or not job.get('refresh')
                errored = row in CONTINUE_ROWS and answer == 'e:' + token   # review 2: the route failed (never stale: per token)
                answered = (row in CONTINUE_ROWS and held and answer == 'r:' + token) or errored
                if not (job['seen_updating'] or api.count(row) > 0 or active_for >= SETTLE_SECONDS or answered):
                    return
                if job.get('refresh') and not job['seen_updating'] and active_for < SETTLE_SECONDS and not answered:
                    return  # the old items are still listed; wait for the refetch itself
                # Stable empty results remain valid, but only while their
                # owner and actual control/placeholder are observable.
                count = api.count(row)
                reason = self.retry_reason(row) if count == 0 else ''
                if row in CONTINUE_ROWS:
                    if errored:
                        # R10.5 review 2: not "empty": leave EmptyConfirmed clear, retry once (new token), then give up
                        # as Failed (the boot gate accepts Failed) without moving focus
                        api.clear(PREFIX + 'EmptyConfirmed.%d' % row)
                        if self.integer_prop('RetryAttempts', row) < 1:
                            reason = 'continue-error'
                        else:
                            api.put(PREFIX + 'Failed.%d' % row, 'true')
                    else:
                        for name in ('RetryAttempts', 'Failed'):
                            api.clear(PREFIX + '%s.%d' % (name, row))
                        if count > 0:
                            api.clear(PREFIX + 'EmptyConfirmed.%d' % row)
                        else:
                            api.put(PREFIX + 'EmptyConfirmed.%d' % row, 'true')
                elif count > 0:
                    self.positive(row)
                elif row in GENRE_ROWS and not reason:
                    if api.prop(PREFIX + 'NonEmpty.%d' % row) == 'true':
                        api.put(PREFIX + 'Failed.%d' % row, 'true')
                        api.log('retry limit row=%d attempts=%d; provider remains empty' %
                                (row, self.integer_prop('RetryAttempts', row)))
                    else:
                        api.put(PREFIX + 'EmptyConfirmed.%d' % row, 'true')
                if reason:
                    self.request_retry(row, now, reason)
                    if reason == 'continue-error':
                        self.pending[job['mode']]['not_before'] = now + REFRESH_GAP_SECONDS
                else:
                    api.put(PREFIX + 'Completed.%d' % row, 'true')
                    api.clear(HOME_STALE + str(row))   # R10.10
                    self.dropped.pop(row, None)   # R10.10: its own job finished: no orphan left for this row
                    self.park_settled(row, now, job)   # R10.23
                api.log('settled row=%d items=%d elapsed=%.3f observed_update=%s' %
                        (row, api.count(row), now - job['started'], job['seen_updating']))
                if self.home_custom.get(row) and not reason and api.count(row) == 0:
                    self.home_break(row, 'no titles')   # R10.10 hook: a custom list that is empty leaves no empty row
                if api.prop('Netflix.CatalogGenre.Loading') == str(row):
                    api.clear('Netflix.CatalogGenre.Loading')
                self.inflight = None
                if row in CONTINUE_ROWS and not errored and not reason:
                    self.continue_settled(row, job, now)
        if self.refresh_tick(now, mode, window):
            return
        if not mode or window != MODE_WINDOW[mode] or api.visible('Player.HasMedia') or api.visible('System.HasActiveModalDialog'):
            self.validation_due = now + SETTLE_SECONDS
            self.zero_since.clear()
            self.saved_quiet = None
            return
        if api.visible('Container.IsUpdating') or api.prop('Netflix.TabDwellTarget') or api.prop('Netflix.TabArrival'):
            self.saved_quiet = None
            self.validation_done = False
            self.validation_due = now + SETTLE_SECONDS
            self.validation_scan = now
            self.zero_since.clear()
            return
        # Validate persisted completion once per observer/mode, then retain the
        # cheap warm path instead of polling every row property at150ms.
        if mode == 'saved':
            if not api.saved_marker():
                # The window was rebuilt (skin reload): its lists are empty again. Forget what was
                # loaded so the rows come back one at a time instead of all together.
                for key in ('Enabled.533', 'Enabled.531', 'SavedShowsEnabled', 'Completed.533',
                            'Completed.531', 'Completed.532', 'saved.Complete'):
                    api.clear(PREFIX + key)
                for key in ('View', 'ViewFrom', 'ViewID', 'ViewPath', 'ViewName', 'ViewError', 'ViewIndex', 'OpenSeq.list', 'OpenSeq.all', 'AllToken', 'Failed', 'Go.list', 'Go.all', 'Loaded.list', 'Loaded.all'):
                    api.clear('Netflix.MyLists.' + key)   # R10.22: a rebuilt window shows the page, not a view whose lists are gone
                self.complete_modes.discard('saved')
                self.pending.pop('saved', None)
                self.saved_ready = None
                api.saved_marker('true')
                api.log('saved window is new; rows will load one at a time')
            if self.view_tick(now):
                return   # R10.22: a My Lists view is waiting for its load gate (or just got it): nothing else starts in this tick
            if self.saved_quiet is None:
                self.saved_quiet = now
            if now - self.saved_quiet >= SAVED_QUIET_SECONDS and self.saved_refresh(now):
                return
        rows = api.rows(mode)
        if (mode in self.complete_modes and self.complete_rows.get(mode) == rows
                and api.prop(PREFIX + mode + '.Complete') == 'true'):
            self.validate_retained(mode, now)
            if mode in self.complete_modes:
                return
        # Enabled means requested, not completed. In particular an interrupted
        # observer/tab switch must resume enabled-but-unsettled rows.
        remaining = [row for row in rows if not self.completed(row)]
        if remaining and api.prop(BOOT_PENDING) == 'true' and api.deferred:
            # R10.10: deferred rows (A24, custom entries) are not part of the boot gate: while it is up they are not asked for, and a
            # mode with only deferred rows left is not Complete yet. Once the gate has released they load here, one at a time.
            remaining = [row for row in remaining if row not in api.deferred]
            if not remaining:
                return
        if not remaining:
            api.put(PREFIX + mode + '.Complete', 'true')
            self.complete_modes.add(mode)
            self.complete_rows[mode] = rows
            self.pending.pop(mode, None)
            self.park_done(mode, now)   # R10.23
            self.validate_retained(mode, now)
            return
        self.complete_modes.discard(mode)
        if api.prop(PREFIX + mode + '.Complete'):
            api.clear(PREFIX + mode + '.Complete')
        if self.inflight:
            return
        if mode != self.last_mode or now - self.last_scan >= (.25 if self.parked.intersection(remaining) else 1):   # R10.23: parked rows wait less
            self.any_busy = any(self.row_busy(row, mode, now) for row in ALL_ROWS)
            self.last_scan, self.last_mode = now, mode
        if self.any_busy:
            return
        if mode == 'saved':
            # My Lists counts as open before its window has finished loading, and the window's own
            # onload may start the restore-row script in that frame. Start the first row only once
            # its control has been on screen for a moment, so the two never launch together.
            if not api.present(SAVED[0]):
                self.saved_ready = None
                return
            if self.saved_ready is None:
                self.saved_ready = now
            if now - self.saved_ready < SAVED_READY_SECONDS:
                return
            if self.orphan_running(now):
                return   # R10.5 re-review F2
        early = now - self.player_last < CONTINUE_AFTER_STOP_SECONDS
        parked = [row for row in remaining if row in self.parked]   # R10.23
        pend = self.pending.get(mode)
        if early and ((remaining[0] in CONTINUE_ROWS and not parked) or (parked and all(row in CONTINUE_ROWS for row in remaining))
                      or (parked and pend and pend['row'] in CONTINUE_ROWS and pend['row'] in remaining)):
            return
        if self.pending.get(mode, {}).get('not_before', 0) > now:
            return   # R10.5 re-review F5
        if not self.pov_quiet(now):
            return   # R10.5.1: another POV call ran in the last POV_QUIET_SECONDS
        if now - self.boot.released_at < HOME_AFTER_GATE_SECONDS and any(
                row in api.deferred or api.prop(HOME_STALE + str(row)) for row in remaining):
            return   # R10.10 review 5: not in the release tick: deferred (A24, custom) and re-pointed rows start a moment later
        job = self.pending.pop(mode, None)
        if job and job['row'] in remaining:
            row = job['row']
            retry_reason = job.get('retry_reason')
            job = self.lease(row, now)
            event = 'resume'
            if retry_reason:
                job['retry_reason'] = retry_reason
                event = 'retry'
        elif parked:
            # R10.23: the rows parked while a player was up come back in park_pick's order (Continue Watching not before 3 s after the player)
            row = self.park_pick(mode, remaining, early)
            if row is None:
                return
            job = self.lease(row, now)
            event = 'resume' if api.enabled(row) else 'start'
        else:
            focused = api.number('System.CurrentControlID')
            preferred = focused - 8000
            row = preferred if preferred in remaining else remaining[0]
            job = self.lease(row, now)
            event = 'resume' if api.enabled(row) else 'start'
        if row in self.parked:
            self.park_started.setdefault(mode, now)   # R10.23: the page's restore has started (park_done)
            self.park_loaded[row] = now
            if row in CONTINUE_ROWS and api.count(row) > 0:
                # review F4: a parked CW row that kept its titles: a new token and the refresh rule (as refresh_tick), so it cannot settle on them
                job['refresh'] = True
                job['focus'] = api.number('System.CurrentControlID')
                job['first'] = self.first_title(row)
                api.put(PREFIX + 'Token.%d' % row, str(time.time()))
        api.put(PREFIX + mode + '.Started', 'true')
        api.put('Netflix.CatalogGenre.Loading', row)
        if row not in CONTINUE_ROWS and api.count(row) > 0:
            job['refresh'] = True   # R10.10 re-review: old titles still listed: wait for the refetch itself
        self.inflight = job
        if row in CONTINUE_ROWS:
            api.clear(CONTINUE_ANSWER + CONTINUE_ROWS[row])
            if job.get('retry_reason'):
                api.put(PREFIX + 'Token.%d' % row, str(time.time()))   # R10.5 review 2: the CW path carries Token
        if row in SAVED_TOKENS and not api.enabled(row):
            api.put(PREFIX + 'Token.%d' % row, api.prop(SAVED_TOKENS[row]))
        # This setter is idempotent and leaves all existing URLs intact.
        if not api.enabled(row):
            api.enable(row)
        if job.get('retry_reason'):
            # Changing just this content URL causes Kodi's dynamic list
            # provider to request it again; Enabled=true alone cannot do that.
            token = self.integer_prop('Retry', row) + 1
            api.put(PREFIX + 'RetryAttempts.%d' % row,
                    self.integer_prop('RetryAttempts', row) + 1)
            api.put(PREFIX + 'Retry.%d' % row, token)
            api.log('retry row=%d token=%d reason=%s' %
                    (row, token, job['retry_reason']))
        api.log('%s mode=%s row=%d existing_items=%d remaining=%d' %
                (event, mode, row, api.count(row), len(remaining)))


class QuitMonitor(xbmc.Monitor):
    """R10.2: a script started by path gets no abort from Kodi at quit (only add-on scripts do), so waitForAbort()
    never returned and every quit waited 5 s and then killed this observer. Kodi announces System.OnQuit to every
    Python monitor before it stops scripts; waitForAbort() delivers it between its 100 ms waits."""

    quitting = False

    def onNotification(self, sender, method, data):
        if method == 'System.OnQuit':
            self.quitting = True


def run():
    # The OS lock covers independent RunScript interpreters; the watchdog cannot
    # duplicate a healthy observer. A failed interpreter releases it automatically.
    import fcntl
    from pathlib import Path
    import xbmcvfs
    # special://temp is the same folder in every Kodi profile, so one observer covers all of them;
    # the old per-profile lock (the skin's addon_data) let a second observer start after a switch.
    lock_dir = Path(xbmcvfs.translatePath(LOCK_DIR))
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / LOCK_NAME).open('a+b') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        api = Kodi()
        api.put(PREFIX + 'Running', 'true')
        api.clear('Netflix.CatalogGenre.Reset')  # Discard only a legacy reset latch, never loaded rows.
        monitor = QuitMonitor()
        coordinator = Coordinator(api)
        coordinator.home_apply(time.monotonic(), startup=True)   # R10.10: the layout is published before the first row is asked for
        # Recover a provider lease if the previous observer failed during a job.
        try:
            row = int(api.prop('Netflix.CatalogGenre.Loading'))
            coordinator.recover(row, time.monotonic())
        except ValueError:
            pass
        heartbeat = 0
        rest_file = xbmcvfs.translatePath(TMDB_REST_FILE)
        api.log('observer started; serial queue ready')
        publish_profiles(api)
        try:
            while not monitor.waitForAbort(
                    FAST_TICK if coordinator.inflight or coordinator.grid_busy or api.prop(BOOT_PENDING) == 'true' else IDLE_TICK):
                if monitor.quitting:
                    break
                now = time.monotonic()
                if now - heartbeat >= 1:
                    api.put(PREFIX + 'Heartbeat', time.time())
                    try:
                        with open(rest_file) as handle:
                            coordinator.tmdb_override(handle.read(32))
                    except OSError:
                        coordinator.tmdb_override('')
                    heartbeat = now
                coordinator.tick(now)
        except Exception as error:
            xbmc.log('Netflix Browse: stopped: %s' % error, xbmc.LOGERROR)
        finally:
            if not monitor.quitting:
                api.clear(PREFIX + 'Running')
                api.clear(PREFIX + 'Heartbeat')
                api.clear('Netflix.CatalogGenre.Loading')
                api.videos_prop(TMDB_PAUSE, '')
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'restore':
        restore(Kodi(), sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == 'start':
        run()
