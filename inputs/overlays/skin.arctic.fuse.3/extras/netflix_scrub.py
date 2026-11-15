"""Independent scrubber: one persistent input owner; OK performs exactly one seek.

scrubber-finish (2026-11-13) keeps Codex's ScrubModel and transport rules and changes only the presentation:
the original play bar (DialogSeekBar) and OSD (VideoOSD) stay on screen and draw the scrub state themselves from
Home properties.

Ownership (review H1/H2):
  * 8200's onfocus sets Netflix.Scrub.UI ("entry requested"), holds the header, arms a 1 s failsafe alarm and starts
    this script. The skin shows scrub mode (buttons out, scrub helper, scrub nib) only while Netflix.Scrub.Owner is
    also set, so it is never shown without a live input owner.
  * One session at a time, by a lock Netflix.Scrub.Active = "state:token:time[:target]", state scrub|closing|seek|hold.
    A new entry waits for a closing/seeking/holding session to let go (Netflix.Scrub.Takeover), then owns the bar.
  * Owner is set only once this session has the lock and focus is still on the timeline, and cleared the moment it
    stops owning input. If focus leaves the timeline (Back/Down) before the input dialog opens, it never opens.
Then the script publishes the target as Netflix.Scrub.Time/Remaining (labels) and Netflix.Scrub.Nib/Fill (ranges
strings; Fill last, it swaps the native fill and nib for the scrub ones in the same frame) and opens Netflix_Scrub.xml,
a dialog with nothing visible, only to own Left/Right/OK/Back.
The +/-10 buttons keep their native immediate actions. No player request runs on directional input; there is no
background worker thread.
"""
import os
import sys
import time
import threading
import uuid

import xbmc
import xbmcgui

EXTRAS = os.path.dirname(os.path.abspath(__file__))
if EXTRAS not in sys.path:
    sys.path.insert(0, EXTRAS)
from netflix_scrub_model import ScrubModel, clock, is_long, fill_range, nib_range, native_percent  # noqa: E402

HOME = 10000
OSD = 12901
FULLSCREEN_VIDEO = 12005
FOCUS = 9001
P = "Netflix.Scrub."
ACTIVE, UI, HEADER, EARLY, OWNER = P + "Active", P + "UI", P + "Header", P + "Early", P + "Owner"
FILL, NIB, TIME, REMAINING = P + "Fill", P + "Nib", P + "Time", P + "Remaining"
RETURNING, TAKEOVER = P + "Returning", P + "Takeover"
FAILSAFE = "netflixscrubsafe"   # AlarmClock armed by 8200's onfocus; this script cancels it as soon as it runs
HOLD_MAX = 10.0         # after OK, keep the target on the bar until playback reports it (slow streams), at most this
HOLD_NEAR = 1.5         # "reached": playback time within this many seconds of the target
TAKEOVER_WAIT = 3.0     # how long a new entry waits for a finishing session to let go


def trace(what):
    # Debug-level timing marks (visible only with Kodi debug logging); no media details.
    xbmc.log("Netflix Scrub: %s t=%.3f" % (what, time.monotonic()), xbmc.LOGDEBUG)


def test_delay(home, name):
    """Mac-test hook only: Netflix.Scrub.TestSlowStart/TestSlowClose (seconds, capped at 1) widen the start-up and
    clean-up windows so the H1/H2 races can be reproduced on a fast Mac. Never set on the Shield."""
    value = home.getProperty(P + name)
    if value:
        try:
            xbmc.sleep(int(min(1.0, max(0.0, float(value))) * 1000))
        except ValueError:
            pass


class Lock:
    """Netflix.Scrub.Active = state:token:time[:target]."""

    def __init__(self, home):
        self.home = home
        self.token = uuid.uuid4().hex[:8]

    def set(self, state, target=None):
        # time.monotonic(): one system clock for every interpreter in the Kodi process, immune to NTP steps.
        value = "%s:%s:%f" % (state, self.token, time.monotonic())
        if target is not None:
            value += ":%.3f" % target
        self.home.setProperty(ACTIVE, value)

    def mine(self):
        return self.home.getProperty(ACTIVE).split(":")[1:2] == [self.token]

    def release(self):
        if self.mine():
            self.home.clearProperty(ACTIVE)


