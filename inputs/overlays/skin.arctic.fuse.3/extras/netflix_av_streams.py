"""Netflix-style Audio & Subtitles card in the playback OSD (window 1147: Audio and Subtitles columns; R10.4).

  RunScript(special://skin/extras/netflix_av_streams.py,populate)   run when a card pops up (simplified lists)
  RunScript(special://skin/extras/netflix_av_streams.py,audio=N)    OK on an audio row (N = row slot)
  RunScript(special://skin/extras/netflix_av_streams.py,sub=N)      OK on a subtitle row (slot 0 is the "Off" row)

It reads Player.GetProperties once and writes plain names into Window(Home) properties that the cards' static
rows show: Netflix.AV.A<i>.L (label) / .C ("1" when current), Netflix.AV.S<i>.L / .C (S0 is "Off"),
Netflix.AV.A.Cur / Netflix.AV.S.Cur (slot of the current choice, used to focus it). Nothing runs per key press.
"""
import json
import re
import sys
import time

try:
    import xbmc
    import xbmcgui
except ImportError:  # offline tests import this file without Kodi
    xbmc = xbmcgui = None

MAX = 40  # rows per card (must match the skin's static rows)
PREFIX = 'Netflix.AV.'

# Kodi's own ISO table gives names such as "Spanish; Castilian" or "Greek, Modern (1453-)": keep the common name.
OVERRIDES = {'nob': 'Norwegian', 'nno': 'Norwegian', 'nor': 'Norwegian', 'fil': 'Filipino', 'zho': 'Chinese',
             'chi': 'Chinese', 'cmn': 'Chinese', 'yue': 'Chinese (Cantonese)', 'ell': 'Greek', 'gre': 'Greek',
             'pob': 'Portuguese (Brazil)', 'heb': 'Hebrew', 'jpn': 'Japanese', 'kor': 'Korean'}
UNKNOWN = ('', 'und', 'unk', 'zxx', 'mis', 'mul', 'non')

# the short upper-case tokens are matched case-sensitively: "ad lib" is not an audio description
AD_RE = re.compile(r'(?i:audio[ ._-]*descri|descriptive|described|visually[ ._-]*impaired)|\bAD\b|\bDVS\b')
CC_RE = re.compile(r'\bSDH\b|\bCC\b|(?i:closed[ ._-]*caption|hearing[ ._-]*impaired|deaf)')
FORCED_RE = re.compile(r'forced', re.I)
COMM_RE = re.compile(r'commentary', re.I)
EXTERNAL_RE = re.compile(r'\s*\((external)\)\s*', re.I)


def clean_language(name):
    name = (name or '').split(';')[0].split(',')[0]
    return re.sub(r'\s*\(.*?\)\s*$', '', name).strip()


def language_name(code, convert=None):
    """'eng' -> 'English'; '' if unknown."""
    code = (code or '').strip().lower()
    if code in UNKNOWN:
        return ''
    if code in OVERRIDES:
        return OVERRIDES[code]
    if convert is None and xbmc is not None:
        convert = lambda c: xbmc.convertLanguage(c, xbmc.ENGLISH_NAME)  # noqa: E731
    try:
        name = clean_language(convert(code)) if convert else ''
    except Exception:
        name = ''
    # Kodi hands back the input code when it does not know it
    return '' if (not name or name.lower() == code) else name


TECH_RE = re.compile(r'\b(aac(-lc)?|he-aac|ac-?3|e-?ac-?3|dd\+?|dts(-hd)?( ma)?|truehd|atmos|flac|opus|vorbis|mp[23]|pcm|lpcm|'
                     r'mono|stereo|surround|dolby|digital|plus|\d\.\d|\d+ ?ch(annels?)?|\d+ ?k?b(it)?(/s|ps)|'
                     r'\d{3,4}[pi]?|subrip|srt|ass|ssa|pgs|vobsub|sup|utf-?8|hdmv|text|graphic|audio|subtitles?|track|stream)\b', re.I)


