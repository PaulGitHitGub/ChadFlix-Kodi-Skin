"""Leave fullscreen video for the title's page (staged/exit-confirm).

Runs once per exit, never per key press:
  exit  Back pressed a second time on the exit prompt (Custom_1178, Netflix.Exit.Go already set)
  stop  Stop without the prompt: OSD Stop button, keyboard x / stop, long-press Back (NetflixExitConfirm.xml)

Stops the player, waits for Kodi to leave fullscreen, then:
  movie    started from the movie page (play_movie() stashed Netflix.Exit.Return): reopen that page (dialog 1167)
           with "Resume" if the film was left part-way;
  episode  started from a show page: nothing; netflix_series.open_series() is still waiting and reopens the page
           (Netflix.Exit.Fast shortens its idle wait);
  trailer  (Netflix.Trailer.Active) or nothing stashed or a different title playing: only stop.
No list is refreshed here: the page opens exactly as a normal OK on a row opens it (show_movie).
R10-playback: for a movie that will be reopened, the transition cover (netflix_playback_cover.hold) goes up BEFORE the
player stops and comes down once the page is visible, so the browse page under it never shows. The settle wait below
stays: it keeps the page's own add-on work away from the Videos list load (crash rule)."""
import json
import math
import sys
import time
from pathlib import Path

import xbmc
import xbmcgui

_EXTRAS = str(Path(__file__).resolve().parent)
if _EXTRAS not in sys.path:
    sys.path.insert(0, _EXTRAS)
from netflix_playback_cover import hold as hold_cover, release as release_cover

HOME = 10000
FULLSCREEN_VIDEO = 12005
PROMPT = 11178
NO_DIALOG = 9999
RETURN = "Netflix.Exit.Return"
NO_NEXT = "Netflix.Exit.NoNext"
RUNNING_SECONDS = 25   # R10.55.2: longer than the longest run (4 s + WINDOW_WAIT + 3 s settle + page + release; was 10)
RUNNING = "Netflix.Exit.Running"
LOG = "Netflix Exit: "
RESUME_FROM, WATCHED_FROM = 5, 80   # POV player.py: set_resume, set_watched (percent)
# R10.55.2 (Stop -> row on big remuxes): the player stops at once, but Kodi leaves fullscreen only when VideoPlayer's
# callback queue reaches OnPlayBackStopped, and that waits behind the stop's own MyVideos write (CSaveFileState; SQLite
# retries while the database is locked). When the movie's cover is up and the player has stopped, the window gets this
# many seconds more to leave fullscreen before the page is given up.
WINDOW_WAIT = 10


def log(message):
    xbmc.log(LOG + message, xbmc.LOGINFO)


def wait(monitor, test, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if test():
            return True
        if monitor.waitForAbort(step):
            return False
    return test()


def playing_title(player):
    try:
        tag = player.getVideoInfoTag()
        return tag.getUniqueID("tmdb") or "", player.getTime(), player.getTotalTime()
    except RuntimeError:
        return "", 0.0, 0.0


def stash(raw):
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}



