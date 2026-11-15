"""Netflix-style subtitle appearance picker (skin window 1177).

  RunScript(special://skin/extras/netflix_subtitle_style.py,open)          read Kodi's settings, then open the picker
  RunScript(special://skin/extras/netflix_subtitle_style.py,size=medium)   apply one size   (OK on a size pill)
  RunScript(special://skin/extras/netflix_subtitle_style.py,style=box)     apply one style  (OK on a style pill)
  RunScript(special://skin/extras/netflix_subtitle_style.py,restore)       from VideoFullScreen onload, only while a
                                                                           restore is pending (see below)

While the picker is open the real subtitle is hidden (it would run under the pills). The `open` run stays alive as a
small watcher (one 0.1 s wait loop, no work per key press) and shows the subtitle again when the picker closes.
If playback stops or ends while the picker is open, Kodi has already saved "subtitles off" for that file; the watcher
then leaves Skin.String(Netflix.Sub.RestoreFile) and the next playback start of that same file turns them back on.

It only runs on OK. The settings are Kodi's own (Settings > Player > Subtitles), written with JSON-RPC
Settings.SetSettingValue, so they persist and apply to the subtitle on screen at once.
Kodi hides the background colour/opacity settings unless the background type is Box or Square box, and JSON-RPC
answers "Invalid params" for a hidden setting, so the background type is always written first.
"""
import hashlib
import json
import sys

import xbmc

SIZES = {'small': 24, 'medium': 30, 'large': 40}

# Order matters: backgroundtype first (it makes the bg*/shadow* settings visible to JSON-RPC).
STYLES = {
    # white text, black outline, soft black drop shadow
    'shadow': [('subtitles.backgroundtype', 1), ('subtitles.colorpick', 'FFFFFFFF'), ('subtitles.opacity', 100),
               ('subtitles.bordersize', 25), ('subtitles.bordercolorpick', 'FF000000'),
               ('subtitles.shadowcolor', 'FF000000'), ('subtitles.shadowopacity', 100), ('subtitles.shadowsize', 15)],
    # white text on a black box behind each line (80 %)
    'box': [('subtitles.backgroundtype', 2), ('subtitles.colorpick', 'FFFFFFFF'), ('subtitles.opacity', 100),
            ('subtitles.bgcolorpick', 'FF000000'), ('subtitles.bgopacity', 80), ('subtitles.shadowopacity', 0)],
    # yellow text on a solid black box (Netflix "Contrast")
    'contrast': [('subtitles.backgroundtype', 2), ('subtitles.colorpick', 'FFFFFF00'), ('subtitles.opacity', 100),
                 ('subtitles.bgcolorpick', 'FF000000'), ('subtitles.bgopacity', 100), ('subtitles.shadowopacity', 0)],
    # black text on a solid white box
    'reverse': [('subtitles.backgroundtype', 2), ('subtitles.colorpick', 'FF000000'), ('subtitles.opacity', 100),
                ('subtitles.bgcolorpick', 'FFFFFFFF'), ('subtitles.bgopacity', 100), ('subtitles.shadowopacity', 0)],
}

WINDOW = 1177
RESTORE = 'Netflix.Sub.RestoreFile'


def path_hash(path):
    """R10.25 (security audit F-07): what Skin.String(Netflix.Sub.RestoreFile) holds. A stream path can carry credentials,
    and skin strings are saved in plain text in the skin's settings.xml, so only this one-way hash is stored; it is also
    safe inside a builtin (no quote or comma)."""
    return hashlib.sha256(path.encode('utf-8', 'surrogatepass')).hexdigest()


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('Netflix Subtitles: %s' % msg, level)