def track_title(raw):
    """The stream's own name, minus codec / channel / resolution detail; '' when nothing readable is left."""
    if EXTERNAL_RE.search(raw or ''):
        return ''  # an external file's name is a file name ("movie.2020.1080p.srt"): "Subtitle N" reads better
    name = re.sub(r'[\s\-|,()\[\]/._]+', ' ', TECH_RE.sub(' ', raw or '')).strip()
    return name


def make_label(stream, kind, position, convert=None):
    """stream: one entry of Player.GetProperties audiostreams / subtitles. position: 1-based among its kind."""
    raw = stream.get('name') or ''
    lang = language_name(stream.get('language'), convert)
    base = lang
    if not base:
        title = track_title(raw)
        base = title if title and not (AD_RE.search(title) or CC_RE.search(title) or FORCED_RE.search(title)) else ''
    if not base:
        base = ('Audio %d' if kind == 'audio' else 'Subtitle %d') % position
    label = base
    forced = bool(stream.get('isforced')) or bool(FORCED_RE.search(raw))
    if kind == 'audio':
        if stream.get('isoriginal'):
            label += ' (Original)'  # R10.4 review M1: the owner's brackets, as in the short list
        if COMM_RE.search(raw):
            if not COMM_RE.search(label):
                label += ' - Commentary'
        elif stream.get('isimpaired') or AD_RE.search(raw):
            label += ' - Audio Description'
    else:
        if stream.get('isimpaired') or CC_RE.search(raw):
            label += ' (CC)'
        if forced:
            label += ' (Forced)'
    return label



ALL = 'all'  # kodi_index of the "All tracks" row
ALL_LABEL = 'All tracks'
MORE_LABEL = 'More'  # the same row on the subtitle card
ENGLISH = 'English'
# Kodi's JSON-RPC gives no subtitle codec, so "image" (PGS / VobSub / DVB) can only be recognised from a stream title
IMAGE_RE = re.compile(r'pgs|vobsub|\bdvb\b|hdmv|bitmap|\bimage\b|\bsup\b', re.I)
CHANNELS = {1: 'mono', 2: 'stereo', 6: '5.1', 8: '7.1'}


def is_external(stream):
    return bool(EXTERNAL_RE.search(stream.get('name') or ''))


def is_image(stream):
    return bool(IMAGE_RE.search(stream.get('name') or ''))


def pick(members, kind):
    """The most reliable stream behind a merged row.
    Audio: Kodi's default / original flag first, then more channels, then the first one.
    Subtitles: embedded before external or downloaded, text before image, the file's default first, then the first."""
    if kind == 'audio':
        return min(members, key=lambda m: (not (m.get('isdefault') or m.get('isoriginal')), -(m.get('channels') or 0), m.get('index', 0)))
    return min(members, key=lambda m: (is_external(m), is_image(m), not m.get('isdefault'), m.get('index', 0)))


def labelled(streams, kind, convert):
    return [(make_label(st, kind, i + 1, convert), st) for i, st in enumerate(streams)]


def simple_rows(streams, kind, current, convert=None):
    """One row per distinct label; the check sits on the row whenever ANY member is the current stream.
    A last "All tracks" row appears only when something was merged."""
    groups = {}
    for label, st in labelled(streams, kind, convert):
        groups.setdefault(label, []).append(st)
    rows = [(label, any(m.get('index') == current for m in members), pick(members, kind).get('index'))
            for label, members in groups.items()]
    return rows, len(rows) < len(streams)


def is_cc(stream):
    """A closed-caption / SDH / hearing-impaired subtitle."""
    return bool(stream.get('isimpaired')) or bool(CC_RE.search(stream.get('name') or ''))


def is_forced(stream):
    return bool(stream.get('isforced')) or bool(FORCED_RE.search(stream.get('name') or ''))


