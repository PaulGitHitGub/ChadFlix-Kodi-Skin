"""Open a series and its episode browser without changing the source container."""

from collections import OrderedDict
from queue import Empty, Queue
import json
import re
import sys
from pathlib import Path
from threading import Thread
from uuid import uuid4
from time import monotonic, sleep, time
from urllib.parse import parse_qsl, urlencode, urlsplit

import xbmc
import xbmcgui


_EXTRAS = str(Path(__file__).resolve().parent)
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)
from netflix_artwork import detail_backdrop
from netflix_playback_cover import (GRACE, hold as hold_cover, loading, release as release_cover, renew as renew_cover,
                                    start_loading, stop_loading)


POV = "plugin.video.pov"
TMDB_HELPER = "plugin.video.themoviedb.helper"
VIDEOS, FULLSCREEN_VIDEO = 10025, 12005
PRIMARY, RESTART, MORE, RELATED = 10, 11, 12, 13
DESCRIPTION, FAVORITE, BOOKMARK = 16, 17, 18
DETAIL_ACTIONS = {DESCRIPTION, FAVORITE, BOOKMARK}
MORE_LISTS = 1018   # R10.18: "Add to Another Watchlist" (the Menu key / a long press of OK on the Watchlist button): not a control, only a request
CONTEXT_MENU = 117  # Kodi's ACTION_CONTEXT_MENU (Menu key, long-press OK)
SAVED_COOLDOWN = 0.7  # seconds after a Favorite / Watchlist action in which another request (a held OK repeating, the OK that closed the picker) is dropped
SAVED_GUARD = 1.4     # ... and never less than this long after the action STARTED: on the show window (a Python dialog) a held key delivered ONE late repeat ~1.03 s after the press (measured), so the lock runs to start + 1.4
STAYS = {MORE, RELATED, FAVORITE, BOOKMARK}  # buttons that are on the page from its first frame
SEASONS, EPISODES, RECOMMENDATIONS, SECONDARY_STATUS = 20, 21, 22, 23
BACK = {9, 10, 92}
LEFT, RIGHT = 1, 2
CACHE_SIZE = 4
COUNT_REST = 0.8  # seconds the season focus must rest before a background count lookup starts
SEPARATOR = "  ·  "
# Show-page load (staged/show-page-load). Owner's rule for the description: a show nobody has started (or
# has finished) is described by the show's own plot with no episode line; the episode's title and plot
# replace it only when the viewer is part-way through the show (set_primary decides this once).
CACHE = "Netflix.Series.Cache."      # Window(Home): seasons + Play episode of the last shows opened, per person
CACHE_SHOWS = 6
KNOWN = "Netflix.Series.Known."      # Window(Home): like/watchlist icons and ratings last seen, per person
KNOWN_SHOWS = 40
REQUEST = "Netflix.Series.Request"   # Window(Home): "<owner>|<monotonic>" while any show page waits on POV
REQUEST_STALE = 45                   # seconds without a sign of life before a claim is ignored
REQUEST_LIMIT = 180                  # a running request stops renewing its claim after this long
OPENING = "Netflix.Series.Opening"   # Window(Home): same form, held from OK until the page closes
OPENING_STALE = 3                    # a script that died while opening blocks the next OK for at most this long
RENEW = 1                            # seconds between renewals of the two claims above
PLAY_GRACE = 0.4                     # seconds after the page appears in which a Play press is ignored: the
                                     # second half of a double OK on the row must not start playback
QUIET = 0.15                         # seconds between the end of any show-page request and PlayMedia
SETTLE = 0.03                        # seconds a new claim waits before it counts (see stake / holds)
LOST = object()                      # worker answer: another page won the request claim, ask again
RATINGS_WAIT = 20                    # seconds a page keeps looking for ratings that were not ready at open
RATINGS_EVERY = 0.5
NATIVE_LISTS = {"590", "591", "593"}  # lists whose own Select is taken over by the OK keymap script
# R10-playback: Play loads ON this page (design B loader, Includes_Netflix_Playback.xml): the page stays open while
# POV finds and opens the episode, and POV closes it when the video starts. Focus rests on LOADER_FOCUS meanwhile.
# The transition cover is held from the press until the page is back after the episode (renewed every COVER_RENEW s),
# so neither the start nor the return shows the browse page.
LOADER_FOCUS = 46
COVER_RENEW = 2.0
FLIGHT = "Netflix.Flight"            # the title pages' single-flight entry (extras/netflix_movie_details.py)
SAVED_DEFAULT = {"Favorite": "", "FavoriteLabel": "Favorite", "Bookmarked": "", "BookmarkLabel": "Add to Watchlist"}
MARKUP = re.compile(r"\[/?(?:COLOR(?: [^\]]+)?|B|I)\]", re.IGNORECASE)
RATINGS = {
    "IMDb": "IMDb_Rating",
    "RottenTomatoes": "RottenTomatoes_Rating",
    "Letterboxd": "Letterboxd_Rating",
}


def info(label):
    return xbmc.getInfoLabel(label).strip()


def number(value, minimum=1):
    value = str(value)
    if value.isascii() and value.isdecimal() and len(value) <= 12:
        integer = int(value)
        if integer >= minimum:
            return integer
    return None


def plugin(path):
    try:
        uri = urlsplit(path or "")
        if uri.scheme == "plugin" and uri.netloc in {POV, TMDB_HELPER}:
            return uri.netloc, dict(parse_qsl(uri.query))
    except ValueError:
        pass
    return "", {}


def pov_url(mode, tmdb_id, **params):
    query = urlencode({"mode": mode, "tmdb_id": tmdb_id, **params})
    return f"plugin://{POV}/?{query}"


def quantity(count, word):
    return f"{count} {word}{'s' if count != 1 else ''}"


def text(value):
    return MARKUP.sub("", value or "").strip()


def show_fields(tmdb_id, title, logo, backdrop, plot, year, mpaa, count, genre=""):
    short_meta = quantity(count, "Season") if count else ""
    year = str(number(year) or "")
    return {
        "ID": str(tmdb_id),
        "Title": text(title),
        "Logo": logo or "",
        "Backdrop": backdrop or "",
        "Plot": plot or "",
        "ShowMeta": short_meta,
        "Year": year,
        "Certification": mpaa or "",
        "Meta": SEPARATOR.join(field for field in (year, genre, short_meta) if field),
        "CanRelated": "1",
    }


def ratings(tmdb_id):
    home = xbmcgui.Window(10000)

    def matches():
        return (
            home.getProperty("TMDbHelper.ListItem.monitor.tmdb_id") == str(tmdb_id)
            and home.getProperty("TMDbHelper.ListItem.monitor.tmdb_type") in {"tv", "tvshow"}
            and not home.getProperty("TMDbHelper.IsUpdating")
            and not home.getProperty("TMDbHelper.IsUpdatingRatings")
        )

    if not matches():
        return {}
    values = {key: home.getProperty(f"TMDbHelper.ListItem.{field}").strip()
              for key, field in RATINGS.items()}
    tomatoes = values["RottenTomatoes"]
    if tomatoes.isdecimal() and 0 < int(tomatoes) <= 100:
        values["RottenTomatoes"] += "%"
    return {key: value for key, value in values.items()
            if value and value not in {"0", "0.0", "0.00", "N/A"}} if matches() else {}


