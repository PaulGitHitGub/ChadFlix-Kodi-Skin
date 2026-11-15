"""Open movie details and dispatch series selection in the native Kodi catalog."""
import json
import os
import re
import sys
from pathlib import Path
from http.client import HTTPSConnection, HTTPException
from time import monotonic, time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs


_EXTRAS = str(Path(__file__).resolve().parent)
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)
from netflix_artwork import detail_backdrop
from netflix_playback_cover import (LOADING, hold as hold_cover, release as release_cover, renew as renew_cover,
                                    set_loading, start_loading, stop_loading)


DETAILS = 11167
MOVIE_INFO = 12003
FULLSCREEN_VIDEO = 12005
NO_DIALOG = 9999
VIDEOS = 10025
POV = "plugin.video.pov"
TMDB_HELPER = "plugin.video.themoviedb.helper"
YOUTUBE = "plugin.video.youtube"
PREFIX = "Netflix.Movie."
DETAIL_FIELDS = ("ID", "Origin", "Title", "Plot", "Meta", "Backdrop", "Poster", "Logo", "Action", "Trailer", "Percent")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# R9 movie page: the source check (POV's netflix_quality_preview route) starts from this named alarm a moment after
# the page is up, so it never starts in the same instant as this script. Closing the page cancels it (onunload)
# unless it is within half a second of going off.
QUALITY_ALARM = "NetflixQualityPreview"
QUALITY_DELAY, QUALITY_SECONDS = "00:01", 1.0  # mm:ss for AlarmClock, and the same in seconds
QUALITY_START_GRACE = 4.0  # how long a "pending" entry stays valid after the alarm's time (POV's interpreter starting)
# Single flight: ONE property names the one POV launch from the movie page that is in flight, for all movies:
# "<pending|check|done|play>|<movie>|<page token>|<due or start>|<valid until>" (see POV's netflix_quality_preview.py).
# No check is scheduled and no Play is launched while it is valid; an owner that crashed frees it by not renewing.
FLIGHT = "Netflix.Flight"
PLAY_PENDING = "Netflix.Flight.Play"  # "<valid until>": an OK on Play is being handled; a second OK is ignored
PLAY_WAIT = 60.0   # longest Play waits for the launch in flight to end (a dead one frees itself in 10 to 20 s)
PLAY_HOLD = 6.0    # validity of the "play" entry: POV's interpreter starting; nothing else launches beside it
QUALITY_TTL = 600  # RESULT_TTL in plugin.video.pov/resources/lib/modules/netflix_quality_preview.py
POV_DATA = "special://profile/addon_data/plugin.video.pov/"
# R10-playback: the play loader (Includes_Netflix_Playback.xml) parks focus on this invisible catcher of the page;
# its Back withdraws the request (clears Netflix.Play.Loading), which POV and settle_quality() read as "cancelled".
LOADER_FOCUS = 42
FACT_FIELDS = ("Res", "SpecA", "SpecB")   # Netflix.Movie.QualityHighest*: POV's netflix_quality_preview FACT_FIELDS
# R10.9: the ratings row reads TMDb Helper's list-item monitor, which describes the title focused on the browse page
# under this page. After playback or a trailer the browse focus is not that title any more, so the page keeps its own
# copy (Netflix.Movie.<field>, Custom_1167's second ratings row), carried through the return stash.
RATING_FIELDS = ("IMDb_Rating", "RottenTomatoes_Rating", "Letterboxd_Rating")
KEPT_RATING_FIELDS = RATING_FIELDS + ("RottenTomatoes_Icon",)
LEAVE_AFTER_LAUNCH = 0.3  # seconds play_movie() stays after PlayMedia, so it never ends as POV starts
COVER_SECONDS = 20        # fail-safe of the transition cover held by Play (renewed while anyone still works on it)


def info(label):
    return xbmc.getInfoLabel(label).strip()


def valid_id(value):
    return value if value.isascii() and value.isdecimal() and int(value) > 0 else ""


def youtube_id(value):
    if not isinstance(value, str):
        return ""
    try:
        url = urlsplit(value)
    except ValueError:
        return ""
    query = parse_qs(url.query)
    path = url.path.rstrip("/")
    if url.scheme == "plugin" and url.netloc == YOUTUBE and path == "/play":
        video_id = query.get("video_id", [""])[0]
    elif url.scheme in {"https", "http"} and url.netloc == "youtu.be":
        video_id = path.removeprefix("/")
    elif url.scheme in {"https", "http"} and url.netloc in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"
    }:
        video_id = query.get("v", [""])[0] if path == "/watch" else path.removeprefix("/embed/")
    else:
        video_id = value
    return video_id if VIDEO_ID.fullmatch(video_id) else ""