def best_english(streams, convert=None):
    """The one stream shown as "English": a full (not forced) non-CC track first, then a full CC one (English that exists
    only as CC); inside a tier the usual pick() rules. Forced-only English is never shown as "English" (it is reachable
    through More, or gets its own row when current)."""
    eng = [st for st in streams
           if language_name(st.get('language'), convert) == ENGLISH and not is_forced(st)]
    if not eng:
        return None
    tier = min(is_cc(st) for st in eng)
    return pick([st for st in eng if is_cc(st) == tier], 'sub')


def extra_label(streams, kind, current, base, convert=None):
    """Label of the extra "current" row: the full list's label, but never identical (any case) to a base row, Off or More.
    base: [(label, stream)] of the rows already shown. A clash (e.g. current = English 2.0 while "English" is the 5.1 track)
    is told apart by what really differs from the stream behind that row (channels / codec, external file / image), then by
    the track's number. None when current is not among the streams."""
    names = {r[2]: r[0] for r in full_rows(streams, kind, current, convert)}
    label = names.get(current)
    cur = next((st for st in streams if st.get('index') == current), None)
    if label is None or cur is None:
        return None
    taken = {b[0].lower() for b in base} | {'off', MORE_LABEL.lower()}
    if label.lower() not in taken:
        return label
    candidates = []
    for _, bst in base:
        if bst is not None:
            words = variant_words(cur, [cur, bst], kind)
            if words:
                candidates.append(label + ' \u00b7 ' + ', '.join(words))
    if kind == 'audio' and cur.get('codec'):
        candidates.append('%s \u00b7 %s' % (label, cur['codec'].upper()))
    for cand in candidates:
        if cand.lower() not in taken:
            return cand
    pos = next(i for i, st in enumerate(streams) if st.get('index') == current) + 1
    cand = '%s \u00b7 %d' % (label, pos)
    while cand.lower() in taken:
        pos += 1
        cand = '%s \u00b7 %d' % (label, pos)
    return cand


def simple_sub_rows(streams, current, enabled, convert=None):
    """The simplified subtitle card: "English" (best English stream), one row for the current stream when it is neither
    Off nor that English stream (labelled as the full list labels it), and "More" (opens the full list) whenever any
    other subtitle exists. Rows are (label, is_current, kodi_index)."""
    best = best_english(streams, convert)
    best_idx = best.get('index') if best else None
    rows = []
    if best:
        rows.append((ENGLISH, bool(enabled) and current == best_idx, best_idx))
    if enabled and current is not None and current != best_idx:
        label = extra_label(streams, 'sub', current, [(ENGLISH, best)] if best else [], convert)
        if label:
            rows.append((label, True, current))
    if len(streams) > (1 if best else 0):
        rows.append((MORE_LABEL, False, ALL))
    return rows


def is_extra_audio(stream):
    """Audio description or commentary: never the "English" / "Original" pick while a regular track exists."""
    raw = stream.get('name') or ''
    return bool(COMM_RE.search(raw) or stream.get('isimpaired') or AD_RE.search(raw))


def best_audio(members):
    """pick() among the regular tracks; only when there are none, among the AD / commentary ones."""
    regular = [m for m in members if not is_extra_audio(m)]
    return pick(regular or members, 'audio') if members else None


def original_audio(streams):
    """The original-language track: flagged isoriginal, else Kodi's default, else unknown (None)."""
    for flag in ('isoriginal', 'isdefault'):
        flagged = [st for st in streams if st.get(flag)]
        if flagged:
            return best_audio(flagged)
    return None