def parse_lock(value):
    parts = value.split(":")
    state = parts[0] if parts else ""
    try:
        when = float(parts[2])
    except (IndexError, ValueError):
        when = 0.0
    try:
        target = float(parts[3])
    except (IndexError, ValueError):
        target = None
    return state, when, target


class Presenter:
    """Writes the scrub state into the skin's own play bar. Pure property traffic, kept testable without Kodi."""

    def __init__(self, props, model):
        self.props = props
        self.model = model
        self.long = is_long(model.duration)
        self.last = {}

    def _set(self, key, value):
        if self.last.get(key) != value:
            self.props.setProperty(key, value)
            self.last[key] = value

    def labels(self):
        self._set(TIME, clock(self.model.target, self.long))
        self._set(REMAINING, clock(self.model.duration - self.model.target, self.long))

    def bar(self):
        percent = self.model.percent()
        # Nib before fill: the fill string is what switches the skin over (see enter()).
        self._set(NIB, nib_range(percent))
        self._set(FILL, fill_range(percent))

    def enter(self, start_native=True):
        # Start the drawn marker where the native bar is drawn (Player.Progress is a whole percent), then let the
        # model's ease carry it to the exact position: the switch itself changes no pixels. When continuing from a
        # committed target that the bar already shows exactly, start there.
        if start_native:
            self.model.display = min(self.model.limit, native_percent(self.model.target, self.model.duration)
                                     * self.model.duration / 100.0)
        self.labels()
        self.bar()

    def frame(self, elapsed):
        self.model.animate(elapsed)
        self.labels()
        self.bar()

    def release(self):
        """Hand the bar back: Fill first (native fill and nib return in that same frame), then the rest."""
        for key in (FILL, NIB, TIME, REMAINING):
            self.props.clearProperty(key)
            self.last.pop(key, None)


class InputDialog(xbmcgui.WindowXMLDialog):
    """Owns input only. Netflix_Scrub.xml has no visible controls."""

    def __init__(self, *args, **kwargs):
        self.model = kwargs.pop("model")
        self.presenter = kwargs.pop("presenter")
        super().__init__(*args, **kwargs)
        self.ready = False
        self.closed = False
        self.stop_requested = False
        self.reenter = False
        self.lock = threading.RLock()
        self.last_action = time.monotonic()
        self.dialog_id = None

    def onInit(self):
        with self.lock:
            self.dialog_id = xbmcgui.getCurrentWindowDialogId()
            self.setFocusId(FOCUS)
            self.ready = True

    def finish(self, commit=False):
        with self.lock:
            if self.closed:
                return
            self.model.finish(commit)
            self.closed = True
            self.ready = False
            self.close()

    def onAction(self, action):
        with self.lock:
            if self.closed:
                # An Up that reaches the dialog while it is closing (Back then Up at once) is a request to scrub
                # again, not a key to drop: re-enter once clean-up has handed focus back.
                if action.getId() == 3:
                    self.reenter = True
                return
            self.last_action = time.monotonic()
            aid = action.getId()
            trace("action %d" % aid)
            if aid in (1, 2):
                self.model.move(1 if aid == 2 else -1, self.last_action)
                # The label follows at once; the marker eases to the target on the next ticks.
                self.presenter.labels()
            elif aid in (7, 79):
                self.finish(True)
            elif aid in (3, 4, 9, 10, 92, 216):
                self.finish(False)
            elif aid == 13:
                self.stop_requested = True
                self.finish(False)

    def onClick(self, control_id):
        if control_id == FOCUS:
            self.finish(True)


def osd_focus_is_timeline():
    return xbmc.getCondVisibility("Window.IsActive(videoosd) + Control.HasFocus(8200)")


def focus_play(home):
    """Return focus to Play (6001). The timeline's onunfocus clears Netflix.Scrub.UI. Returning guards the timeline's
    onfocus only while focus is actually moving."""
    home.setProperty(RETURNING, "true")
    try:
        if xbmc.getCondVisibility("Window.IsActive(videoosd)"):
            xbmcgui.Window(OSD).setFocusId(6001)
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline and osd_focus_is_timeline():
                xbmc.sleep(10)
    except RuntimeError:
        pass
    finally:
        home.clearProperty(RETURNING)
        home.clearProperty(UI)