def rpc(method, params):
    try:
        reply = json.loads(xbmc.executeJSONRPC(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
    except ValueError as exc:
        raise RuntimeError('%s %s: bad reply (%s)' % (method, params.get('setting'), exc))
    if not isinstance(reply, dict):
        raise RuntimeError('%s %s: bad reply' % (method, params.get('setting')))
    if 'error' in reply:
        raise RuntimeError('%s %s: %s' % (method, params.get('setting'), reply['error'].get('message')))
    return reply.get('result')


def get(setting):
    """Value, or None when Kodi hides the setting (for example bgcolorpick while there is no box)."""
    try:
        return rpc('Settings.GetSettingValue', {'setting': setting})['value']
    except (RuntimeError, KeyError, TypeError):
        return None


def norm(colour):
    return (colour or '').upper()


def classify(values):
    """(size, size_near, style, style_near) from the current Kodi settings."""
    fontsize = values.get('fontsize')
    size = next((k for k, v in SIZES.items() if v == fontsize), 'custom')
    if size != 'custom':
        size_near = size
    elif isinstance(fontsize, int):
        size_near = min(SIZES, key=lambda k: abs(SIZES[k] - fontsize))
    else:
        size_near = 'medium'

    bgtype, text = values.get('backgroundtype'), norm(values.get('colorpick'))
    bg, bgop = norm(values.get('bgcolorpick')), values.get('bgopacity')
    style = 'custom'
    for name, writes in STYLES.items():
        want = dict(writes)
        if want['subtitles.backgroundtype'] != bgtype or norm(want['subtitles.colorpick']) != text:
            continue
        if bgtype == 2 and (norm(want['subtitles.bgcolorpick']) != bg or want['subtitles.bgopacity'] != bgop):
            continue
        style = name
        break
    if style != 'custom':
        style_near = style
    elif bgtype in (2, 3):
        style_near = 'contrast' if text.endswith('FFFF00') else 'reverse' if bg.endswith('FFFFFF') else 'box'
    else:
        style_near = 'shadow'
    return size, size_near, style, style_near


def remember(size=None, size_near=None, style=None, style_near=None):
    for key, value in (('Size', size), ('SizeNear', size_near), ('Style', style), ('StyleNear', style_near)):
        if value:
            xbmc.executebuiltin('Skin.SetString(Netflix.Sub.%s,%s)' % (key, value))


def read_current():
    values = {k: get('subtitles.' + k) for k in ('fontsize', 'backgroundtype', 'colorpick', 'bgcolorpick', 'bgopacity')}
    return classify(values)


def apply(writes):
    """All or nothing: on a failed write, put back every value already changed (background type first, so the
    hidden child settings are reachable again), then raise."""
    before = [(setting, get(setting)) for setting, _ in writes]
    done = []
    try:
        for setting, value in writes:
            rpc('Settings.SetSettingValue', {'setting': setting, 'value': value})
            done.append(setting)
    except RuntimeError:
        for setting, value in before:
            if setting in done and value is not None:
                try:
                    rpc('Settings.SetSettingValue', {'setting': setting, 'value': value})
                except RuntimeError as exc:
                    log('rollback of %s failed: %s' % (setting, exc), xbmc.LOGERROR)
        raise


def cond(text):
    return xbmc.getCondVisibility(text)


def watch(player, hid, path):
    """Runs inside the `open` call while the picker is up. Only waits; returns when the picker has closed."""
    monitor = xbmc.Monitor()
    for _ in range(30):  # the window opens a frame or two after ActivateWindow
        if cond('Window.IsVisible(%d)' % WINDOW) or monitor.waitForAbort(0.1):
            break
    while cond('Window.IsVisible(%d)' % WINDOW) and cond('Player.HasVideo'):
        if monitor.waitForAbort(0.1):
            return
    if cond('Player.HasVideo'):
        if hid:
            player.showSubtitles(True)
        return
    xbmc.executebuiltin('Dialog.Close(%d,true)' % WINDOW)
    if hid and path:
        # stopped or ended while open: Kodi saved "off" for this file; fix it on its next start
        xbmc.executebuiltin('Skin.SetString(%s,%s)' % (RESTORE, path_hash(path)))
        log('playback ended while the picker was open; restore pending')


def restore():
    """Next playback start after a stop-while-open: same file -> subtitles on again. One shot either way."""
    want = xbmc.getInfoLabel('Skin.String(%s)' % RESTORE)
    xbmc.executebuiltin('Skin.Reset(%s)' % RESTORE)
    if not want:
        return
    monitor, player = xbmc.Monitor(), xbmc.Player()
    for _ in range(100):  # up to 10 s for the streams to open
        if cond('Player.HasVideo') and player.getAvailableSubtitleStreams():
            break
        if monitor.waitForAbort(0.1):
            return
    try:
        playing = player.getPlayingFile()
    except RuntimeError:
        return
    # R10.25: the hash; a value an older version saved (the path itself) is honoured this once (it was reset above)
    if want in (path_hash(playing), playing) and not cond('VideoPlayer.SubtitlesEnabled'):
        player.showSubtitles(True)
        log('subtitles restored after a stop while the picker was open')


def main(arg):
    if arg == 'open':
        remember(*read_current())
        player = xbmc.Player()
        hid = cond('Player.HasVideo') and cond('VideoPlayer.SubtitlesEnabled')
        path = ''
        if hid:
            try:
                path = player.getPlayingFile()
            except RuntimeError:
                path = ''
            player.showSubtitles(False)
        xbmc.executebuiltin('Dialog.Close(all,true)')
        xbmc.executebuiltin('ActivateWindow(%d)' % WINDOW)
        watch(player, hid, path)
        return
    if arg == 'restore':
        restore()
        return
    kind, _, name = arg.partition('=')
    try:
        if kind == 'size' and name in SIZES:
            apply([('subtitles.fontsize', SIZES[name])])
            remember(size=name, size_near=name)
        elif kind == 'style' and name in STYLES:
            apply(STYLES[name])
            remember(style=name, style_near=name)
        else:
            log('unknown argument %r' % arg, xbmc.LOGWARNING)
            return
        log('applied %s' % arg)
    except RuntimeError as exc:
        log('could not apply %s: %s' % (arg, exc), xbmc.LOGERROR)
        # what Kodi really has now, so the check mark never lies
        remember(*read_current())
        xbmc.executebuiltin('Notification(Subtitles,Could not change the subtitle look,3000)')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'open')