def simple_audio_rows(streams, current, convert=None):
    """The simplified audio card. Original audio English or unknown: "English", "More". Original in another language:
    "<Language> (Original)", "English", "More". One extra row for the current stream when it is neither of those (labelled as
    the full list labels it). "More" (opens the full list) whenever any track exists beyond the first two. No Off row."""
    eng = best_audio([st for st in streams if language_name(st.get('language'), convert) == ENGLISH])
    orig = original_audio(streams)
    orig_lang = language_name(orig.get('language'), convert) if orig else ''
    base, base_st = [], []
    if orig and orig_lang and orig_lang != ENGLISH:
        base.append(('%s (Original)' % orig_lang, orig.get('index')))
        base_st.append(orig)
    if eng:
        base.append((ENGLISH, eng.get('index')))
        base_st.append(eng)
    rows = [(label, current == idx, idx) for label, idx in base]
    shown = {idx for _, idx in base}
    if current is not None and current not in shown:
        label = extra_label(streams, 'audio', current, [(b[0], st) for b, st in zip(base, base_st)], convert)
        if label:
            rows.append((label, True, current))
    if len(streams) > len(base):
        rows.append((MORE_LABEL, False, ALL))
    return rows


def variant_words(st, group, kind):
    """What tells this stream from the others that read the same: only attributes that really differ in the group."""
    words = []
    if kind == 'sub':
        if len({is_external(m) for m in group}) > 1 and is_external(st):
            words.append('external file')
        if len({is_image(m) for m in group}) > 1 and is_image(st):
            words.append('image')
    else:
        if len({m.get('channels') for m in group}) > 1 and st.get('channels'):
            words.append(CHANNELS.get(st['channels'], '%d ch' % st['channels']))
        elif len({(m.get('codec') or '').lower() for m in group}) > 1 and st.get('codec'):
            words.append(st['codec'].upper())
    return words


def full_rows(streams, kind, current, convert=None):
    """Every stream, in Kodi's order, same-reading ones told apart by the smallest difference ("English · 5.1",
    "English · external file", "English · image"), then by number ("English · 2")."""
    pairs = labelled(streams, kind, convert)
    groups = {}
    for label, st in pairs:
        groups.setdefault(label, []).append(st)
    out, seen = [], {}
    for label, st in pairs:
        group = groups[label]
        words = variant_words(st, group, kind) if len(group) > 1 else []
        text = label + (' \u00b7 ' + ', '.join(words) if words else '')
        seen[text] = seen.get(text, 0) + 1
        if seen[text] > 1:
            text = '%s \u00b7 %d' % (text, seen[text])
        out.append((text, st.get('index') == current, st.get('index')))
    return out


def cap_rows(rows):
    """At most MAX rows; when the current one would fall off the end it takes the last row, so it keeps its check."""
    if len(rows) <= MAX:
        return rows
    shown = rows[:MAX]
    if not any(r[1] for r in shown):
        cur = next((r for r in rows if r[1]), None)
        if cur:
            shown[-1] = cur
    return shown


def build(props, convert=None, full=None):
    """props: Player.GetProperties result -> (audio, subs, audio_cur_slot, sub_cur_slot).
    Rows are (label, is_current, kodi_index). Simplified lists merge identical labels and end with an "All tracks" row (subtitles: Off / English / current / "More")
    (kodi_index ALL); full[letter] true gives the unmerged list instead. subs[0] is Off (kodi_index None) when there are subtitles."""
    full = full or {}
    acur = (props.get('currentaudiostream') or {}).get('index')
    scur = (props.get('currentsubtitle') or {}).get('index')
    enabled = bool(props.get('subtitleenabled'))
    astreams = [s for s in (props.get('audiostreams') or []) if s]
    sstreams = [s for s in (props.get('subtitles') or []) if s]

    def rows_for(streams, kind, cur, letter):
        if full.get(letter):
            return cap_rows(full_rows(streams, kind, cur, convert))
        if kind == 'sub':  # English / current / More only
            return simple_sub_rows(streams, cur, enabled, convert)
        if kind == 'audio':  # Original (when not English) / English / current / More
            return simple_audio_rows(streams, cur, convert)
        rows, merged = simple_rows(streams, kind, cur, convert)
        rows = cap_rows(rows)
        if merged:
            rows = rows[:MAX - 1] + [(ALL_LABEL, False, ALL)]
        return rows

    audio = rows_for(astreams, 'audio', acur, 'A')
    subs = rows_for(sstreams, 'sub', scur, 'S')
    if sstreams:
        subs = [('Off', not enabled, None)] + [(lab, cur and enabled, idx) for lab, cur, idx in subs]
    acs = next((i for i, r in enumerate(audio) if r[1]), 0)
    scs = next((i for i, r in enumerate(subs) if r[1]), 0)
    return audio, subs, acs, scs