def wait_unpaused(seconds=0.4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and xbmc.getCondVisibility("Player.Paused"):
        xbmc.sleep(10)


def claim(home, lock):
    """Returns (True, previous_commit_target) when this session owns the bar, (None, None) when a live session already
    owns input (leave everything alone), (False, None) when a finishing session did not let go in time."""
    stamp = home.getProperty(ACTIVE)
    target = None
    if stamp:
        state, when, target = parse_lock(stamp)
        fresh = 0 <= time.monotonic() - when < 120
        if fresh and state == "scrub":
            return None, None
        if fresh:
            disarm_failsafe()          # alive and will either own the bar or hand focus back itself
            home.setProperty(TAKEOVER, lock.token)
            deadline = time.monotonic() + TAKEOVER_WAIT
            while time.monotonic() < deadline and home.getProperty(ACTIVE):
                if home.getProperty(TAKEOVER) != lock.token:
                    return None, None      # a later Up is waiting too: the latest one takes the entry
                xbmc.sleep(10)
            if home.getProperty(TAKEOVER) != lock.token:
                return None, None
            if home.getProperty(TAKEOVER) == lock.token:
                home.clearProperty(TAKEOVER)
            if home.getProperty(ACTIVE):
                return False, None
        if state not in ("seek", "hold"):
            target = None
    lock.set("scrub")
    disarm_failsafe()
    return True, target


def disarm_failsafe():
    """8200's onfocus arms a 1 s AlarmClock that puts focus back on Play when no script takes the entry. Cancelled only
    by a session that owns (or is about to own) the entry; a duplicate launch that defers to a live owner leaves it."""
    xbmc.executebuiltin("CancelAlarm(%s,true)" % FAILSAFE)


def taken_over(home, lock):
    t = home.getProperty(TAKEOVER)
    return bool(t) and t != lock.token


def run():
    home = xbmcgui.Window(HOME)
    player = xbmc.Player()
    lock = Lock(home)
    claimed, previous_target = claim(home, lock)
    if claimed is None:
        return
    if not claimed:
        # The previous session did not let go in time. Leave the timeline so nothing waits for an owner.
        if osd_focus_is_timeline():
            focus_play(home)
        home.clearProperty(EARLY)
        return
    dialog = None
    presenter = None
    paused_here = False
    identity = None
    commit = None
    before = None
    monitor = xbmc.Monitor()

    def still_wanted():
        # Back/Down/the OSD closing all clear UI (8200 onunfocus, VideoOSD onunload).
        return bool(home.getProperty(UI)) and osd_focus_is_timeline() and not taken_over(home, lock)

    try:
        test_delay(home, "TestSlowStart")
        if (not player.isPlayingVideo() or not xbmc.getCondVisibility("Player.SeekEnabled")
                or xbmc.getCondVisibility("Pvr.IsPlayingTv") or not still_wanted()):
            return
        if not home.getProperty(HEADER):
            # The previous session may have let go of the held header before this one started (fast re-entry).
            home.setProperty(HEADER, "paused" if xbmc.getCondVisibility("Player.Paused") else "playing")
        home.setProperty(OWNER, lock.token)          # scrub mode becomes visible from here
        if not xbmc.getCondVisibility("Player.Paused"):
            player.pause()
            paused_here = True
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline and not xbmc.getCondVisibility("Player.Paused"):
                xbmc.sleep(5)
        duration = player.getTotalTime()
        position = player.getTime()
        continuing = False
        if previous_target is not None and (xbmc.getCondVisibility("Player.Seeking")
                                            or abs(position - previous_target) > HOLD_NEAR):
            # Re-entry while the last commit's seek has not landed yet: start where the bar shows it.
            position = previous_target
            continuing = True
        model = ScrubModel(position, duration)
        # Kept in memory only. Never log the stream URL or write it to a property.
        identity = player.getPlayingFile()
        xbmc.executebuiltin("CancelAlarm(osd_timeout,true)")
        presenter = Presenter(home, model)
        early = home.getProperty(EARLY)
        home.clearProperty(EARLY)
        presenter.enter(start_native=not continuing)
        if early in ("1", "-1"):
            model.move(int(early), time.monotonic())
            presenter.labels()
        # Last check before taking input: a Back/Down during start-up must cancel, never leave an invisible owner.
        if not still_wanted():
            trace("cancelled before input")
            return
        disarm_failsafe()      # a later Up's alarm must not take focus from this live session
        dialog = InputDialog("Netflix_Scrub.xml", "special://skin", "Default", "1080i", model=model, presenter=presenter)
        dialog.show()
        started = previous = last_check = time.monotonic()
        xbmc.log("Netflix Scrub: opened", xbmc.LOGINFO)
        while not monitor.abortRequested() and not dialog.closed:
            now = time.monotonic()
            # A skin reload, external Close(all), or a higher-priority modal ends ownership immediately.
            if dialog.ready and xbmcgui.getCurrentWindowDialogId() != dialog.dialog_id:
                dialog.finish(False)
                break
            if not home.getProperty(UI) or taken_over(home, lock):
                dialog.finish(False)
                break
            if now - last_check >= 0.20:
                last_check = now
                if (not player.isPlayingVideo() or player.getPlayingFile() != identity
                        or xbmcgui.getCurrentWindowId() != FULLSCREEN_VIDEO
                        or not xbmc.getCondVisibility("Window.IsActive(videoosd)")
                        or now - dialog.last_action > 90
                        or (not dialog.ready and now - started > 3)):
                    dialog.finish(False)
                    break
            with dialog.lock:
                if not dialog.closed:
                    presenter.frame(min(0.1, now - previous))
            previous = now
            moving = abs(model.target - model.display) >= 0.02
            # waitForAbort (not a plain Python wait) is what lets Kodi deliver onAction/onClick to this script.
            if monitor.waitForAbort(0.016 if moving else 0.03):
                break
        trace("loop end")
        same = (not monitor.abortRequested() and player.isPlayingVideo() and player.getPlayingFile() == identity)
        if dialog.stop_requested:
            presenter.release()
            player.stop()
            same = False
        # The model is immutable after finish, and a double Select cannot seek twice.
        if model.result is not None and same:
            commit = model.result
            # The bar shows the target exactly and keeps it (fill + time label) until playback reports it.
            model.display = model.target
            presenter.bar()
    except (RuntimeError, ValueError):
        xbmc.log("Netflix Scrub: unavailable or playback changed", xbmc.LOGWARNING)
    finally:
        # No longer the input owner: from this frame the skin shows normal mode again.
        if lock.mine():
            lock.set("closing")
        if home.getProperty(OWNER) == lock.token:
            home.clearProperty(OWNER)
        if dialog is not None:
            dialog.finish(False)
        home.clearProperty(EARLY)
        # Controls come back first (buttons fade in, helper swaps back); the seek may block for a while.
        if not taken_over(home, lock) and (osd_focus_is_timeline() or home.getProperty(UI)):
            focus_play(home)
        trace("focus play")
        if commit is None and presenter is not None:
            # Cancel: the nib went with the focus; now the native fill returns at the playing position.
            presenter.release()
        if dialog is not None and dialog.reenter and not taken_over(home, lock):
            try:
                if xbmc.getCondVisibility("Window.IsActive(videoosd)"):
                    # 8200's onfocus starts the next session; it takes over from this one (Takeover) at once.
                    xbmcgui.Window(OSD).setFocusId(8200)
                    trace("re-entry")
            except RuntimeError:
                pass
        try:
            if commit is not None:
                lock.set("seek", commit)
                trace("seek")
                try:
                    before = player.getTime()
                except RuntimeError:
                    before = None
                player.seekTime(commit)
                trace("seek returned")
                xbmc.log("Netflix Scrub: committed once", xbmc.LOGINFO)
            elif presenter is not None:
                xbmc.log("Netflix Scrub: cancelled", xbmc.LOGINFO)
        except RuntimeError:
            commit = None
        resumed = False
        try:
            # Restore transport state only for the same item we paused ourselves.
            if (paused_here and not monitor.abortRequested() and player.isPlayingVideo()
                    and player.getPlayingFile() == identity and xbmc.getCondVisibility("Player.Paused")):
                player.pause()
                resumed = True
        except RuntimeError:
            pass
        if resumed:
            wait_unpaused()
        trace("resumed" if resumed else "no resume")
        test_delay(home, "TestSlowClose")
        if commit is not None and presenter is not None:
            lock.set("hold", commit)
            reason = hold(home, lock, player, commit, before, identity, monitor)
            if not taken_over(home, lock):
                presenter.release()
            if reason == "landed":
                settle_header(home, lock, player, monitor)
        if presenter is None and lock.mine():
            # Never drew anything itself (cancelled during start-up): drop a bar state inherited from a takeover.
            for key in (FILL, NIB, TIME, REMAINING):
                home.clearProperty(key)
        elif presenter is not None and not taken_over(home, lock):
            # Taken over: the newcomer starts from this target and overwrites the bar itself, so it is left in place
            # (releasing it would show the pre-seek spot for a moment on a slow stream).
            presenter.release()
        # The held header (and Play/Pause icon) now match the real play state; let them go - unless a new session is
        # taking over or an Up for one is already on the timeline; they keep the same held state.
        if not taken_over(home, lock) and not (home.getProperty(UI) and osd_focus_is_timeline()):
            home.clearProperty(HEADER)
        lock.release()
        trace("done")
        if dialog is not None:
            del dialog


def hold(home, lock, player, target, before, identity, monitor):
    """After OK the controls are back, but Player.Time/Progress can report the old spot until the seek lands (seconds
    on a stream). Keep the target on the bar (fill + time label; no nib, Play has focus) until playback reports it,
    at most HOLD_MAX. Returns why it ended:
      "landed"  playback is at the target;
      "moved"   playback is somewhere else (a +/-10 press, which also clears Fill, or a seek that landed off target),
                or has kept playing for 3 s without reaching it: the bar must show the real position now;
      "ended"   takeover, Stop, media change, OSD closed, timeout."""
    deadline = time.monotonic() + HOLD_MAX
    running_since = None
    try:
        while time.monotonic() < deadline and not monitor.abortRequested():
            if taken_over(home, lock):
                return "ended"
            if not home.getProperty(FILL):
                return "moved"
            if (not player.isPlayingVideo() or player.getPlayingFile() != identity
                    or not xbmc.getCondVisibility("Window.IsActive(videoosd)")):
                return "ended"
            if not xbmc.getCondVisibility("Player.Seeking") and not xbmc.getCondVisibility("Player.Caching"):
                now_t = player.getTime()
                if abs(now_t - target) <= HOLD_NEAR:
                    return "landed"
                if before is not None and abs(now_t - before) > HOLD_NEAR:
                    return "moved"
                if running_since is None:
                    running_since = now_t
                elif now_t - running_since > 3.0:
                    return "moved"
            if monitor.waitForAbort(0.02):
                break
    except RuntimeError:
        pass
    return "ended"


def settle_header(home, lock, player, monitor):
    """Kodi labels every seek "Seeking +0:30" (then chapter info) for 3 s. Ours was asked for on purpose, so the header
    keeps the held play state until that window has passed, the real play state differs (someone pressed Play/Pause),
    a new session takes over, playback stops or the OSD closes (then onunload clears it anyway)."""
    held = home.getProperty(HEADER)
    deadline = time.monotonic() + 3.5
    try:
        while held and time.monotonic() < deadline and not monitor.abortRequested():
            if taken_over(home, lock) or not xbmc.getCondVisibility("Player.HasPerformedSeek(3)"):
                break
            if not player.isPlayingVideo() or not xbmc.getCondVisibility("Window.IsActive(videoosd)"):
                break
            if xbmc.getCondVisibility("Player.Paused") != (held == "paused"):
                break
            if monitor.waitForAbort(0.05):
                break
    except RuntimeError:
        pass
    trace("header settled")


if __name__ == "__main__":
    run()
