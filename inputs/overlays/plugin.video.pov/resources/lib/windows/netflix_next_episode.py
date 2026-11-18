"""Native presentation for the playback-local Next Episode controller."""
import xbmcgui


LEFT, RIGHT, CAPTION, FILL = 10, 20, 30, 31
PHASE = 'tikiskins.nextep_function'


def caption(episode):
    if episode is None:
        return ''
    label = '[B]S%d: E%d[/B]' % (episode.season, episode.number)
    if episode.duration:
        hours, minutes = divmod(max(1, round(episode.duration / 60)), 60)
        runtime = '%dh %dm' % (hours, minutes) if hours else '%dm' % minutes
        label += ' [COLOR BDFFFFFF]· %s[/COLOR]' % runtime
    return label


class NextEpisodeCard(xbmcgui.WindowXMLDialog):
    def __init__(self, *args):
        super().__init__(*args)
        self.dialog_id = None
        self.frame = 'preparing', None, None
        self.choose = None
        self.stop_playback = None

    def onInit(self):
        self.dialog_id = xbmcgui.getCurrentWindowDialogId()
        self.draw()

    def topmost(self):
        return self.dialog_id == xbmcgui.getCurrentWindowDialogId()

    def render(self, phase, episode, fill):
        frame = phase, episode, fill
        if frame != self.frame:
            focus_retry = phase == 'failed' and self.frame[0] != 'failed'
            self.frame = frame
            if self.dialog_id is not None:
                self.draw()
                if focus_retry:
                    self.setFocusId(RIGHT)

    def draw(self):
        phase, episode, fill = self.frame
        failed = phase == 'failed'
        self.setProperty(PHASE, phase)
        self.getControl(LEFT).setLabel('Episodes' if failed else 'Watch Credits')
        self.getControl(RIGHT).setLabel('Try Again' if failed else 'Next Episode')
        self.getControl(CAPTION).setLabel('[B]No Source Found[/B]' if failed else caption(episode))
        self.getControl(FILL).setPercent((fill or 0) * 100)

    def onClick(self, control_id):
        phase = self.frame[0]
        if self.choose is None or phase == 'handoff':
            return
        if control_id == LEFT:
            self.choose(self, 'episodes' if phase == 'failed' else 'credits')
        elif control_id == RIGHT and phase in ('ready', 'failed'):
            self.choose(self, 'retry' if phase == 'failed' else 'play')

    def onAction(self, action):
        key = action.getId()
        if key == xbmcgui.ACTION_STOP:
            if self.stop_playback is not None: self.stop_playback()
        elif self.choose is not None and key in (xbmcgui.ACTION_NAV_BACK, xbmcgui.ACTION_PREVIOUS_MENU, xbmcgui.ACTION_PARENT_DIR):
            self.choose(self, 'credits')