def main(mode):
    home = xbmcgui.Window(HOME)
    stamp = home.getProperty(RUNNING)
    try:
        if stamp and time.time() - float(stamp) < RUNNING_SECONDS:
            # A repeated Stop still stops media right away; only the original
            # script can reopen its captured title or release its cover.
            home.setProperty(NO_NEXT, str(time.time()))
            if mode == "exit":
                # R10.55.2 (review): this press's Exit.Go (set by Custom_1178) belongs to no run that clears it; a stale
                # one hides the seek bar on the next playback.
                home.clearProperty("Netflix.Exit.Go")
            xbmc.executebuiltin("PlayerControl(Stop)")
            return
    except ValueError:
        pass
    running = str(time.time())
    home.setProperty(RUNNING, running)
    monitor = xbmc.Monitor()
    player = xbmc.Player()
    cover = None
    raw = home.getProperty(RETURN)
    go = home.getProperty("Netflix.Exit.Go")
    target = stash(raw)
    stopped = False
    try:
        trailer = bool(home.getProperty("Netflix.Trailer.Active"))
        playing = player.isPlayingVideo()
        tmdb, position, total = playing_title(player)
        if playing and not trailer and target.get("kind") == "movie" and (not tmdb or tmdb == target.get("ID")):
            # Up before the stop: Videos is drawn under it from its first frame. Trailers keep their own (the
            # trailer script holds one through the trailer); episodes too (netflix_series.play_and_wait).
            cover = hold_cover(target.get("Backdrop", ""), seconds=30)   # stop 4 s + WINDOW_WAIT + settle 3 s + page + release
        # Stamp the original exit intent for POV, then stop without waiting for its worker or Flight.
        play = target.get("play") if target.get("kind") == "episode" and (not tmdb or target.get("ID") == tmdb) else None
        home.setProperty(NO_NEXT, str(time.time()) + ("|" + play if isinstance(play, str) and play else ""))
        home.setProperty("Netflix.Exit.Fast", str(time.time()))
        xbmc.executebuiltin("PlayerControl(Stop)")
        if not playing:
            return  # an async successor may still be opening; retain the NoNext intent
        stopped = wait(monitor, lambda: not player.isPlaying()
                       and xbmcgui.getCurrentWindowId() != FULLSCREEN_VIDEO, 4)
        if not stopped and cover and not player.isPlaying():
            # R10.55.2: stopped, still on the fullscreen window (see WINDOW_WAIT). The prompt is not left over the black
            # screen meanwhile (it closes here at the same moment as before).
            if xbmcgui.getCurrentWindowDialogId() == PROMPT:
                xbmc.executebuiltin("Dialog.Close(1178,true)")
            began = time.monotonic()
            stopped = wait(monitor, lambda: not player.isPlaying()
                           and xbmcgui.getCurrentWindowId() != FULLSCREEN_VIDEO, WINDOW_WAIT)
            # Its own line, worded so that neither the line below (ev_flows.py exit_line) nor a stop mark
            # (ev_kodi.py STOP_MARK) matches it.
            log("fullscreen still up 4 s after the %s; waited %.1f s more for the window (%s)" % (
                mode, time.monotonic() - began, "left" if stopped else "gave up"))
        if xbmcgui.getCurrentWindowDialogId() == PROMPT:
            xbmc.executebuiltin("Dialog.Close(1178,true)")
        log("%s: stopped=%s at %.0f/%.0f s, window %d, trailer=%s, stash=%s" % (
            mode, stopped, position, total, xbmcgui.getCurrentWindowId(), trailer, target.get("kind", "-")))
        if not stopped or trailer or target.get("kind") != "movie":
            return
        if tmdb and tmdb != target.get("ID"):
            log("a different title was playing (%s), not reopening %s" % (tmdb, target.get("ID")))
            return
        origin = target.get("Origin", "")
        # Let the window underneath settle (no dialog, list not loading) so the page's own add-on calls
        # never overlap a list load.
        settled = wait(monitor, lambda: xbmcgui.getCurrentWindowDialogId() == NO_DIALOG
                       and not xbmc.getCondVisibility("Container.IsUpdating"), 3)
        window = str(xbmcgui.getCurrentWindowId())
        if window != origin:
            log("landed on window %s, page came from %s: not reopening" % (window, origin))
            return
        if not settled:
            log("window %s did not settle in 3 s: not reopening" % window)
            return
        values = {key: value for key, value in target.items() if key != "kind"}
        if total > 0:
            # R10 (review M1): "Resume" only where POV keeps a bookmark to resume from: it saves one from 5 % and marks
            # the title watched (no bookmark) from 80 % (POV player.py set_resume / set_watched). The page's choice is
            # final now, so the label must say what POV will do.
            percent = 100 * position / total
            values["Action"] = "Resume" if RESUME_FROM <= percent < WATCHED_FROM else "Play"
            values["Percent"] = str(math.ceil(percent)) if values["Action"] == "Resume" else ""
        from netflix_movie_details import show_movie
        # R9: no source check is started from here; a check kept for this movie fills the quality menu.
        show_movie(values, check_sources=False)
        log("movie page reopened (%s)" % values.get("Action"))
        release_cover(cover, until="Window.IsVisible(1167)", monitor=monitor)
        cover = None
    finally:
        release_cover(cover)  # no reopen: down at once
        if home.getProperty(RUNNING) == running:
            if home.getProperty(RETURN) == raw:
                # Episodes is consumed by SeriesPlayer after the existing Videos settle wait.
                if not (stopped and target.get("kind") == "episode" and target.get("episodes")):
                    home.clearProperty(RETURN)
                if home.getProperty("Netflix.Exit.Go") == go:
                    home.clearProperty("Netflix.Exit.Go")
            home.clearProperty(RUNNING)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "stop")
