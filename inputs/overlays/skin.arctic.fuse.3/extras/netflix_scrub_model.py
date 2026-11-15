"""Local-only scrub destination and play-bar geometry. This module never contacts Kodi or a source.

ScrubModel is Codex's proven model, unchanged in behaviour. scrubber-finish adds the time format shared with the skin's
play-bar labels and the strings that drive the skin's own fill and nib (ranges controls in Includes_OSD.xml/VideoOSD.xml).
"""
import math

# Play-bar nib textures (progress/slider_bg.png is 1360x112, progress/slider_nib.png is 112x112). The native nib is a
# progress control's right texture: its left edge sits at PERCENT * (1360 - 112) / 1360 of the control and it is
# 112/1360 of the control wide. The scrub nib reproduces exactly that box.
NIB_BG_W = 1360.0
NIB_W = 112.0
NIB_SPAN = (NIB_BG_W - NIB_W) / NIB_BG_W
NIB_WIDTH_PCT = NIB_W / NIB_BG_W * 100.0


def kodi_seconds(seconds):
    """Whole seconds exactly as Kodi's Player.Time/Player.Duration labels show them (std::lrint: nearest, ties to even)."""
    if not math.isfinite(seconds):
        return 0
    return max(0, int(round(seconds)))


def is_long(duration, position=0.0):
    """The skin's rule: H:MM:SS when the duration is an hour or more, unknown (0), or the position is past the hour
    (Integer.IsGreaterOrEqual(Player.Duration(secs),3600) | ...Time(secs)... | String.IsEmpty(Player.Duration(secs))).
    M:SS would wrap at the hour (Kodi's "m" is minutes % 60), so it is used only for known sub-hour videos."""
    total = kodi_seconds(duration)
    return total >= 3600 or total == 0 or kodi_seconds(position) >= 3600


def clock(seconds, long_format):
    """H:MM:SS (0:05:12, 1:24:08) for hour-plus videos, M:SS (5:12, 22:30) for shorter ones."""
    value = kodi_seconds(seconds)
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    if long_format:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (value // 60, secs)


def native_percent(position, duration):
    """The whole percent Kodi's Player.Progress shows (std::lrintf of time/total*100)."""
    return float(int(round(100.0 * position / duration)))


def fill_range(percent):
    percent = min(100.0, max(0.0, percent))
    return "0,%.4f" % percent


def nib_range(percent):
    percent = min(100.0, max(0.0, percent))
    left = percent * NIB_SPAN
    return "%.4f,%.4f" % (left, left + NIB_WIDTH_PCT)


class ScrubModel:
    def __init__(self, position, duration):
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("A finite positive duration is required")
        self.duration = float(duration)
        # Never commit at EOF: seeking there can advance a playlist immediately.
        self.limit = max(0.0, self.duration - 1.0)
        self.target = min(self.limit, max(0.0, float(position)))
        self.display = self.target
        self.direction = 0
        self.last_input = None
        self.streak_started = 0.0
        self.finished = False
        self.result = None

    def move(self, direction, now):
        if self.finished:
            return False
        direction = 1 if direction > 0 else -1
        if direction != self.direction or self.last_input is None or now - self.last_input > 0.40:
            self.streak_started = now
        self.direction = direction
        self.last_input = now
        held = max(0.0, now - self.streak_started)
        step = 120 if held >= 4 else 60 if held >= 2 else 30 if held >= 0.8 else 10
        self.target = min(self.limit, max(0.0, self.target + direction * step))
        return True

    def animate(self, elapsed):
        # Exponential ease-out stays interruptible and never blocks another input.
        self.display += (self.target - self.display) * (1 - math.exp(-max(0.0, elapsed) / 0.045))
        if abs(self.target - self.display) < 0.02:
            self.display = self.target
        return self.display

    def finish(self, commit):
        if self.finished:
            return False
        self.finished = True
        self.result = self.target if commit else None
        return True

    def percent(self, seconds=None):
        return 100.0 * (self.display if seconds is None else seconds) / self.duration
