"""Next Episode in the original POV playback request."""
from threading import Event, Thread
from time import monotonic, time

from modules import kodi_utils
from modules.netflix_next_model import FADE, NextEpisode, episode_times


SCRUB = 'Netflix.Scrub.'
BLOCKED = 'Window.IsVisible(Netflix_Scrub.xml) | Player.Seeking | Window.IsActive(videoosd)'
CARD = 'netflix_next_episode.xml'


def scrub_active():
    return (any(kodi_utils.get_property(SCRUB + field) for field in ('UI', 'Active', 'Owner', 'Takeover'))
            or kodi_utils.get_visibility(BLOCKED))


def playing_episode(player):
    tag = player.getVideoInfoTag()
    return str(tag.getUniqueID('tmdb')), tag.getSeason(), tag.getEpisode()


class NextEpisodeController:
    def __init__(self, player, flight):
        self.player, self.flight = player, flight
        self.model = NextEpisode()
        self.file = player.getPlayingFile()
        self.identity = (str(player.tmdb_id), int(player.season), int(player.episode))
        self.began = time()
        self.worker = self.cancelled = None
        self.result = (None, None)
        self.window = None
        self.held = False

    def exiting(self):
        from modules.sources import Sources
        return Sources.exit_requested(self.began, self.flight.token)

    def owns_playback(self):
        try:
            return (self.flight.owns_playback() and self.player.isPlayingVideo() and self.file
                    and self.player.getPlayingFile() == self.file and not self.exiting()
                    and playing_episode(self.player) == self.identity
                    and not self.player.monitor.abortRequested())
        except RuntimeError:
            return False

    def can_offer(self):
        visible = kodi_utils.get_visibility
        return (not scrub_active() and visible('Window.IsActive(fullscreenvideo)')
                and (not visible('System.HasActiveModalDialog')
                     or (self.window and self.window.topmost())))

    def can_commit(self):
        if not self.owns_playback() or not self.can_offer():
            return False
        try:
            times = episode_times(self.player.getTotalTime(), getattr(self.player, 'credits', None))
            return bool(times and self.player.getTime() >= times[1])
        except RuntimeError:
            return False

    def restore(self, prepared):
        self.hide()
        self.model.restore(prepared)

    def request_stop(self):
        try:
            if self.cancelled is not None: self.cancelled.set()
            in_handoff = self.window is not None and self.window.frame[0] == 'handoff'
            if self.owns_playback() or (in_handoff and self.flight.owns_playback()):
                kodi_utils.set_property('Netflix.Exit.NoNext', '%s|%s' % (time(), self.flight.token))
                kodi_utils.set_property('Netflix.Exit.Fast', str(time()))
        finally:
            self.player.stop()

    def poll_interval(self):
        if self.window is None:
            return 1
        if self.model.phase == 'handoff':
            return FADE
        return 0.05 if self.model.phase == 'ready' else 1

    def start(self):
        if self.worker is not None or not self.model.start():
            return
        from modules.sources import Sources

        cancelled = self.cancelled = Event()
        self.result = (None, None)

        def prepare():
            result = Sources.prepare_next(self.player.meta, cancelled, self.publish)
            if not cancelled.is_set(): self.result = result

        self.worker = Thread(target=prepare, name='pov_next_episode')
        self.worker.start()

    def publish(self, episode):
        # Presentation-only data. The owner claims results only after join.
        if not self.cancelled.is_set(): self.model.episode = episode

    def collect(self):
        if self.worker is not None and not self.worker.is_alive():
            self.worker.join()
            self.worker = None
            self.model.complete(*self.result)
            self.result = (None, None)

    def hide(self):
        if self.window is not None:
            window, self.window = self.window, None
            window.close()

    def release_pause(self):
        if self.held and self.owns_playback() and not scrub_active():
            self.held = False
            if kodi_utils.get_visibility('Player.Paused'):
                self.player.pause()

    def dismiss(self):
        self.model.dismiss()
        if self.cancelled is not None:
            self.cancelled.set()
        self.hide()
        self.release_pause()

    def close(self, handoff=False):
        self.model.dismiss()
        if self.cancelled is not None:
            self.cancelled.set()
        # Kodi retires interpreter children after this entry point returns.
        # Cancel now and let the scoped worker finish its bounded I/O there.
        self.result = (None, None)
        if not handoff:
            self.hide()
            self.release_pause()

    def show(self, phase, fill):
        if self.window is None:
            from windows.netflix_next_episode import NextEpisodeCard

            self.window = NextEpisodeCard(CARD, 'special://skin', 'Default', '1080i')
            self.window.stop_playback = self.request_stop
            self.window.choose = self.select
            self.window.render(phase, self.model.episode, fill)
            self.window.show()
        else:
            self.window.render(phase, self.model.episode, fill)

    def select(self, window, choice):
        if self.window is not window or self.model.phase == 'dismissed' or not self.can_commit():
            return
        if choice == 'credits':
            self.dismiss()
        elif choice == 'retry' and self.model.phase == 'failed':
            self.start()
            self.show(self.model.phase, None)
        elif choice == 'episodes' and self.model.phase == 'failed':
            from modules.sources import Sources

            episode = self.model.episode
            if Sources.update_series_return(self.flight, 'episodes', episode.identity):
                self.request_stop()
            self.model.dismiss()
            self.hide()
        elif choice == 'play' and self.model.phase == 'ready':
            self.model.fade(monotonic())
            self.show('handoff', None)

    def tick(self, position, duration, credits):
        if not self.owns_playback():
            self.dismiss()
            return
        self.collect()
        if self.held and not kodi_utils.get_visibility('Player.Paused'):
            self.held = False
        if self.model.phase == 'dismissed':
            self.release_pause()
            return
        times = episode_times(duration, credits)
        if times is None:
            return
        prefetch, offer = times
        eligible = self.can_offer()
        if eligible and self.model.phase == 'unstarted' and position >= prefetch:
            self.start()
        if not eligible or position < offer or self.model.phase == 'unstarted':
            self.model.defer()
            self.hide()
            self.release_pause()
            return
        if duration - position <= 1.5 and not self.held and not kodi_utils.get_visibility('Player.Paused'):
            self.player.pause()
            self.held = True
        now = monotonic()
        if self.model.phase == 'handoff':
            if self.model.after_fade(now) and self.owns_playback() and self.can_offer():
                return self.model.take()
            return
        if self.model.phase == 'ready' and self.window is None:
            self.show('ready', 0.0)
            return
        fill = self.model.fill(now, True)
        if self.model.phase == 'ready' and fill == 1.0:
            self.model.fade(now)
            self.show('handoff', None)
        else:
            self.show(self.model.phase, fill)
