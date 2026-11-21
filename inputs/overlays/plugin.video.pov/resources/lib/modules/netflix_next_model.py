"""Playback-local Next Episode decisions."""
from dataclasses import dataclass, field
from math import isfinite


COUNTDOWN = 10.0
FADE = 0.12  # R10.53: the skin's handoff fade (netflix_next_episode.xml, 120 ms); keep them equal


def episode_times(duration, credits):
    """Return the prefetch and offer positions on the current video."""
    if not isfinite(duration) or duration <= 0:
        return None
    if isinstance(credits, (int, float)) and 0 < credits < duration:
        return max(0.0, credits - 90.0), credits
    return max(0.0, duration - 90.0), max(0.0, duration - min(30.0, duration * 0.02))


@dataclass(frozen=True)
class Episode:
    show: str
    season: int
    number: int
    duration: int

    @property
    def identity(self):
        return self.show, self.season, self.number


@dataclass(frozen=True)
class PreparedEpisode:
    episode: Episode
    link: str = field(repr=False)
    filename: str = field(repr=False)
    meta: dict = field(repr=False)


class NextEpisode:
    """One phase and one countdown, owned by the current player's poll."""

    def __init__(self):
        self.phase = 'unstarted'
        self.countdown = None
        self.episode = None
        self.prepared = None

    def start(self):
        if self.phase not in ('unstarted', 'failed'):
            return False
        self.phase = 'preparing'
        self.countdown = None
        return True

    def complete(self, episode, prepared):
        if self.phase != 'preparing':
            return
        self.episode = episode
        if episode is None:
            self.phase = 'dismissed'
        elif prepared is None:
            self.fail(episode)
        else:
            self.prepared = prepared
            self.phase = 'ready'

    def fail(self, episode):
        self.phase = 'failed'
        self.episode = episode
        self.prepared = self.countdown = None

    def dismiss(self):
        self.phase = 'dismissed'
        self.prepared = None
        self.countdown = None

    def fill(self, now, eligible):
        if self.phase != 'ready' or not eligible:
            self.countdown = None
            return None
        if self.countdown is None:
            self.countdown = now
        return min(1.0, max(0.0, (now - self.countdown) / COUNTDOWN))

    def fade(self, now):
        if self.phase == 'ready':
            self.phase = 'handoff'
            self.countdown = now

    def defer(self):
        if self.phase == 'handoff':
            self.phase = 'ready'
        self.countdown = None

    def after_fade(self, now):
        return self.phase == 'handoff' and now >= self.countdown + FADE

    def take(self):
        if self.phase != 'handoff':
            return None
        prepared = self.prepared
        self.dismiss()
        return prepared

    def restore(self, prepared):
        self.prepared = prepared
        self.phase = 'ready'
        self.countdown = None