def rpc(method, params):
    reply = json.loads(xbmc.executeJSONRPC(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
    if 'error' in reply:
        raise RuntimeError('%s: %s' % (method, reply['error'].get('message')))
    return reply.get('result')


def publish(rows, letter, cur):
    """Write the rows; blank the slots left over from a longer earlier list."""
    for i in range(MAX + 1):
        row = rows[i] if i < len(rows) else None
        # labels may contain commas or brackets: Home properties are set through the Python API, not a builtin
        win = xbmcgui.Window(10000)
        if row:
            win.setProperty('%s%s%d.L' % (PREFIX, letter, i), row[0])
            win.setProperty('%s%s%d.C' % (PREFIX, letter, i), '1' if row[1] else '')
        else:
            win.clearProperty('%s%s%d.L' % (PREFIX, letter, i))
            win.clearProperty('%s%s%d.C' % (PREFIX, letter, i))
    xbmcgui.Window(10000).setProperty('%s%s.Cur' % (PREFIX, letter), str(cur))


def read_props():
    for pid in (1, 0):
        try:
            return rpc('Player.GetProperties', {'playerid': pid, 'properties': [
                'audiostreams', 'currentaudiostream', 'subtitles', 'currentsubtitle', 'subtitleenabled']})
        except RuntimeError:
            continue
    return {}


LISTS = {'A': 6206, 'S': 6207}
CARDS = {'A': 1147, 'S': 1147}  # R10.4: one combined card; Back from either full list reopens it
INFO_CARD = 1145


def flags():
    win = xbmcgui.Window(10000)
    return {L: bool(win.getProperty('%s%s.Full' % (PREFIX, L))) for L in 'AS'}


def set_flag(letter, on):
    win = xbmcgui.Window(10000)
    if on:
        win.setProperty('%s%s.Full' % (PREFIX, letter), '1')
    else:
        win.clearProperty('%s%s.Full' % (PREFIX, letter))


def ready(token=None):
    """R10.56: the card's columns show once this opening's rows are written (Custom_1147 clears it on load / unload).
    token: the opening's populate run. Each opening clears Netflix.AV.Gen and its run writes its own token, so a run left over
    from an earlier opening (closed and reopened quickly) finds another value and leaves Ready to the current run."""
    home = xbmcgui.Window(10000)
    if token is not None and home.getProperty(PREFIX + 'Gen') != token:
        xbmc.log('Netflix AV: a populate from an earlier card opening ended late; Ready left to the current one', xbmc.LOGINFO)
        return
    home.setProperty(PREFIX + 'Ready', '1')
    xbmc.executebuiltin('CancelAlarm(NetflixAVReady,true)')


def populate(override=None):
    props = read_props()
    # Kodi applies SetAudioStream / SetSubtitle on its player thread: right after the call the properties can still show
    # the old choice, so the check follows what was just asked for (a later open reads Kodi's real state again).
    props.update(override or {})
    audio, subs, acs, scs = build(props, full=flags())
    publish(audio, 'A', acs)
    publish(subs, 'S', scs)
    return audio, subs, acs, scs


def already_current(row):
    """OK on a row that already has the check (any member of a merged row is current) changes nothing: it must not
    switch to the merged row's preferred stream."""
    return bool(row[1]) and row[2] != ALL


def should_reopen(cond, letter):
    """After Back from a full ("More") list: bring the card back only if the OSD is still up, the card is gone and the
    info card has not taken over (a second Back, a move to the info button or an OSD time-out in the 350 ms window).
    Both lists live in one card since R10.4, so there is no other track card to check."""
    return (cond('Player.HasVideo') and cond('Window.IsActive(videoosd)') and not cond('Window.IsVisible(%d)' % CARDS[letter])
            and not cond('Window.IsVisible(%d)' % INFO_CARD))


def focus(letter, slot):
    xbmc.sleep(250)  # let the card pick up the new rows first
    xbmc.executebuiltin('SetFocus(%d,%d,absolute)' % (LISTS[letter], slot))


def main(arg):
    if arg == 'populate':  # a card popped up: always start from the simplified lists
        token = '%.6f.%d' % (time.time(), id(sys.argv))   # this run (R10.56: see ready())
        xbmcgui.Window(10000).setProperty(PREFIX + 'Gen', token)
        try:
            set_flag('A', False)
            set_flag('S', False)
            populate()
        finally:
            ready(token)  # R10.56: also after an error (the rows then are what they are; never an empty card)
        return
    kind, _, value = arg.partition('=')
    if kind in ('audio_view', 'sub_view'):  # Back from "All tracks"
        letter = 'A' if kind == 'audio_view' else 'S'
        set_flag(letter, False)
        rows = populate()[0 if letter == 'A' else 1]
        # On some keymaps Back also closes the card (the Mac's backspace does; the Shield remote may not): wait a
        # moment and bring the card back if it went, so Back always lands in the simplified list.
        card = CARDS[letter]
        xbmc.sleep(350)
        if should_reopen(xbmc.getCondVisibility, letter):
            xbmc.executebuiltin('ActivateWindow(%d)' % card)
            for _ in range(20):
                if xbmc.getCondVisibility('Window.IsVisible(%d)' % card):
                    break
                xbmc.sleep(50)
            xbmc.sleep(300)
            populate()  # the card's own load run has reset the lists to simplified: publish again to be sure
        focus(letter, len(rows) - 1)  # onto the "All tracks" row it came from
        return
    try:
        # the rows carry their slot; the real Kodi stream index is looked up again from the live list
        audio, subs, _, _ = build(read_props(), full=flags())
        override = {}
        letter, rows = ('A', audio) if kind == 'audio' else ('S', subs)
        slot = int(value)
        seen = xbmcgui.Window(10000).getProperty('%s%s%d.L' % (PREFIX, letter, slot))
        if slot >= len(rows) or rows[slot][0] != seen:
            # the stream list changed since the card was filled (subtitle added, tracks found late): never apply a
            # different track than the one shown; refresh the card instead
            xbmc.log('Netflix AV: list changed under the card, refreshed instead of applying %s' % arg)
            populate()
            return
        if already_current(rows[slot]):
            populate()
            return
        target = rows[slot][2]
        if target == ALL:
            set_flag(letter, True)
            rows = populate()[0 if letter == 'A' else 1]
            focus(letter, next((i for i, r in enumerate(rows) if r[1]), 0))
            return
        if kind == 'audio':
            rpc('Player.SetAudioStream', {'playerid': 1, 'stream': target})
            override = {'currentaudiostream': {'index': target}}
        elif kind == 'sub' and target is None:
            rpc('Player.SetSubtitle', {'playerid': 1, 'subtitle': 'off'})
            override = {'subtitleenabled': False}
        elif kind == 'sub':
            rpc('Player.SetSubtitle', {'playerid': 1, 'subtitle': target, 'enable': True})
            override = {'subtitleenabled': True, 'currentsubtitle': {'index': target}}
        else:
            xbmc.log('Netflix AV: unknown argument %r' % arg, xbmc.LOGWARNING)
            return
    except (RuntimeError, ValueError, IndexError) as exc:
        xbmc.log('Netflix AV: could not apply %s: %s' % (arg, exc), xbmc.LOGERROR)
        xbmc.executebuiltin('Notification(Audio & Subtitles,Could not change the track,3000)')
        override = {}
    populate(override)  # moves the check to what Kodi really has now
    ready()


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'populate')