def lookup_trailer(movie_id):
    # Use POV's configured TMDb access only when the selected item has no trailer.
    connection = HTTPSConnection("api.themoviedb.org", timeout=5)
    try:
        token = xbmcaddon.Addon(POV).getSettingString("tmdb_read_token")
        if not token:
            return ""
        connection.request("GET", f"/3/movie/{movie_id}/videos", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200:
            return ""
        results = json.load(response).get("results", [])
    except (AttributeError, HTTPException, OSError, RuntimeError, ValueError):
        return ""
    finally:
        connection.close()
    if not isinstance(results, list):
        return ""
    videos = [
        (video["type"] == "Trailer", video.get("official") is True, video_id)
        for video in results
        if isinstance(video, dict) and video.get("site") == "YouTube"
        and video.get("type") in {"Trailer", "Teaser"} and (video_id := youtube_id(video.get("key")))
    ]
    best = max(videos, key=lambda video: video[:2], default=(False, False, ""))
    return best[2]


def duration_label(duration):
    parts = duration.split(":")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return duration
    hours, minutes, _ = map(int, parts)
    return " ".join(part for part in (f"{hours}h" if hours else "", f"{minutes}m" if minutes else "") if part)


NATIVE_LISTS = {"590", "591", "593"}  # same set as netflix_series.py


def show_page_claimed():
    """A show page is opening or open (netflix_series.py OPENING / OPENING_STALE): this OK is the second half
    of a double press. Passing it on as Select would press whatever the page has focused, i.e. Play."""
    _, _, stamp = xbmcgui.Window(10000).getProperty("Netflix.Series.Opening").partition("|")
    try:
        return monotonic() - float(stamp) < 3
    except ValueError:
        return False


def open_show_row(source):
    """OK on a show in ANY list of the Videos window (Shows Trending 592, the Shows genre rows 561-570,
    the genre grid 593, the window's own lists 590/591): open the show page from this script instead of
    passing the key on to the list's onclick, which would start a second script right behind this one.
    Same test as those onclicks (Includes_Search.xml 592/593, Includes_Lists.xml rows)."""
    item = f"Container({source}).ListItem."
    path = info(item + "FileNameAndPath")
    if (not source.isdecimal()
            or xbmc.getSkinDir() != "skin.arctic.fuse.3" or info(item + "DBType") != "tvshow"
            or not path.startswith(f"plugin://{POV}/")
            or not (valid_id(info(item + "UniqueID(tmdb)")) or valid_id(info(item + "Property(tmdb_id)"))
                    or "tmdb_id=" in path)):
        return False
    from netflix_series import open_series
    open_series("native" if source in NATIVE_LISTS else source)
    return True


TRENDING_ROOTS = {("build_movie_list", "trakt_movies_trending"): "movies", ("build_tvshow_list", "trakt_tv_trending"): "shows"}


def open_trending_grid(control_id):
    """staged/trending-grid: OK on the "More Titles" tile (POV's next-page folder) at the end of the Trending row
    in the window's own list 591 opens the poster grid over the root, exactly as a genre row's tile does
    (Includes_Lists.xml Netflix_Focus_Rail onclick lines). 591 is the window's view control: Kodi never runs
    a view control's own <onclick>, so this keymap script (which every OK already starts) does it instead of
    passing the key on as Select (which paged the row in place). Page 2 and later keep the old paging."""
    if control_id != "591":
        return False
    root = urlsplit(info("Container.FolderPath"))
    if (root.scheme, root.netloc) != ("plugin", POV):
        return False
    query = parse_qs(root.query)
    route = (query.get("mode", [""])[0], query.get("action", [""])[0])
    tab = TRENDING_ROOTS.get(route)
    tile = info("Container(591).ListItem.FolderPath")
    if (not tab or "new_page" in query or not xbmc.getCondVisibility("Container(591).ListItem.IsFolder")
            or "new_page=" not in tile or ("action=%s&" % route[1]) not in tile + "&"):
        return False
    home = xbmcgui.Window(10000)
    home.clearProperty("Netflix.GenreMenu.Token")
    home.setProperty("Netflix.Grid.Row", "591")
    home.setProperty("Netflix.Grid.Shell", "%s&netflix_tab_genre=%s&netflix_grid_shell=1" % (tile, tab))
    xbmc.executebuiltin("SetFocus(8593)")
    return True


def open_details(source):
    native = source == "native"
    if native:
        catalog = urlsplit(info("Container.FolderPath"))
        control_id = info("System.CurrentControlID")
        if xbmcgui.getCurrentWindowId() == VIDEOS and (open_trending_grid(control_id) or open_show_row(control_id)
                                                       or show_page_claimed()):
            return
        if (
            xbmcgui.getCurrentWindowId() != VIDEOS
            or control_id not in NATIVE_LISTS
            or catalog.netloc != POV
            or xbmc.getSkinDir() != "skin.arctic.fuse.3"
        ):
            xbmc.executebuiltin("Action(Select)")
            return
        source = control_id
    if not source.isdecimal() or xbmcgui.getCurrentWindowDialogId() == DETAILS:
        return

    item = f"Container({source}).ListItem."

    def selected(field):
        return info(item + field)

    path = urlsplit(selected("FileNameAndPath"))
    media_type = selected("DBType")
    series = native and media_type == "tvshow"
    if (media_type != "movie" and not series) or path.scheme != "plugin" or path.netloc not in {POV, TMDB_HELPER}:
        if native:
            xbmc.executebuiltin("Action(Select)")
        return

    params = parse_qs(path.query)
    tmdb_id = next(
        (tmdb_id for value in (selected("UniqueID(tmdb)"), selected("Property(tmdb_id)"),
                               params.get("tmdb_id", [""])[0]) if (tmdb_id := valid_id(value))),
        "",
    )
    if series:
        if tmdb_id:
            # Same interpreter: a second RunScript here would start right behind this one.
            from netflix_series import open_series
            open_series("native")
        else:
            xbmc.executebuiltin("Action(Select)")
        return

    movie_id = tmdb_id
    progress = selected("PercentPlayed")
    resume = progress.isdecimal() and 0 < int(progress) < 100
    year = selected("Year")
    values = {
        "ID": movie_id,
        "Origin": str(xbmcgui.getCurrentWindowId()),
        "Title": selected("Title") or selected("Label"),
        "Plot": selected("Plot"),
        # The age rating is left off this page only; browse rows still show it.
        "Meta": "  ·  ".join(field for field in (
            year if valid_id(year) else "", duration_label(selected("Duration"))
        ) if field),
        "Backdrop": selected("Art(detail_fanart)") or selected("Art(fanart)") or selected("Art(landscape)"),
        "Poster": selected("Art(poster)"),
        "Logo": selected("Art(clearlogo)"),
        "Action": "Resume" if resume else "Play",
        "Trailer": youtube_id(selected("Trailer")),
        "Percent": progress if resume else "",
    }
    show_movie(values)


def cached_quality(movie_id):
    """The panel lines ('view') of the finished source check POV kept for this movie, or None.
    Same validity test as load_result() in POV's netflix_quality_preview.py. Only the settings file's time is read."""
    try:
        base = Path(xbmcvfs.translatePath(POV_DATA))
        record = json.loads((base / "quality_preview" / f"movie-{movie_id}.json").read_bytes())
        try:
            stamp = os.stat(base / "settings.xml").st_mtime_ns
        except OSError:
            stamp = None
        view = record["view"]
        if (record.get("v") == 1 and record.get("movie") == movie_id and 0 <= time() - record["created"] < QUALITY_TTL
                and record.get("settings") == stamp and isinstance(record.get("results"), list)
                and isinstance(view["lines"], dict) and isinstance(view["labels"], dict)
                and isinstance(view["total"], int)):
            return view
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        pass
    return None


def show_quality(home, view):
    """Fill the quality menu from a kept check: the same values POV's publish(view, final=True) writes."""
    total = view["total"]
    count = "%d %s" % (total, "source" if total == 1 else "sources")
    for tier in ("Highest", "Medium", "Lowest"):
        labels = view["labels"].get(tier.lower()) or ("Unavailable", "", "")
        for field, value in zip(("Specs", "Video", "Audio"), labels):
            home.setProperty(PREFIX + "Quality" + tier + field, str(value))
        home.setProperty(PREFIX + "Quality" + tier + "Line", str(view["lines"].get(tier.lower(), "Not available")))
    facts = view.get("facts")
    for field, value in zip(FACT_FIELDS, facts if isinstance(facts, list) and len(facts) == 3 else ("", "", "")):
        home.setProperty(PREFIX + "QualityHighest" + field, str(value))
    home.setProperty(PREFIX + "QualityAllSpecs", count)
    home.setProperty(PREFIX + "QualityAllLine", count if total else "No sources found")
    home.setProperty(PREFIX + "QualityState", "ready")


def page_ratings(home, movie_id):
    """The ratings this page shows now, as Netflix.Movie.* fields for show_movie(): TMDb Helper's values while its
    monitor names this movie (Custom_1167's first row), else the copy the page already carries (second row).
    Reads properties only; called from the Play and Trailer scripts, never per key press."""
    # TMDb Helper's flags IsUpdating / IsUpdatingRatings are not checked here (review 4): when its monitor names this
    # movie, its values are this movie's even mid-update, and a Play pressed then must not lose them.
    live = (bool(movie_id) and home.getProperty("TMDbHelper.ListItem.monitor.tmdb_id") == movie_id
            and home.getProperty("TMDbHelper.ListItem.monitor.tmdb_type") == "movie")
    values = {field: home.getProperty("TMDbHelper.ListItem." + field) for field in RATING_FIELDS} if live else {}
    if not any(values.values()):
        return {field: home.getProperty(PREFIX + field) for field in KEPT_RATING_FIELDS}
    tomatoes, image = values["RottenTomatoes_Rating"], home.getProperty("TMDbHelper.ListItem.RottenTomatoes_Image")
    # the icon Image_ListItem_RottenTomatoes (Includes_Images.xml) picks for the live row
    if not tomatoes:
        values["RottenTomatoes_Icon"] = ""
    elif image in ("certified", "fresh", "rotten"):
        values["RottenTomatoes_Icon"] = {"certified": "certified.png", "fresh": "rtfresh.png", "rotten": "rtrotten.png"}[image]
    else:
        values["RottenTomatoes_Icon"] = "rtfresh.png" if tomatoes.isdecimal() and int(tomatoes) > 59 else "rtrotten.png"
    return values


def flight(home):
    """(kind, movie, token, due) of the launch in flight, or None when there is none or it went stale."""
    parts = home.getProperty(FLIGHT).split("|")
    try:
        return (parts[0], parts[1], parts[2], float(parts[3])) if time() < float(parts[4]) else None
    except (IndexError, ValueError):
        return None


def flight_busy(home):
    """For a page that opens: something is in flight that a new check must not start beside. A "pending" entry
    found here belongs to a page that closed as its alarm went off (the window's onunload clears it otherwise):
    POV is starting. "done" lasts 0.8 s; an alarm set now goes off after it."""
    entry = flight(home)
    return bool(entry) and entry[0] != "done"


def schedule_quality(home, movie_id, token):
    """Set the alarm that starts this page's check, as the launch in flight. False when the page went away."""
    query = urlencode({"mode": "netflix_quality_preview", "tmdb_id": movie_id, "preview": token})
    due = time() + QUALITY_SECONDS      # taken before the call, so it is never later than the real alarm time
    home.setProperty(FLIGHT, "pending|%s|%s|%.2f|%.2f" % (movie_id, token, due, due + QUALITY_START_GRACE))
    # wait=True: Kodi has created the alarm when this returns. The page may have closed just before (its onunload
    # cancelled nothing yet), or Play may have taken the page over (new token): take the alarm back then.
    xbmc.executebuiltin(f"AlarmClock({QUALITY_ALARM},RunPlugin(plugin://{POV}/?{query}),{QUALITY_DELAY},silent)", True)
    if home.getProperty(PREFIX + "QualityToken") == token:
        return True
    if home.getProperty(PREFIX + "ID") in ("", movie_id):   # not a newer page of another movie (it owns the name now)
        xbmc.executebuiltin(f"CancelAlarm({QUALITY_ALARM},true)", True)
    entry = flight(home)
    if entry and entry[0] == "pending" and entry[2] == token:
        home.clearProperty(FLIGHT)
    return False


def sync_saved():
    """My List / watchlist state of the page, in this interpreter (was a second script started beside this one)."""
    try:
        from netflix_saved_lists import main as saved_lists
        saved_lists("sync-movie")
    except Exception as error:  # the page must still open
        xbmc.log("Netflix Movie: saved-state sync failed: %s" % type(error).__name__, xbmc.LOGWARNING)


def show_movie(values, check_sources=True):
    """check_sources=False (back from a trailer or from playback): never start a source check from here. A check
    kept for this movie (10 minutes) fills the menu; without one the check waits for the first Right on Play.
    The same goes for a page opened while another launch is in flight (state idle)."""
    values = {**values, "Backdrop": detail_backdrop(values.get("Backdrop", ""))}
    home = xbmcgui.Window(10000)
    stop_loading()  # a page opening fresh never shows a loader (a request whose page went away ends with it)
    for key, value in values.items():
        home.setProperty(PREFIX + key, value)
    for field in ("Favorite", "Bookmarked", "FavoriteLabel", "BookmarkLabel"):
        home.clearProperty(PREFIX + field)
    for field in KEPT_RATING_FIELDS:   # R10.9: only a reopen carries ratings; a fresh page shows TMDb Helper's
        if field not in values:
            home.clearProperty(PREFIX + field)
    movie_id = valid_id(values.get("ID", ""))
    view, auto = None, False
    if movie_id:
        token = uuid4().hex
        home.setProperty(PREFIX + "QualityToken", token)
        view = cached_quality(movie_id)
        if view:
            show_quality(home, view)
            xbmc.log("Netflix Quality: page reused the kept check for movie %s" % movie_id, xbmc.LOGINFO)
        else:
            auto = check_sources and not flight_busy(home)
            # idle: nothing is scheduled for this page; open_movie_quality() starts the check on the first Right.
            home.setProperty(PREFIX + "QualityState", "checking" if auto else "idle")
            for tier in ("Highest", "Medium", "Lowest", "All"):
                home.setProperty(PREFIX + "Quality" + tier + "Specs", "Checking sources…")
                for field in ("Video", "Audio", "Line"):
                    home.clearProperty(PREFIX + "Quality" + tier + field)
            for field in FACT_FIELDS:
                home.clearProperty(PREFIX + "QualityHighest" + field)
    xbmc.executebuiltin("ActivateWindow(1167)")
    sync_saved()
    # The sync can take a second: the page may have closed or been replaced, or Play may be on its way.
    if auto and home.getProperty(PREFIX + "QualityToken") == token:
        if flight_busy(home) or play_pending(home):
            home.setProperty(PREFIX + "QualityState", "idle")
        else:
            schedule_quality(home, movie_id, token)


def show_more_info():
    if xbmcgui.getCurrentWindowDialogId() != DETAILS:
        return
    home = xbmcgui.Window(10000)
    origin = home.getProperty(PREFIX + "Origin")
    if origin == str(xbmcgui.getCurrentWindowId()):
        xbmc.executebuiltin(f"Action(Info,{origin})")


def show_description():
    if xbmcgui.getCurrentWindowDialogId() != DETAILS:
        return
    home = xbmcgui.Window(10000)
    plot = home.getProperty(PREFIX + "Plot")
    if plot:
        xbmcgui.Dialog().ok(home.getProperty(PREFIX + "Title"), plot)


def open_movie_quality():
    if xbmcgui.getCurrentWindowDialogId() != DETAILS:
        return
    home = xbmcgui.Window(10000)
    movie_id = valid_id(home.getProperty(PREFIX + "ID"))
    if not movie_id:
        return
    token = home.getProperty(PREFIX + "QualityToken")
    entry = flight(home)
    mine = entry and entry[2] == token and entry[0] in ("pending", "check")
    # idle: no check was scheduled for this page (back from a trailer or playback, or opened while another launch
    # was in flight). checking without an entry of this page: that check died. Either way it starts here.
    start = bool(token) and not mine and home.getProperty(PREFIX + "QualityState") in ("idle", "checking")
    if start:
        view = cached_quality(movie_id)
        if view:
            show_quality(home, view)
            start = False
        else:
            home.setProperty(PREFIX + "QualityState", "checking")
    home.setProperty(PREFIX + "QualityOpen", "true")
    monitor = xbmc.Monitor()
    for _ in range(12):
        if xbmcgui.getCurrentWindowDialogId() != DETAILS:
            return
        if xbmc.getCondVisibility("Control.IsVisible(30)"):
            xbmc.executebuiltin("SetFocus(30,0,absolute)")
            break
        if monitor.waitForAbort(0.025):
            return
    else:
        home.clearProperty(PREFIX + "QualityOpen")
    if not start:
        return

    def here():
        return xbmcgui.getCurrentWindowDialogId() == DETAILS and home.getProperty(PREFIX + "QualityToken") == token

    # Whatever is in flight goes first, "done" included: this script must not end while that interpreter leaves.
    end = time() + PLAY_WAIT
    while here() and time() < end and (flight(home) or play_pending(home)):
        if monitor.waitForAbort(0.1):
            return
    if here():
        if flight(home) or play_pending(home):
            home.setProperty(PREFIX + "QualityState", "idle")
        elif home.getProperty(PREFIX + "QualityState") == "checking":
            # Still nothing for this page. (Right pressed just as the page script set its alarm: that check has
            # run meanwhile and the state is "ready".)
            schedule_quality(home, movie_id, token)


def play_pending(home):
    try:
        return time() < float(home.getProperty(PLAY_PENDING))
    except ValueError:
        return False


def settle_quality(home, movie_id, token, dialog, reuse, request=None):
    """Before Play launches POV: wait (the page shows "Starting…" after 0.4 s: Custom_1167) until nothing
    is in flight, so that Play's interpreter never starts while a check's is starting, running or leaving. A
    check of this page that has clearly not started is cancelled instead; one that Play will not use (reuse is
    False) is told to stop. Returns the token to pass on, or None when the page went away meanwhile (Back during
    the wait cancels Play). R10-playback: Back on the loader withdraws the request (Netflix.Play.Loading) and
    cancels Play the same way."""
    monitor = xbmc.Monitor()
    entry = flight(home)
    if entry and entry[0] == "pending" and entry[2] == token and time() < entry[3] - 0.15:
        # wait=True: the alarm is gone when this returns. It only counts when that was clearly before its time.
        xbmc.executebuiltin(f"CancelAlarm({QUALITY_ALARM},true)", True)
        if time() < entry[3] - 0.05:
            home.clearProperty(FLIGHT)
    entry = flight(home)
    if not reuse and entry and entry[2] == token and entry[0] != "done":
        # All Sources searches for itself: this page's check stops now (its page token is gone) and
        # only its exit is waited for. The menu's check starts again on the next Right.
        token = uuid4().hex
        home.setProperty(PREFIX + "QualityToken", token)
        home.setProperty(PREFIX + "QualityState", "idle")
    def withdrawn():
        return request is not None and home.getProperty(LOADING) != request

    end = time() + PLAY_WAIT
    renewed = time()
    while flight(home) and time() < end:            # gone off / starting / running / leaving: let it end
        if (xbmcgui.getCurrentWindowDialogId() != dialog or home.getProperty(PREFIX + "QualityToken") != token
                or withdrawn() or monitor.waitForAbort(0.05)):
            return None
        if request and time() - renewed >= 2:
            renewed = time()
            renew_cover(request, seconds=COVER_SECONDS)
    if (xbmcgui.getCurrentWindowDialogId() != dialog or home.getProperty(PREFIX + "QualityToken") != token
            or withdrawn()):
        return None
    if token and home.getProperty(PREFIX + "QualityState") == "checking":
        # No check ran for this page (cancelled, or it never began): Play's own search is the only one. The
        # new token makes a check that still turns up return at once; the page script sets no alarm for it.
        xbmc.executebuiltin(f"CancelAlarm({QUALITY_ALARM},true)", True)
        token = uuid4().hex
        home.setProperty(PREFIX + "QualityToken", token)
        home.setProperty(PREFIX + "QualityState", "idle")
    return token


def park_focus():
    """Focus the loader's catcher once Kodi has drawn it (it is visible only while the loader is up)."""
    monitor = xbmc.Monitor()
    for _ in range(12):
        if xbmcgui.getCurrentWindowDialogId() != DETAILS:
            return
        if xbmc.getCondVisibility("Control.IsVisible(%d)" % LOADER_FOCUS):
            xbmc.executebuiltin("SetFocus(%d)" % LOADER_FOCUS)
            return
        if monitor.waitForAbort(0.025):
            return


def loader_facts(home, request, quality):
    """Plain Play opens the Highest tier's source first: when the page's check has finished, the loader shows its
    facts at once ("open"). The quality rows open their tier's source from the kept check too, without facts.
    All Sources, and Play without a finished check, keep "find" until POV reports."""
    if quality == "all" or home.getProperty(PREFIX + "QualityState") != "ready":
        return
    facts = [home.getProperty(PREFIX + "QualityHighest" + field) for field in FACT_FIELDS]
    if quality is None and facts[0]:
        set_loading(request, Phase="open", **dict(zip(FACT_FIELDS, facts)))
    elif quality is not None:
        set_loading(request, Phase="open")


def play_movie(*, from_info=False, quality=None, restart=False):
    expected_dialog = MOVIE_INFO if from_info else DETAILS
    if xbmcgui.getCurrentWindowDialogId() != expected_dialog:
        return
    home = xbmcgui.Window(10000)
    movie_id = valid_id(home.getProperty(PREFIX + "ID"))
    if not movie_id:
        return
    if from_info:
        if movie_id not in {info("ListItem.UniqueID(tmdb)"), info("ListItem.Property(tmdb_id)")}:
            return
    if play_pending(home) or home.getProperty(LOADING):
        return  # a second OK while the first Play is still on its way (its loader is up)
    params = {"mode": "play_media", "mediatype": "movie", "tmdb_id": movie_id}
    params["autoplay"] = "false" if quality == "all" else "true"
    if quality is not None:
        if home.getProperty(PREFIX + "QualityOpen") != "true":
            return
        home.clearProperty(PREFIX + "QualityOpen")
        xbmc.executebuiltin("SetFocus(10)")
        if quality != "all":
            params["movie_quality"] = quality
    home.setProperty(PLAY_PENDING, str(time() + PLAY_WAIT + 10))
    # R10-playback: the loader goes up at once, on this page (POV opens no loading window for this request), with
    # the transition cover under it for the moment Kodi closes the page into video. Both carry the same token,
    # which is also the request's: POV stops when the page withdraws it (Back on the loader).
    # 20 s fail-safe, renewed while Play waits here (settle_quality) and then by POV while it runs (sources.PlayFlight).
    request = hold_cover(home.getProperty(PREFIX + "Backdrop"), seconds=COVER_SECONDS)
    params["netflix_playback_cover"] = request
    # R10-playback: the page's choice is final, as on Netflix: "Resume" resumes and "Play" / "Play From Beginning"
    # start at 0, and POV never asks "Resume / Start Over" on top (sources.py passes netflix_resume to its player).
    resume = home.getProperty(PREFIX + "Action") == "Resume" and not restart
    params["netflix_resume"] = "resume" if resume else "restart"
    percent = home.getProperty(PREFIX + "Percent")
    start_loading(request, verb="Resuming" if resume else "Starting", phase="find",
                  watched="%s%% watched" % percent if resume and percent.isdecimal() else "")
    # All Sources always fetches its own list, as on R8.6 (a kept check holds only the available sources).
    reuse = quality != "all"
    loader_facts(home, request, quality)
    if not from_info:
        park_focus()
    token = settle_quality(home, movie_id, home.getProperty(PREFIX + "QualityToken"), expected_dialog, reuse, request)
    if token is None:
        stop_loading(request)
        release_cover(request)
        home.clearProperty(PLAY_PENDING)
        if xbmcgui.getCurrentWindowDialogId() == DETAILS and xbmc.getCondVisibility("Control.HasFocus(%d)" % LOADER_FOCUS):
            xbmc.executebuiltin("SetFocus(10)")
        return
    if token and reuse:
        params["netflix_quality_token"] = token
        loader_facts(home, request, quality)   # the check Play waited for may have finished meanwhile
    if quality is None:
        params["netflix_play_facts"] = "true"  # POV names each source it opens (fall-backs included)
    if from_info:
        xbmc.executebuiltin("Dialog.Close(movieinformation,true)", wait=True)
        park_focus()
    query = urlencode(params)
    # staged/exit-confirm: where Exit / Stop returns to (netflix_exit_return.py). POV closes this page and its
    # onunload clears Netflix.Movie.*, so keep a copy of what show_movie() needs.
    home.setProperty("Netflix.Exit.Return", json.dumps(
        {"kind": "movie", **{field: home.getProperty(PREFIX + field) for field in DETAIL_FIELDS},
         **page_ratings(home, movie_id)}))   # R10.9: the reopened page shows the same ratings
    # Keep the details page open behind the sources list; POV closes all dialogs itself once playback starts.
    home.setProperty(FLIGHT, "play|%s|%s|%.2f|%.2f" % (movie_id, token, time(), time() + PLAY_HOLD))
    xbmc.executebuiltin(f'PlayMedia("plugin://{POV}/?{query}")')
    home.clearProperty(PLAY_PENDING)
    # R10-playback: this script's interpreter ends a moment after POV's has started, never in the same instant
    # (the Mac log showed 5 to 13 ms between them; same-instant start/stop is the crash pattern).
    xbmc.Monitor().waitForAbort(LEAVE_AFTER_LAUNCH)


class TrailerPlayer(xbmc.Player):
    """Track Kodi's playback callbacks after it has processed player cleanup."""

    def __init__(self):
        super().__init__()
        self.completed = 0

    def onPlayBackStopped(self):
        self.completed += 1

    def onPlayBackEnded(self):
        self.completed += 1

    def onPlayBackError(self):
        self.completed += 1


def play_trailer():
    if xbmcgui.getCurrentWindowDialogId() != DETAILS or not xbmc.getCondVisibility(
        f"System.AddonIsEnabled({YOUTUBE})"
    ):
        return
    home = xbmcgui.Window(10000)
    values = {field: home.getProperty(PREFIX + field) for field in DETAIL_FIELDS}
    origin = xbmcgui.getCurrentWindowId()
    if values["Origin"] != str(origin):
        return
    movie_id = valid_id(values["ID"])
    video_id = youtube_id(values["Trailer"]) or (lookup_trailer(movie_id) if movie_id else "")
    if (xbmcgui.getCurrentWindowDialogId() != DETAILS
            or any(home.getProperty(PREFIX + field) != value for field, value in values.items())):
        return
    if not video_id:
        xbmcgui.Dialog().notification("Trailer", "No YouTube trailer found for this movie.")
        return
    values["Trailer"] = video_id
    values.update(page_ratings(home, movie_id))   # R10.9: the page reopened after the trailer shows the same ratings
    skin = xbmc.getSkinDir()
    monitor = xbmc.Monitor()
    player = TrailerPlayer()
    query = urlencode({"video_id": video_id, "ask_for_quality": "false", "audio_only": "false"})
    # R10-playback: the transition cover goes up under the page before it closes and stays (renewed by the wait
    # loop) until the page is back, so neither the trailer start nor its return shows the browse page. While
    # YouTube resolves, the cover carries the loader ("STARTING TRAILER").
    cover = hold_cover(values.get("Backdrop", ""))
    reopened = False
    try:
        # The movie page is modal, so release it for fullscreen video and reopen it on return.
        xbmc.executebuiltin("Dialog.Close(1167,true)", wait=True)
        start_loading(cover, verb="Trailer", phase="open")
        folder = info("Container.FolderPath")
        # staged/exit-confirm: Back during the trailer gets no exit prompt (Custom_1178 stops it instead) while this is set.
        home.setProperty("Netflix.Trailer.Active", video_id)
        try:
            reopened = wait_for_trailer(values, origin, folder, skin, monitor, player, query, cover)
        finally:
            home.clearProperty("Netflix.Trailer.Active")
    finally:
        stop_loading(cover)
        release_cover(cover, until="Window.IsVisible(1167)" if reopened else None, monitor=monitor)


def wait_for_trailer(values, origin, folder, skin, monitor, player, query, cover=None):
    """True when it reopened the movie page."""
    # The Python resolver must run while this script waits for the player.
    xbmc.executebuiltin(f'PlayMedia("plugin://{YOUTUBE}/play/?{query}",noresume)')
    start_deadline = monotonic() + 20
    completed_at_start = None
    trailer_file = ""
    left_fullscreen = None
    idle_since = None
    stop_requested = False
    renewed = monotonic()

    while not monitor.waitForAbort(0.1):
        if cover and monotonic() - renewed >= 2:
            renewed = monotonic()
            renew_cover(cover)
        window = xbmcgui.getCurrentWindowId()
        if window == FULLSCREEN_VIDEO:
            stop_loading(cover)  # the return shows the backdrop only, then the page
            if player.isPlayingVideo() and not trailer_file:
                trailer_file = info("Player.FilenameAndPath")
                if trailer_file:
                    completed_at_start = player.completed
            left_fullscreen = None
            idle_since = None
            continue
        if window != origin or xbmc.getSkinDir() != skin or info("Container.FolderPath") != folder:
            return

        dialog = xbmcgui.getCurrentWindowDialogId()
        if dialog == DETAILS:
            return

        now = monotonic()
        if completed_at_start is None:
            if now >= start_deadline and dialog == NO_DIALOG and not player.isPlaying():
                show_movie(values, check_sources=False)
                return True
            if now >= start_deadline + 5:
                return False
            continue

        if left_fullscreen is None:
            left_fullscreen = now
        if now - left_fullscreen >= 6:
            return
        if dialog != NO_DIALOG:
            idle_since = None
            continue
        if player.isPlaying():
            idle_since = None
            if info("Player.FilenameAndPath") != trailer_file:
                return
            if not stop_requested and player.completed == completed_at_start and now - left_fullscreen >= 0.3:
                # Back leaves fullscreen without necessarily stopping the video.
                xbmc.log("Netflix Trailer: requesting stop after fullscreen exit", xbmc.LOGINFO)
                xbmc.executebuiltin("PlayerControl(Stop)")
                stop_requested = True
            continue

        if player.completed == completed_at_start:
            continue
        if idle_since is None:
            idle_since = now
        if now - idle_since >= 0.25:
            xbmc.log("Netflix Trailer: playback cleanup complete, restoring movie page", xbmc.LOGINFO)
            show_movie(values, check_sources=False)
            return True


if __name__ == "__main__" and len(sys.argv) == 2:
    command = sys.argv[1]
    if command in {"play", "play-info"}:
        play_movie(from_info=command == "play-info")
    elif command == "open-quality":
        open_movie_quality()
    elif command in {"play-highest", "play-medium", "play-lowest", "play-all"}:
        play_movie(quality=command.removeprefix("play-"))
    elif command == "play-restart":
        play_movie(restart=True)
    elif command == "info":
        show_more_info()
    elif command == "description":
        show_description()
    elif command == "trailer":
        play_trailer()
    else:
        open_details(command)