def capture(source):
    if not number(source):
        return None
    item = f"Container({source}).ListItem."

    def field(name):
        return info(item + name)

    path = field("FileNameAndPath")
    provider, params = plugin(path)
    if not provider or field("DBType") != "tvshow":
        return None
    tmdb_id = (number(field("UniqueID(tmdb)")) or number(field("Property(tmdb_id)"))
               or number(params.get("tmdb_id")))
    if not tmdb_id:
        return None
    values = show_fields(
        tmdb_id, field("Title") or field("Label"), field("Art(clearlogo)"),
        field("Art(detail_fanart)") or field("Art(fanart)") or field("Art(landscape)"), field("Plot"),
        field("Year"), field("MPAA"), number(field("Property(totalseasons)")),
        field("Genre").split(" / ")[0],
    )
    if not values["Title"] or path != field("FileNameAndPath"):
        return None
    values["Poster"] = field("Art(poster)") or field("Art(thumb)")
    values.update(ratings(tmdb_id))
    return values


def directory(path):
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "Files.GetDirectory",
        "params": {
            "directory": path, "media": "video",
            "properties": ["title", "plot", "season", "episode", "runtime", "playcount",
                           "resume", "art", "uniqueid", "year", "mpaa", "genre"],
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        result = response.get("result")
        if isinstance(result, dict):
            rows = result.get("files")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
            if result.get("limits", {}).get("total") == 0:
                return []
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return None


def season_rows(rows, tmdb_id):
    seasons = {}
    for row in rows:
        url = row.get("file", "")
        provider, params = plugin(url)
        season = number(params.get("season"), minimum=0)
        if (provider != POV or params.get("mode") != "build_episode_list"
                or number(params.get("tmdb_id")) != tmdb_id or season is None):
            continue
        seasons[season] = {
            "number": season, "url": url,
            "label": text(row.get("label")) or (f"Season {season}" if season else "Specials"),
            "count": number(row.get("episode"), minimum=0),
            "watched": bool(number(row.get("playcount"))),
            "primary": number(params.get("netflix_primary_episode")),
            "action": params.get("netflix_primary_action"),
        }
    return [seasons[season] for season in sorted(seasons)]


def progress(row):
    resume = row.get("resume") or {}
    try:
        position = float(resume.get("position", 0))
        duration = float(resume.get("total", 0)) or float(row.get("runtime") or 0)
        if position > 0:
            return max(1, min(99, int(position / duration * 100))) if duration > 0 else 1
    except (AttributeError, OverflowError, TypeError, ValueError):
        pass
    return 100 if number(row.get("playcount")) else 0


# R10.10a: Files.GetDirectory answers art as "image://<url-encoded TMDb url>/" (a plain URL is handled too). The still tile is
# 437 px wide; the "original" file is ~1920 px. The same path segment in the encoded form is %2ft%2fp%2foriginal%2f.
STILLS = (("image.tmdb.org/t/p/original/", "image.tmdb.org/t/p/w780/"),
          ("image.tmdb.org%2ft%2fp%2foriginal%2f", "image.tmdb.org%2ft%2fp%2fw780%2f"))


def still(url):
    for full, small in STILLS:
        if full in url:
            return url.replace(full, small, 1)
    return url


def episode_rows(rows, tmdb_id, season):
    episodes = {}
    for row in rows:
        provider, params = plugin(row.get("file", ""))
        episode = number(params.get("episode"))
        if (provider != POV or params.get("mode") != "play_media" or params.get("mediatype") != "episode"
                or number(params.get("tmdb_id")) != tmdb_id
                or number(params.get("season"), minimum=0) != season or not episode):
            continue
        episodes[episode] = {
            "label": text(row.get("title") or row.get("label")) or f"Episode {episode}",
            "plot": row.get("plot") or "",
            "thumb": still((row.get("art") or {}).get("thumb", "")),
            "season": season, "episode": episode,
            "code": f"S{season}: E{episode}",
            "progress": progress(row),
            "runtime": number(row.get("runtime")),
            "unaired": params.get("netflix_unaired") == "true",
        }
    return [episodes[episode] for episode in sorted(episodes)]


def related_rows(rows, tmdb_id):
    related = {}
    for row in rows:
        provider, params = plugin(row.get("file", ""))
        related_id = number((row.get("uniqueid") or {}).get("tmdb")) or number(params.get("tmdb_id"))
        if provider != POV or not related_id or related_id == tmdb_id or not row.get("title"):
            continue
        art = row.get("art") or {}
        values = show_fields(related_id, row["title"], art.get("clearlogo"),
                             art.get("detail_fanart") or art.get("fanart") or art.get("landscape"), row.get("plot"),
                             row.get("year"), row.get("mpaa"), None,
                             next(iter(row.get("genre") or ()), ""))
        values["Poster"] = art.get("poster") or art.get("thumb") or ""
        related[related_id] = (values, values["Poster"])
    return list(related.values())


def pick_primary(season, episodes):
    aired = [episode for episode in episodes if not episode["unaired"]]
    if season["primary"]:
        return next((episode for episode in aired if episode["episode"] == season["primary"]), None)
    return (next((episode for episode in aired if 0 < episode["progress"] < 100), None)
            or next((episode for episode in aired if episode["progress"] == 0), None)
            or next(iter(aired), None))


def set_primary(view, season, episodes):
    """Choose the episode Play starts and decide, once, how the page describes it."""
    episode = view.primary = pick_primary(season, episodes)
    view.own_text = view.again = False
    if not episode:
        return
    if season["primary"]:                       # POV's play pointer: resume / next / play
        view.own_text = episode["progress"] > 0 or season["action"] != "play"
    elif episode["progress"] >= 100:            # nothing left to watch: Play starts the season again
        view.again = True
    else:                                       # no pointer: started unless it is the very first episode
        ordinary = [row for row in view.seasons if row["number"] > 0] or view.seasons
        first = next(item for item in episodes if not item["unaired"])
        view.own_text = episode["progress"] > 0 or season is not ordinary[0] or episode is not first


def show_key(tmdb_id):
    return f"{xbmcgui.Window(10000).getProperty('Netflix.Profiles.Current.Index')}.{tmdb_id}"



def remember(prefix, limit, key, value):
    """Session memory in Window(Home), newest `limit` shows kept; value None forgets the show."""
    home = xbmcgui.Window(10000)
    name = prefix + key
    names = [old for old in home.getProperty(prefix + "Keys").split("|") if old and old != name]
    if value is None:
        home.clearProperty(name)
    else:
        home.setProperty(name, json.dumps(value))
        names.append(name)
        for old in names[:-limit]:
            home.clearProperty(old)
    home.setProperty(prefix + "Keys", "|".join(names[-limit:]))


def recall(prefix, key):
    try:
        return json.loads(xbmcgui.Window(10000).getProperty(prefix + key) or "null")
    except ValueError:
        return None


def cache_store(view):
    """Remember this show's seasons and Play episodes for the next open in this Kodi session."""
    remember(CACHE, CACHE_SHOWS, show_key(view.tmdb_id), {
        "seasons": view.seasons, "primary_season": view.primary_season,
        "episodes": view.episodes[view.primary_season],
        "meta": [view.fields["ShowMeta"], view.fields["Meta"]],
    } if view.primary else None)


def cache_restore(view):
    """Fill a new view from the session cache; the page then re-checks it behind the picture."""
    try:
        saved = recall(CACHE, show_key(view.tmdb_id))
        seasons, number, episodes = saved["seasons"], saved["primary_season"], saved["episodes"]
        season = next(row for row in seasons if row["number"] == number)
        if not pick_primary(season, episodes):
            return
        if not view.fields["ShowMeta"]:
            view.fields["ShowMeta"], view.fields["Meta"] = saved["meta"]
    except (AttributeError, KeyError, StopIteration, TypeError, ValueError):
        return
    view.seasons, view.primary_season = seasons, number
    view.season_index = seasons.index(season)
    view.episodes[number] = episodes
    set_primary(view, season, episodes)
    view.revalidate = "seasons"


def remembered(saved):
    """R10.18: what is kept for a show's next opening: the two states with GENERIC labels, never a watchlist's name (the list may be renamed, or the
    title removed from it in My Lists, before the page opens again; the real label arrives with the first refresh_saved_actions())."""
    favorite, bookmarked = saved.get("Favorite") == "true", saved.get("Bookmarked") == "true"
    return {"Favorite": "true" if favorite else "", "FavoriteLabel": "Remove from Favorites" if favorite else "Favorite",
            "Bookmarked": "true" if bookmarked else "", "BookmarkLabel": "Remove from Watchlist" if bookmarked else "Add to Watchlist"}


def known_restore(view):
    """Like/watchlist icons and ratings as last seen for this show, so the first frame already has them."""
    known = recall(KNOWN, show_key(view.tmdb_id))
    known = known if isinstance(known, dict) else {}
    saved, rated = known.get("saved"), known.get("ratings")
    if isinstance(saved, dict) and set(saved) == set(SAVED_DEFAULT) and all(isinstance(v, str) for v in saved.values()):
        view.saved = remembered(saved)
    if any(view.fields.get(key) for key in RATINGS):
        known_store(view, ratings={key: view.fields[key] for key in RATINGS if view.fields.get(key)})
    elif isinstance(rated, dict) and set(rated) <= set(RATINGS) and all(isinstance(v, str) for v in rated.values()):
        view.fields.update(rated)


def known_store(view, **parts):
    known = recall(KNOWN, show_key(view.tmdb_id))
    known = known if isinstance(known, dict) else {}
    if any(known.get(key) != value for key, value in parts.items()):
        remember(KNOWN, KNOWN_SHOWS, show_key(view.tmdb_id), {**known, **parts})


def holder(name, stale):
    """Who holds a claim ("" when free or when its last sign of life is older than `stale` seconds)."""
    owner, _, stamp = xbmcgui.Window(10000).getProperty(name).partition("|")
    try:
        return owner if monotonic() - float(stamp) < stale else ""
    except ValueError:
        return ""


def stake(name, owner, stale):
    """First half of a claim: write our name unless someone else holds it. Two scripts can both pass
    this in the same instant, so the claim only counts once holds() still shows our name SETTLE seconds
    later: by then a rival that also saw it free has written too, and only the last writer remains."""
    if holder(name, stale) not in ("", owner):
        return False
    xbmcgui.Window(10000).setProperty(name, f"{owner}|{monotonic()}")
    return True


def holds(name, owner):
    return xbmcgui.Window(10000).getProperty(name).partition("|")[0] == owner


def renew(name, owner):
    if holds(name, owner):
        xbmcgui.Window(10000).setProperty(name, f"{owner}|{monotonic()}")


def release(name, owner):
    if holds(name, owner):
        xbmcgui.Window(10000).clearProperty(name)


class SeriesView:
    def __init__(self, fields, from_row=True):
        self.fields = {**fields, "Backdrop": detail_backdrop(fields.get("Backdrop", ""))}
        # The browse-size backdrop the row already loaded (not for More Like This shows: no row loaded it).
        self.quick_backdrop = (fields.get("Backdrop", "")
                               if from_row and self.fields["Backdrop"] != fields.get("Backdrop") else "")
        self.tmdb_id = int(fields["ID"])
        self.page = ""
        self.seasons = None
        self.season_index = 0
        self.primary_season = None
        self.primary = None
        self.episodes = OrderedDict()
        self.related = None
        self.related_index = 0
        self.episode_positions = {}
        self.failed = set()
        self.focus = PRIMARY
        self.restore_season = None
        self.own_text = False     # main page shows the Play episode's title and plot
        self.again = False        # everything aired is watched: Play starts over
        self.revalidate = ""      # session-cached data being re-checked: "seasons", then "episodes" if needed
        self.recheck_failed = False
        self.saved = None         # like/watchlist properties, once known
        self.ratings_until = None
        cache_restore(self)
        known_restore(self)

    @property
    def primary_pending(self):
        return (self.primary is None and "seasons" not in self.failed and
                (self.seasons is None or
                 (bool(self.seasons) and self.primary_season not in self.episodes
                  and self.primary_season not in self.failed)))

    def season_of(self, number):
        return next(index for index, season in enumerate(self.seasons) if season["number"] == number)

    @property
    def retry(self):
        """Nothing to play because a load failed: the Play button becomes Try Again."""
        return self.primary is None and bool(
            "seasons" in self.failed or (self.seasons and self.primary_season in self.failed))

    @property
    def resumable(self):
        return bool(self.primary) and 0 < self.primary["progress"] < 100

    def invalidate_playback(self):
        if self.seasons:
            self.restore_season = self.seasons[self.season_index]["number"]
        self.seasons = None
        self.primary = None
        self.primary_season = None
        self.episodes.clear()
        self.failed.clear()
        self.revalidate = ""
        self.recheck_failed = False
        remember(CACHE, CACHE_SHOWS, show_key(self.tmdb_id), None)


class SeriesDialog(xbmcgui.WindowXMLDialog):
    def __init__(self, *args):
        self.stack = []
        self.active = None
        self.result = Queue(maxsize=1)
        self.pending = None
        self.worker = None
        self.pending_focus = None
        self.play_request = None
        self.play_token = None      # R10: the play request's token (Netflix.Play.Loading) while the loader is up
        self.player = None          # R10: SeriesPlayer, created just before PlayMedia
        self.launched = None        # R10: monotonic() of PlayMedia
        self.cover = None           # R10: transition cover token: held from Play, or handed over by open_series
        self.cover_renewed = float("-inf")
        self.cover_release_at = None
        self.detail_request = None
        self.saved_done = float("-inf")   # R10.18: monotonic() when the last Favorite / Watchlist action finished (inf while one runs)
        self.closed = False
        self.ready = False
        self.season_moved = float("-inf")
        self.owner = f"{id(self)}.{monotonic()}"
        self.opener = None          # set by open_series: this page's "opening" claim
        self.play_wanted = None     # a held press: (control, (season, episode) or None, was "Watch Again")
        self.early_click = None     # (control, when): Play pressed before onInit
        # monotonic() may start near 0, so "never" is -inf
        self.requested = self.renewed = self.opening_renewed = self.ratings_checked = float("-inf")
        self.shown_at = float("-inf")   # set by run(): when the window was shown
        self.ended = float("-inf")      # when this page's last request ended

    @property
    def view(self):
        return self.stack[-1]

    def properties(self, **values):
        for key, value in values.items():
            self.setProperty(f"Series.{key}", value)

    def onInit(self):
        self.ready = True
        self.season_list = self.getControl(SEASONS)
        self.episode_list = self.getControl(EPISODES)
        self.related_list = self.getControl(RECOMMENDATIONS)
        if not self.view.page and self.focus_id() in STAYS:
            self.view.focus = self.focus_id()  # a key pressed between the first frame and this call
        self.load_needed()  # the POV request starts before refresh()'s local favourites lookup
        self.refresh()
        if self.early_click is not None:
            self.press_play(*self.early_click)  # OK on Play before this call: treated as when it was pressed
            self.early_click = None

    def paint(self):
        """Everything already known about the view, in its final place. Safe before the window is shown."""
        self.properties(**{
            **dict.fromkeys(RATINGS, ""),
            **self.view.fields,
            **(self.view.saved or SAVED_DEFAULT),
            # Shown under the full-size backdrop until that has loaded.
            "BackdropQuick": self.view.quick_backdrop,
            "Page": self.view.page,
            "EpisodeStatus": "",
            "EpisodeLoading": "",
        })
        self.show_primary()

    def refresh(self):
        self.play_wanted = None  # a held Play press belongs to the page it was made on
        self.paint()
        self.refresh_saved_actions()
        if self.view.page == "episodes":
            self.show_seasons()
        elif self.view.page == "related":
            self.show_related()
        self.restore_focus()

    def refresh_saved_actions(self, action=None):
        from netflix_saved_lists import (
            ListsError, bookmark_item, favorite_item, media_item, more_watchlists, notify, saved_properties,
        )

        # Each SeriesView owns its own state, including nested recommendations. paint() has already put
        # the last known state (or the plain icons) on the page; this confirms or corrects it in place.
        try:
            fields = self.view.fields
            item = media_item({"type": "tvshow", "id": fields["ID"],
                               **{key.lower(): fields.get(key, "") for key in
                                  ("Title", "Plot", "Meta", "Poster", "Backdrop", "Logo")}})
            if action == FAVORITE and not favorite_item(item):
                return
            if action == BOOKMARK and not bookmark_item(item):
                return
            if action == MORE_LISTS and not more_watchlists(item):
                return
            self.view.saved = saved_properties(item)
            self.properties(**self.view.saved)
            known_store(self.view, saved=remembered(self.view.saved))   # R10.18: adding to a watchlist is not announced (errors below still notify)
        except (ListsError, OSError, RuntimeError, ImportError, KeyError) as error:
            if action is not None:
                notify(str(error) if isinstance(error, ListsError) else "Could not update saved titles. Try again.")

    def restore_focus(self):
        view = self.view
        if view.page == "related":
            focus = RECOMMENDATIONS if view.related else SECONDARY_STATUS
        elif view.page == "episodes":
            if not view.seasons:
                focus = SECONDARY_STATUS
            elif view.focus == EPISODES and view.episodes.get(self.active):
                focus = EPISODES
            else:
                focus = SEASONS
        else:
            focus = view.focus
            if view.primary_pending:
                if focus not in STAYS:  # those are on screen while episodes load
                    focus = PRIMARY
            elif focus in {PRIMARY, RESTART} and not view.primary:
                focus = PRIMARY if view.retry else MORE
            elif focus == RESTART and not view.resumable:
                focus = PRIMARY
        self.pending_focus = focus
        self.apply_pending_focus()

    def apply_pending_focus(self):
        focus = self.pending_focus
        if focus is not None and xbmc.getCondVisibility(f"Control.IsVisible({focus})"):
            if self.focus_id() != focus:
                self.setFocusId(focus)
            self.pending_focus = None

    def focus_id(self):
        try:
            return self.getFocusId()
        except RuntimeError:
            return 0

    def show_primary(self):
        view = self.view
        episode = view.primary
        title, plot, play, restart, status = "", view.fields["Plot"], "1", "", ""
        if view.primary_pending:
            # Nothing is blanked while episodes load: the show's plot and every button stay where they
            # will be. Only the Play label ("Play" -> "Play S1: E1") and the episode line fill in later.
            label = "Play"
        elif episode:
            label = f"{'Resume' if view.resumable else 'Watch Again' if view.again else 'Play'} {episode['code']}"
            if view.own_text:
                # The Play button already says "S1: E1"; the heading is just the episode name.
                title, plot = episode["label"], episode["plot"] or plot
            plot = plot or episode["plot"]  # a show without a plot of its own
            restart = "1" if view.resumable else ""
            if view.recheck_failed:
                status = "Couldn’t check for newer progress. Press Play to try again."
        elif view.retry:
            label, status = "Try Again", "Couldn’t load episodes."  # same button, same place
        else:
            label, play = "", ""
            status = "No episodes available." if view.seasons == [] else "Choose an episode to play."
        if self.play_wanted and self.play_wanted[0] == PRIMARY:
            label = "Getting ready…"
        elif self.play_wanted and self.play_wanted[0] == RESTART:
            status = "Getting ready…"
        # ActionsReady is always 1 now; it is still written because the XML conditions name it.
        self.properties(ActionsReady="1", PrimaryLabel=label, EpisodeTitle=title, Plot=plot,
                        CanPlay=play, CanRestart=restart, Status=status)

    def settle_focus(self, was_pending=False):
        """Main page, after an answer: move the highlight only off a button that has gone, or from Play to
        the button a returning viewer was on. A viewer who moved to another button is left alone."""
        view = self.view
        if view.page:
            return
        focus = self.focus_id()
        gone = ((focus in {PRIMARY, RESTART} and not view.primary and not view.primary_pending and not view.retry)
                or (focus == RESTART and not view.resumable))
        if gone:
            view.focus = focus
        if gone or (was_pending and not view.primary_pending and focus == PRIMARY):
            self.restore_focus()

    def current_season(self):
        return self.view.seasons[self.view.season_index] if self.view.seasons else None

    def show_seasons(self):
        view = self.view
        self.season_list.reset()
        items = []
        for index, season in enumerate(view.seasons or ()):
            item = xbmcgui.ListItem(label=season["label"], offscreen=True)
            if season["count"] is not None:
                item.setProperty("Count", quantity(season["count"], "Episode"))
            if index == view.season_index:
                item.setProperty("Selected", "1")
            items.append(item)
        if items:
            self.season_list.addItems(items)
            self.season_list.selectItem(view.season_index)
        self.show_episodes()

    def show_episodes(self):
        view = self.view
        season = self.current_season()
        self.episode_list.reset()
        self.active = None
        self.properties(SeasonLabel=season["label"] if season else "", EpisodeStatus="")
        loading = ""   # R10.10a: "1" = grey cards (Netflix_Series.xml) instead of a "Loading ..." line
        if not season:
            message = ("Couldn’t load seasons." if "seasons" in view.failed
                       else "No seasons available." if view.seasons == [] else "")
            loading = "" if message else "1"
        else:
            self.active = season["number"]
            episodes = view.episodes.get(self.active)
            if episodes:
                items = []
                for episode in episodes:
                    item = xbmcgui.ListItem(label=episode["label"], offscreen=True)
                    item.setArt({"thumb": episode["thumb"]})
                    item.getVideoInfoTag().setPlot(episode["plot"])
                    item.setProperty("Code", episode["code"])
                    item.setProperty("Progress", str(episode["progress"]))
                    if episode["runtime"]:
                        item.setProperty("Duration", f"{max(1, episode['runtime'] // 60)}m")
                    items.append(item)
                self.episode_list.addItems(items)
                index = view.episode_positions.get(self.active)
                if index is None and view.primary in episodes:
                    index = episodes.index(view.primary)
                self.episode_list.selectItem(min(index or 0, len(episodes) - 1))
                self.properties(EpisodeLoading="")
                return
            message = ("Couldn’t load episodes. Select this season to retry." if self.active in view.failed
                       else "No episodes available." if episodes == [] else "")
            loading = "" if message else "1"
        self.properties(EpisodeStatus=message, EpisodeLoading=loading)

    def show_related(self):
        view = self.view
        self.related_list.reset()
        items = []
        for fields, thumb in view.related or ():
            item = xbmcgui.ListItem(label=fields["Title"], offscreen=True)
            item.setArt({"thumb": thumb})
            item.getVideoInfoTag().setPlot(fields["Plot"])
            items.append(item)
        loading_cards = ""
        if items:
            self.related_list.addItems(items)
            self.related_list.selectItem(view.related_index)
            message = ""
        elif "related" in view.failed:
            message = "Couldn’t load similar shows."
        elif view.related == []:
            message = "No similar shows available."
        else:
            # R10-playback (backlog 9c): straight to the grey loading cards (Netflix_Series.xml), no text.
            message, loading_cards = "", "1"
        self.properties(EpisodeStatus=message, RelatedLoading=loading_cards)

    def set_page(self, page, focus):
        view = self.view
        if page != "related":
            self.properties(RelatedLoading="")
        view.page, view.focus = page, focus
        if page == "episodes":
            view.failed.discard("seasons")
            season = self.current_season()
            if season:
                view.failed.discard(season["number"])
        elif page == "related":
            view.failed.discard("related")
        self.refresh()

    def show_season_count(self, view, season):
        # Season rows are built once; a count learned later is written into the existing row.
        if (view is not self.view or view.page != "episodes" or season["count"] is None
                or not view.seasons or season not in view.seasons):
            return
        index = view.seasons.index(season)
        if index < self.season_list.size():
            self.season_list.getListItem(index).setProperty("Count", quantity(season["count"], "Episode"))

    def update_season(self):
        if self.view.page != "episodes" or not self.view.seasons or self.focus_id() != SEASONS:
            return
        index = self.season_list.getSelectedPosition()
        if 0 <= index < len(self.view.seasons) and index != self.view.season_index:
            self.view.season_index = index
            self.season_moved = monotonic()
            for position in range(len(self.view.seasons)):
                self.season_list.getListItem(position).setProperty("Selected", "1" if position == index else "")
            self.show_episodes()

    def enter_episodes(self):
        self.update_season()
        view = self.view
        if self.active in view.failed:
            view.failed.discard(self.active)
            self.show_episodes()
        elif view.episodes.get(self.active):
            view.focus = EPISODES
            self.setFocusId(EPISODES)

    def leave_episodes(self):
        self.view.episode_positions[self.active] = self.episode_list.getSelectedPosition()
        self.view.focus = SEASONS
        self.setFocusId(SEASONS)

    def onAction(self, action):
        if self.closed or not self.ready:
            return
        action = action.getId()
        view = self.view
        if self.play_request:
            # Back cancels until POV has the stream (Phase "play": the loader shows it is committed; review M3).
            if action in BACK and xbmcgui.Window(10000).getProperty("Netflix.Play.Phase") != "play":
                stop_loading(self.play_token)  # POV reads it as cancelled; the loop puts the page back
            return
        if action in BACK:
            if view.page:
                self.set_page("", MORE if view.page == "episodes" else RELATED)
            elif len(self.stack) > 1:
                self.stack.pop()
                self.refresh()
            else:
                self.closed = True
        elif action == CONTEXT_MENU and not view.page and self.focus_id() == BOOKMARK:
            self.request_saved(view, MORE_LISTS)   # R10.18: Menu key / long-press OK on the Watchlist button: the picker with checks
        elif view.page == "episodes":
            focus = self.focus_id()
            if action == RIGHT and focus == SEASONS:
                self.enter_episodes()
            elif action == LEFT and focus == EPISODES:
                self.leave_episodes()

    def request_saved(self, view, action):
        """R10.18: queue a Favorite / Watchlist action unless one runs, or one finished less than SAVED_COOLDOWN ago (each refused request in the
        cooldown renews it, so a held OK is one toggle however long it is held)."""
        since = monotonic() - self.saved_done
        if since < SAVED_COOLDOWN:
            if since >= 0:
                self.saved_done = monotonic()
            return
        if self.detail_request is None:
            self.detail_request = (view, action)

    def onClick(self, control):
        if self.closed or self.play_request:
            return
        if not self.ready:
            if control in {PRIMARY, RESTART} and not self.view.page:
                self.early_click = (control, monotonic())
            return
        view = self.view
        if view.page == "episodes":
            if control == SEASONS:
                self.enter_episodes()
            elif control == SECONDARY_STATUS and "seasons" in view.failed:
                view.failed.discard("seasons")
                self.show_seasons()
            elif control == EPISODES:
                index = self.episode_list.getSelectedPosition()
                episodes = view.episodes.get(self.active) or []
                if 0 <= index < len(episodes):
                    view.episode_positions[self.active] = index
                    view.focus = EPISODES
                    self.press_episode(episodes[index])
        elif view.page == "related":
            if control == SECONDARY_STATUS and "related" in view.failed:
                view.failed.discard("related")
                self.show_related()
                return
            index = self.related_list.getSelectedPosition()
            if control == RECOMMENDATIONS and view.related and 0 <= index < len(view.related):
                fields = view.related[index][0]
                view.related_index = index
                view.focus = RECOMMENDATIONS
                if len(self.stack) == 6:
                    del self.stack[1]
                self.stack.append(SeriesView(fields, from_row=False))
                self.refresh()
        elif control in {FAVORITE, BOOKMARK}:
            self.request_saved(view, control)
        elif control in DETAIL_ACTIONS:
            if self.detail_request is None:
                self.detail_request = (view, control)
        elif control in {PRIMARY, RESTART}:
            self.press_play(control)
        elif control == MORE:
            self.set_page("episodes", SEASONS)
        elif control == RELATED:
            self.set_page("related", RECOMMENDATIONS)

    @property
    def busy(self):
        """A request is out, the first load is pending, or a remembered show has not been re-checked yet."""
        return self.pending is not None or self.view.primary_pending or bool(self.view.revalidate)

    def press_play(self, control, when=None):
        view = self.view
        if (monotonic() if when is None else when) - self.shown_at < PLAY_GRACE:
            return  # the tail of the OK that opened the page, not a choice to play
        if view.retry:
            if control == PRIMARY:  # "Try Again"
                view.failed.discard("seasons")
                view.failed.discard(view.primary_season)
                self.show_primary()
            return
        if view.recheck_failed:
            view.recheck_failed, view.revalidate = False, "seasons"  # never play what could not be confirmed
        view.focus = control
        if self.busy:
            # A request is out (first load, or the re-check of a remembered show): keep the press, with the
            # episode the button named, and act on it when the answer is in ONLY if the answer still means
            # that. PlayMedia never starts beside a request, and never against a remembered target.
            episode = view.primary
            self.play_wanted = (control, (episode["season"], episode["episode"]) if episode else None, view.again)
            self.show_primary()
        elif view.primary:
            self.play(view.primary, restart=control == RESTART)

    def press_episode(self, episode):
        """OK on an episode in More Episodes: the same rule as Play. What is on screen may be remembered data."""
        view = self.view
        if view.recheck_failed:
            view.recheck_failed, view.revalidate = False, "seasons"
        if self.busy:
            self.play_wanted = (EPISODES, (episode["season"], episode["episode"]), False)
            self.properties(EpisodeStatus="Getting ready…")
        else:
            self.play(episode)

    def resolve_play(self):
        view = self.view
        if self.play_wanted is None or self.busy:
            return
        (control, named, again), self.play_wanted = self.play_wanted, None
        episode = view.primary
        current = (episode["season"], episode["episode"]) if episode else None
        if control == EPISODES:
            # Play the pressed episode as the fresh list has it (its progress decides resume or not).
            fresh = next((item for item in view.episodes.get(named[0]) or ()
                          if item["episode"] == named[1]), None) if view.page == "episodes" else None
            if view.recheck_failed:
                self.properties(EpisodeStatus="Couldn’t check for newer progress. Select the episode to try again.")
            elif fresh:
                self.play(fresh)
            else:
                self.properties(EpisodeStatus="")
            return
        # Main page: act only if the answer still means what the button said when it was pressed. A press on
        # plain "Play" during the first load named no episode and follows whatever loaded, except onto
        # "Watch Again", which nobody was shown.
        same = current is not None and (named == current and again == view.again if named else not view.again)
        if same and not view.recheck_failed and not view.page and (control == PRIMARY or view.resumable):
            self.play(episode, restart=control == RESTART)
        if not self.closed:  # nothing (or no longer that) to play: show what the answer said instead
            self.show_primary()
            self.settle_focus()

    def play(self, episode, restart=False):
        if episode["unaired"]:
            self.properties(EpisodeStatus="This episode hasn’t aired yet.")
            return
        resumable = 0 < episode["progress"] < 100
        if restart and not resumable:
            return
        params = {
            "mediatype": "episode", "season": episode["season"], "episode": episode["episode"],
            "netflix_series": "true",
        }
        # R10-playback (owner): the page's choice is final, POV never asks "Resume / Start Over" on top: "Resume"
        # resumes, every other press (Play, Watch Again, Play From Beginning, an episode row) starts at 0.
        params["netflix_resume"] = "resume" if resumable and not restart else "restart"
        # R10-playback: the request's token names it for POV (quiet mode: no loading window of its own, facts and
        # phase published for the loader) and is what Back withdraws. POV names the episode's source mid-load.
        self.play_token = uuid4().hex
        params["netflix_playback_cover"] = self.play_token
        params["netflix_play_facts"] = "true"
        # Playing changes what Play should do next, however this playback ends.
        remember(CACHE, CACHE_SHOWS, show_key(self.view.tmdb_id), None)
        self.play_request = pov_url("play_media", self.view.tmdb_id, **params)
        resuming = params.get("netflix_resume") == "resume"
        start_loading(self.play_token, verb="Resuming" if resuming else "Starting", phase="find",
                      watched="%d%% watched" % episode["progress"] if resuming else "")
        self.cover = hold_cover(self.view.fields.get("Backdrop") or self.view.quick_backdrop)
        self.cover_renewed = monotonic()
        self.cover_release_at = None
        self.pending_focus = LOADER_FOCUS

    def launch_play(self):
        """PlayMedia, from this page's loop, once nothing else of POV's is out: this page's own request, any show
        page's request (and QUIET after it), and any title page's launch in flight. The page stays up meanwhile."""
        if not self.play_request or self.launched is not None:
            return
        if (self.pending is not None or self.worker is not None or holder(REQUEST, REQUEST_STALE)
                or monotonic() - self.ended < QUIET or pov_in_flight()
                or xbmc.getCondVisibility("System.HasAlarm(netflix_pov_leaving)")):
            return
        # staged/exit-confirm: Exit / Stop does not reopen a movie page for an episode; open_series reopens this one.
        self.player = SeriesPlayer(self.play_request)
        xbmcgui.Window(10000).setProperty("Netflix.Exit.Return", json.dumps({"kind": "episode", "ID": self.player.episode[0]}))
        xbmc.executebuiltin(f'PlayMedia("{self.play_request}")')
        self.launched = monotonic()

    def end_play(self):
        """The request ended without video (Back, no source, an error): the page is usable again. A new Play waits
        in launch_play() until POV's interpreter has left (pov_in_flight)."""
        stop_loading(self.play_token)
        release_cover(self.cover)
        self.play_request = self.play_token = self.player = self.launched = self.cover = None
        self.cover_release_at = None
        if not self.closed:
            self.show_primary()
            self.restore_focus()

    def watch_play(self):
        if not self.play_request:
            return
        now = monotonic()
        if self.cover and now - self.cover_renewed >= COVER_RENEW:
            self.cover_renewed = now
            renew_cover(self.cover)
        if loading(self.play_token) or (self.player is not None and self.player.started):
            return
        self.end_play()

    def watch_cover(self):
        """A page reopened after an episode: the cover handed over by open_series comes down GRACE after the page
        is up (its own backdrop loads under the cover's copy meanwhile)."""
        if not self.cover or self.play_request:
            return
        now = monotonic()
        if self.cover_release_at is None:
            self.cover_release_at = now + GRACE
        elif now >= self.cover_release_at:
            release_cover(self.cover)
            self.cover = None

    def request(self, key, url):
        view = self.view
        if not stake(REQUEST, self.owner, REQUEST_STALE):
            return  # another page's request is still out: asked again on the next loop turn
        self.pending = (view, key)
        self.requested = self.renewed = monotonic()

        def load():
            rows = None
            try:
                sleep(SETTLE)
                rows = directory(url) if holds(REQUEST, self.owner) else LOST
            finally:
                self.ended = monotonic()
                release(REQUEST, self.owner)
                self.result.put((view, key, rows))

        self.worker = Thread(target=load)
        self.worker.start()

    def renew_claims(self):
        """Signs of life: a running request and an open page never look abandoned to another page."""
        now = monotonic()
        if self.opener and now - self.opening_renewed >= RENEW:
            self.opening_renewed = now
            renew(OPENING, self.opener)
        if now - self.renewed >= RENEW:
            self.renewed = now
            if self.worker and now - self.requested < REQUEST_LIMIT:
                renew(REQUEST, self.owner)

    def finish_worker(self):
        # The dialog can close immediately, but Kodi must finish the provider
        # call before this script and its Python interpreter are torn down.
        if self.worker:
            while self.worker.is_alive():
                self.worker.join(RENEW)
                self.renew_claims()
            self.worker = None

    def check_result(self):
        try:
            view, key, rows = self.result.get_nowait()
        except Empty:
            return
        self.finish_worker()
        self.pending = None
        if view not in self.stack:
            return
        if rows is LOST:
            return
        was_pending = view.primary_pending
        process = True
        if view.revalidate == ("seasons" if key == "seasons" else "episodes" if key == view.primary_season else None):
            # The answer to a re-check of a remembered show: keep what is shown unless POV says otherwise.
            view.revalidate = ""
            if rows is None:
                view.recheck_failed, process = True, False
            elif key == "seasons":
                if season_rows(rows, view.tmdb_id) == view.seasons:
                    process = False
                    inline = episode_rows(rows, view.tmdb_id, view.primary_season)
                    if not inline:  # a POV without the inline patch: ask for the Play season next
                        view.revalidate = "episodes"
                        return
                    if inline != view.episodes.get(view.primary_season):
                        self.take_episodes(view, view.primary_season, inline)
                        if view is self.view and view.page == "episodes" and self.active == view.primary_season:
                            self.show_episodes()
                else:
                    view.restore_season = view.seasons[view.season_index]["number"]
                    view.primary = None
                    view.episodes.pop(view.primary_season, None)
        if not process:
            pass
        elif rows is None:
            view.failed.add(key)
        elif key == "seasons":
            view.seasons = season_rows(rows, view.tmdb_id)
            ordinary = [season for season in view.seasons if season["number"] > 0] or view.seasons
            preferred = (next((season for season in view.seasons if season["primary"]), None)
                         or next((season for season in ordinary if not season["watched"] and season["count"] != 0), None)
                         or next(iter(ordinary), None))
            if preferred:
                view.primary_season = preferred["number"]
                selected = next((season for season in view.seasons if season["number"] == view.restore_season), preferred)
                view.season_index = view.seasons.index(selected)
            view.restore_season = None
            if not view.fields["ShowMeta"]:
                regular_seasons = len([season for season in view.seasons if season["number"] > 0])
                if regular_seasons:
                    count = quantity(regular_seasons, "Season")
                    view.fields["ShowMeta"] = count
                    view.fields["Meta"] = SEPARATOR.join(filter(None, (view.fields["Meta"], count)))
            # A patched POV answers with the primary season's episodes in the same listing (one launch).
            inline = episode_rows(rows, view.tmdb_id, view.primary_season) if preferred else []
            if inline:
                self.take_episodes(view, view.primary_season, inline)
        elif key == "related":
            view.related = related_rows(rows, view.tmdb_id)
        elif isinstance(key, tuple):
            # Background count lookup for a season whose row came without one; episodes are not cached.
            season = next((season for season in view.seasons or () if season["number"] == key[1]), None)
            if season and season["count"] is None:
                season["count"] = len(episode_rows(rows, view.tmdb_id, key[1]))
                self.show_season_count(view, season)
            return
        else:
            self.take_episodes(view, key, episode_rows(rows, view.tmdb_id, key))
        if view is not self.view:
            return
        if not process:
            pass
        elif key == "seasons":
            self.properties(ShowMeta=view.fields["ShowMeta"], Meta=view.fields["Meta"])
            if view.page == "episodes":
                self.show_seasons()
                self.restore_focus()
        elif key == "related" and view.page == "related":
            self.show_related()
            self.restore_focus()
        elif view.page == "episodes" and key == self.active:
            self.show_episodes()
            if view.focus == EPISODES and view.episodes.get(key):
                self.restore_focus()
        self.show_primary()
        self.settle_focus(was_pending)

    def take_episodes(self, view, key, episodes):
        view.episodes[key] = episodes
        if len(view.episodes) > CACHE_SIZE:
            view.episodes.popitem(last=False)
        season = next(season for season in view.seasons if season["number"] == key)
        if season["count"] is None:
            season["count"] = len(episodes)
            self.show_season_count(view, season)
        if key == view.primary_season:
            set_primary(view, season, episodes)
            cache_store(view)

    def load_needed(self):
        view = self.view
        if self.pending or self.closed or self.play_request:
            return  # R10: no new POV request while a play request waits or runs
        if view.page == "related" and view.related is None and "related" not in view.failed:
            self.request("related", pov_url("build_tvshow_list", view.tmdb_id, action="tmdb_tv_recommendations"))
            return
        if view.seasons is None or view.revalidate == "seasons":
            if "seasons" not in view.failed:
                self.request("seasons", pov_url("build_season_list", view.tmdb_id, netflix_series="true",
                                                netflix_inline="primary"))
            return
        if view.revalidate == "episodes":
            self.request(view.primary_season, view.seasons[view.season_of(view.primary_season)]["url"])
            return
        season = self.current_season() if view.page == "episodes" else None
        if not season and not view.primary and view.page != "related":
            season = next((row for row in view.seasons if row["number"] == view.primary_season), None)
        if not season:
            return
        key = season["number"]
        if key in view.episodes:
            view.episodes.move_to_end(key)
        elif key not in view.failed:
            self.request(key, season["url"])
            return
        # Seasons list open, focused season settled: look up one missing count at a time.
        if view.page == "episodes" and monotonic() - self.season_moved >= COUNT_REST:
            missing = next((row for row in view.seasons if row["count"] is None
                            and row["number"] not in view.episodes and ("count", row["number"]) not in view.failed), None)
            if missing:
                self.request(("count", missing["number"]), missing["url"])

    def update_ratings(self, now=None):
        """Ratings that were not ready when the page opened are put in place when TMDb Helper has them
        for this show (its id, its type, not mid-update: see ratings()). Only ever adds; keeps looking, at
        most twice a second, until all three are there or RATINGS_WAIT seconds have passed for this show."""
        view, now = self.view, monotonic() if now is None else now
        if view.ratings_until is None:
            view.ratings_until = now + RATINGS_WAIT
        if (now > view.ratings_until or now - self.ratings_checked < RATINGS_EVERY
                or all(view.fields.get(key) for key in RATINGS)):
            return
        self.ratings_checked = now
        values = {key: value for key, value in ratings(view.tmdb_id).items() if not view.fields.get(key)}
        if values:
            view.fields.update(values)
            self.properties(**values)
            known_store(view, ratings={key: view.fields[key] for key in RATINGS if view.fields.get(key)})

    def run(self, monitor):
        self.update_ratings()  # one more look before painting: usually TMDb Helper has them by now
        self.paint()  # the first frame already carries everything known from the browse row
        if self.opener and not holds(OPENING, self.opener):
            self.closed = True  # a second OK on the same title: the other script's page is the one that opens
            return
        self.renew_claims()
        self.show()
        self.shown_at = monotonic()
        window = xbmcgui.getCurrentWindowDialogId()

        def up():
            # R10: while a play request is loading, another dialog may sit on top (POV's resume question, a busy
            # spinner): the page is still this loop's until it is closed.
            return xbmcgui.getCurrentWindowDialogId() == window or (
                self.play_request is not None and xbmc.getCondVisibility(f"Window.IsVisible({window})"))

        try:
            while not self.closed and not monitor.abortRequested() and up():
                if self.ready and not self.closed:
                    if self.detail_request is not None:
                        view, action = self.detail_request
                        # Own the modal on this loop. While it is open the
                        # current dialog ID belongs to the picker, not the series.
                        if view is self.view and not view.page:
                            if action == DESCRIPTION:
                                plot = self.getProperty("Series.Plot")
                                if plot:
                                    xbmcgui.Dialog().ok(view.fields["Title"], plot)
                            else:
                                started = monotonic()
                                self.saved_done = float("inf")
                                try:
                                    self.refresh_saved_actions(action)
                                finally:
                                    self.saved_done = max(monotonic(), started + SAVED_GUARD - SAVED_COOLDOWN)
                        self.detail_request = None
                    self.check_result()
                    self.resolve_play()
                    self.apply_pending_focus()
                    self.update_season()
                    self.update_ratings()
                    self.load_needed()
                    self.launch_play()
                    self.watch_play()
                    self.watch_cover()
                self.renew_claims()
                waiting = (self.pending is not None or self.pending_focus is not None or not self.ready
                           or self.play_wanted is not None or self.play_request is not None)
                if monitor.waitForAbort(0.04 if waiting else 0.09):
                    break
        finally:
            if up():
                self.close()


class SeriesPlayer(xbmc.Player):
    def __init__(self, url):
        super().__init__()
        _, params = plugin(url)
        self.episode = (params["tmdb_id"], int(params["season"]), int(params["episode"]))
        self.started = False
        self.finished = False
        self.unrelated = False

    def onAVStarted(self):
        # Ignore an independent player launch if this request was cancelled.
        if not self.started:
            try:
                tag = self.getVideoInfoTag()
                self.unrelated = (tag.getUniqueID("tmdb"), tag.getSeason(), tag.getEpisode()) != self.episode
            except RuntimeError:
                self.unrelated = True
        self.started = not self.unrelated
        self.finished = False

    def onPlayBackStopped(self):
        self.finished = True

    def onPlayBackEnded(self):
        self.finished = True

    def onPlayBackError(self):
        self.finished = True


def pov_in_flight():
    """A POV launch from a title page has not ended yet (a page's source check, or a play request still running or
    leaving: POV keeps a "play" entry for its whole run). PlayMedia waits, so two POV interpreters never overlap."""
    parts = xbmcgui.Window(10000).getProperty(FLIGHT).split("|")
    try:
        return time() < float(parts[4])
    except (IndexError, ValueError):
        return False


def exit_fast():
    """staged/exit-confirm: playback was just left with Exit / Stop (netflix_exit_return.py): come back sooner,
    but only once the window underneath is not loading a list."""
    try:
        recent = time() - float(xbmcgui.Window(10000).getProperty("Netflix.Exit.Fast")) < 6
    except ValueError:
        return False
    return recent and not xbmc.getCondVisibility("Container.IsUpdating")


def play_and_wait(player, request, monitor, origin, current_origin, cover=None):
    """After the page launched the episode (SeriesDialog.launch_play) and closed into video: wait for the playback
    to end and the window underneath to settle, then True (open_series reopens the page). The transition cover is
    renewed all the while, so the stop and the wait show the backdrop, never the browse page. The settle waits
    are unchanged (they keep the page's own POV requests away from the Videos list load)."""
    start_deadline = monotonic() + 90
    idle_since = None
    renewed = browse_since = None
    while not monitor.waitForAbort(0.25):
        now = monotonic()
        if cover and (renewed is None or now - renewed >= COVER_RENEW):
            renewed = now
            renew_cover(cover)
        if player.started:
            stop_loading(request)
        if player.unrelated:
            return False
        window = xbmcgui.getCurrentWindowId()
        if window == origin[1] and xbmcgui.getCurrentWindowDialogId() == origin[-1]:
            if current_origin() != origin:
                return False
        elif window not in {origin[1], FULLSCREEN_VIDEO}:
            return False
        if not player.started and not player.finished and (monotonic() >= start_deadline or not loading(request)):
            return False  # no video came (POV ended, or the request was withdrawn)
        if cover and window != FULLSCREEN_VIDEO and player.isPlayingVideo():
            # The video plays on with the browse page in front: the viewer chose that page, so it is not covered.
            browse_since = browse_since or now
            if now - browse_since >= 1.5:
                release_cover(cover)
                cover = None
        else:
            browse_since = None
        if not player.finished or player.isPlayingVideo() or window == FULLSCREEN_VIDEO:
            idle_since = None
        elif xbmcgui.getCurrentWindowDialogId() == origin[-1]:
            now = monotonic()
            idle_since = idle_since or now
            if now - idle_since >= (0.4 if exit_fast() else 1.25):
                return True
        else:
            idle_since = None
    return False


def open_series(source):
    native = source == "native"
    if native:
        source = info("System.CurrentControlID")
        if xbmcgui.getCurrentWindowId() != VIDEOS or source not in NATIVE_LISTS:
            xbmc.executebuiltin("Action(Select)")
            return
    monitor = xbmc.Monitor()
    opener = f"{id(monitor)}.{monotonic()}"
    if not stake(OPENING, opener, OPENING_STALE):
        return  # a second OK: another script is already opening, or showing, a show page
    staked = monotonic()
    cover = None   # R10: the transition cover, from Play until the page is back after the episode
    try:
        fields = capture(source)
        if not fields:
            if native:
                xbmc.executebuiltin("Action(Select)")
            return

        def current_origin():
            return (
                xbmc.getSkinDir(), xbmcgui.getCurrentWindowId(), info("Container.FolderPath"),
                info(f"Container({source}).ListItem.FileNameAndPath"), xbmcgui.getCurrentWindowDialogId(),
            )

        origin = current_origin()
        stack = [SeriesView(fields)]
        while not monitor.abortRequested():
            dialog = SeriesDialog("Netflix_Series.xml", "special://skin", "Default", "1080i")
            dialog.stack = stack
            dialog.cover = cover  # handed over: it comes down once this page is up (watch_cover)
            if opener:
                sleep(max(0.0, SETTLE - (monotonic() - staked)))  # normally already past
                dialog.opener = opener
            try:
                dialog.run(monitor)
                if opener:
                    release(OPENING, opener)  # the page is gone: the next OK may open one
                    opener = None
                cover = dialog.cover
                if not dialog.play_request or monitor.abortRequested():
                    break
                if dialog.launched is None:
                    # The page went away before PlayMedia (its loop launches only when no other POV call is out).
                    stop_loading(dialog.play_token)
                    break
                if not play_and_wait(dialog.player, dialog.play_token, monitor, origin, current_origin, cover):
                    break
                stack[-1].invalidate_playback()
            finally:
                dialog.finish_worker()
    finally:
        if opener:
            release(OPENING, opener)
        release_cover(cover)


if __name__ == "__main__" and len(sys.argv) == 2:
    open_series(sys.argv[1])
