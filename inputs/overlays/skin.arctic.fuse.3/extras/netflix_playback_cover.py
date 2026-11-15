"""R10-playback: the one transition cover and the play loader (skin 1080i/Includes_Netflix_Playback.xml).

Cover: a black-plus-backdrop layer drawn inside the Videos window (and by dialog 1169 over other windows). It is held
while a title page hands over to video and back, so the browse page never shows in between:
    token = hold(image)                 before the step that would reveal the window under the page
    renew(token)                        about every 2 s while a loop keeps it (it lapses SECONDS after the last renew)
    release(token, until=...)           after the page is back (waits for it, then a short grace), or at once
Only the holder's token releases it; a newer hold replaces an older one. The named alarm is the fail-safe: if the
holder dies, the cover is gone SECONDS later. No add-on is launched by any of this (alarms and properties only).

Loader: Window(Home).Property(Netflix.Play.Loading) = the play request's token while a title page waits for POV.
POV (modules/sources.py) withdraws nothing itself except at its end; the page's Back clears the property, which POV
reads as "cancelled"."""
from uuid import uuid4

import xbmc
import xbmcgui

HOME = 10000
COVER = "Netflix.PlaybackCover"
IMAGE = COVER + ".Image"
ALARM = "netflix_playback_cover"
SECONDS = 8                 # fail-safe: a cover nobody renews is gone this long after the last hold/renew
GRACE = 0.5                 # after the page is visible: its own backdrop loads (async) while the cover is still under it
LOADING = "Netflix.Play.Loading"
PLAY = "Netflix.Play."
PLAY_FIELDS = ("Verb", "Phase", "Res", "SpecA", "SpecB", "Watched")


def _home():
    return xbmcgui.Window(HOME)


def _alarm(seconds):
    minutes, rest = divmod(max(1, int(seconds)), 60)
    # A second AlarmClock with the same name replaces the first one (silently: the old one was silent).
    xbmc.executebuiltin("AlarmClock(%s,noop,%02d:%02d,silent)" % (ALARM, minutes, rest), True)


def hold(image="", seconds=SECONDS, token=None):
    """Raise the cover (or take it over) and return its token. The alarm is set first, so the layer is visible from
    the moment the token is."""
    token = token or uuid4().hex
    _alarm(seconds)
    home = _home()
    home.setProperty(IMAGE, image or "")
    home.setProperty(COVER, token)
    return token


def renew(token, seconds=SECONDS):
    """Keep a held cover alive; False when it is no longer this token's."""
    if not token or _home().getProperty(COVER) != token:
        return False
    _alarm(seconds)
    return True


def held(token):
    return bool(token) and _home().getProperty(COVER) == token


def release(token, until=None, timeout=2.0, grace=GRACE, monitor=None):
    """Take the cover down if it is still this token's. With until: first wait (at most timeout) for that condition
    (the page being visible), then grace seconds, so the page is complete before the layer under it goes."""
    if not token:
        return
    monitor = monitor or xbmc.Monitor()
    if until:
        steps = int(timeout / 0.01)
        for _ in range(steps):
            if xbmc.getCondVisibility(until) or monitor.waitForAbort(0.01):
                break
        if grace and held(token):
            monitor.waitForAbort(grace)
    home = _home()
    if home.getProperty(COVER) == token:
        home.clearProperty(COVER)
        home.clearProperty(IMAGE)
        # The alarm is left to lapse: cancelling it here could drop the alarm of a hold() made in between (review L2).
        # The layer needs both, so it is down the moment the property is gone.


def start_loading(token, verb="Starting", phase="find", facts=("", "", ""), watched=""):
    """Show the loader for this request (the page hides its buttons and parks focus while it is up)."""
    home = _home()
    values = dict(zip(PLAY_FIELDS, (verb, phase, *facts, watched)))
    for key, value in values.items():
        home.setProperty(PLAY + key, value or "")
    home.setProperty(LOADING, token)


def set_loading(token, **fields):
    """Update the loader of this request only (a newer request, or a cancel, owns the fields then)."""
    home = _home()
    if home.getProperty(LOADING) != token:
        return False
    for key, value in fields.items():
        home.setProperty(PLAY + key, value or "")
    return True


def loading(token):
    return bool(token) and _home().getProperty(LOADING) == token


def stop_loading(token=None):
    """End the loader: this request's only, or any (token None: a page opening fresh)."""
    home = _home()
    if token is not None and home.getProperty(LOADING) != token:
        return
    home.clearProperty(LOADING)
    for key in PLAY_FIELDS:
        home.clearProperty(PLAY + key)
