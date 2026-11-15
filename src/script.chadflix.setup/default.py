# -*- coding: utf-8 -*-
"""ChadFlix Setup.

Run once after installing. Safe to run again: every step checks first and only changes what differs.

What it does, in order:
  1. checks that the add-ons the look needs are installed and enabled (installs or enables any that are not);
  2. offers YouTube (for trailers), optional;
  3. writes the ChadFlix layout settings for Arctic Fuse 3, including the flag that skips the
     skin's own setup wizard (only these skin settings; nothing else in any add-on's data);
  4. switches the skin to Arctic Fuse 3 after asking, and answers Kodi's own "Keep skin?" question
     itself (see _switch_once for why; it answers that question only, never another yes/no dialog);
  5. copies four remote-control key files into userdata/keymaps and reloads them;
  6. offers to close Kodi once so every add-on restarts on its new version.

The first menu also offers "Undo ChadFlix keymaps": it removes the four key files (putting back any file they
replaced) so Back works normally again in another skin.

Every file it replaces is backed up first under this add-on's own data folder
(userdata/addon_data/script.chadflix.setup/backups/<date-time>/). It never reads or writes any
other add-on's settings, sign-ins, keys or history.
"""
import hashlib
import json
import os
import shutil
import time
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
TITLE = ADDON.getAddonInfo('name')
HERE = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
OWN_DATA = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
RES = os.path.join(HERE, 'resources')
CFG = json.load(open(os.path.join(RES, 'setup.json'), encoding='utf-8'))
SKIN = CFG['skin']
KEYMAPS_SRC = os.path.join(RES, 'keymaps')
KEYMAPS_DST = xbmcvfs.translatePath('special://masterprofile/keymaps/')
SKIN_SETTINGS = xbmcvfs.translatePath('special://profile/addon_data/%s/settings.xml' % SKIN)
STAMP = time.strftime('%Y%m%d-%H%M%S')
BACKUP = os.path.join(OWN_DATA, 'backups', STAMP)
DIALOG = xbmcgui.Dialog()


def log(msg):
    xbmc.log('[%s] %s' % (ADDON.getAddonInfo('id'), msg), xbmc.LOGINFO)


def rpc(method, params=None):
    req = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params is not None:
        req['params'] = params
    return json.loads(xbmc.executeJSONRPC(json.dumps(req)))


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def backup(path, name):
    """Copy an existing file into this add-on's own backup folder before it is replaced."""
    if not os.path.isfile(path):
        return
    dst = os.path.join(BACKUP, name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(path, dst)
    log('backed up %s -> %s' % (path, dst))


def vtuple(v):
    out = []
    for part in v.replace('+', '.').replace('~', '.').split('.'):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out)


# 1. add-ons ---------------------------------------------------------------------------------------
def addon_state(addon_id):
    r = rpc('Addons.GetAddonDetails', {'addonid': addon_id, 'properties': ['enabled', 'version']})
    if 'result' not in r:
        return None
    return r['result']['addon']


def ensure_addons():
    problems = []
    for addon_id, min_version in CFG['requires']:
        st = addon_state(addon_id)
        if st is None:
            log('installing %s' % addon_id)
            xbmc.executebuiltin('InstallAddon(%s)' % addon_id, True)
            for _ in range(180):
                st = addon_state(addon_id)
                if st is not None:
                    break
                xbmc.sleep(1000)
        if st is None:
            problems.append('%s is not installed' % addon_id)
            continue
        if not st.get('enabled'):
            log('enabling %s' % addon_id)
            rpc('Addons.SetAddonEnabled', {'addonid': addon_id, 'enabled': True})
            st = addon_state(addon_id) or st
            if not st.get('enabled'):
                problems.append('%s is disabled' % addon_id)
        if min_version and vtuple(st.get('version', '0')) < vtuple(min_version):
            problems.append('%s is version %s, needs %s or newer' % (addon_id, st.get('version'), min_version))
    return problems


def offer_youtube():
    if addon_state('plugin.video.youtube') is not None:
        return
    if DIALOG.yesno(TITLE, 'Trailers play through the YouTube add-on, which is not installed.[CR][CR]'
                           'Install YouTube now? (Optional. You can skip this.)', nolabel='Skip', yeslabel='Install'):
        xbmc.executebuiltin('InstallAddon(plugin.video.youtube)', True)


# 3. skin layout settings ---------------------------------------------------------------------------
def wanted_settings():
    bools = [b.lower() for b in CFG['skin_bools']]
    return bools, CFG['skin_strings']


def apply_settings_live():
    """The skin is running: Kodi holds its settings in memory, so set them through Kodi."""
    bools, strings = wanted_settings()
    changed = 0
    for b in bools:
        if not xbmc.getCondVisibility('Skin.HasSetting(%s)' % b):
            xbmc.executebuiltin('Skin.SetBool(%s)' % b)
            changed += 1
    for k, v in strings.items():
        if xbmc.getInfoLabel('Skin.String(%s)' % k) != v:
            xbmc.executebuiltin('Skin.SetString(%s,"%s")' % (k, v))
            changed += 1
    log('live skin settings changed: %d' % changed)
    return changed


def apply_settings_file():
    """The skin is not running: merge our values into its settings file (Kodi reads it when the skin loads)."""
    bools, strings = wanted_settings()
    root = None
    if os.path.isfile(SKIN_SETTINGS):
        try:
            root = ET.parse(SKIN_SETTINGS).getroot()
        except ET.ParseError:
            root = None
    if root is None:
        root = ET.Element('settings')
    have = {}
    for el in root.findall('setting'):
        have[(el.get('type'), el.get('id') if el.get('type') == 'string' else (el.get('id') or '').lower())] = el
    changed = 0
    for b in bools:
        el = have.get(('bool', b))
        if el is None:
            el = ET.SubElement(root, 'setting', {'id': b, 'type': 'bool'})
        if el.text != 'true':
            el.text = 'true'
            changed += 1
    for k, v in strings.items():
        el = have.get(('string', k))
        if el is None:
            el = ET.SubElement(root, 'setting', {'id': k, 'type': 'string'})
        if el.text != v:
            el.text = v
            changed += 1
    if changed:
        backup(SKIN_SETTINGS, 'skin-settings/settings.xml')
        os.makedirs(os.path.dirname(SKIN_SETTINGS), exist_ok=True)
        tmp = SKIN_SETTINGS + '.btn-tmp'
        ET.ElementTree(root).write(tmp, encoding='utf-8', xml_declaration=False)
        os.replace(tmp, SKIN_SETTINGS)
    log('skin settings file changed: %d' % changed)
    return changed


# 4. skin switch ------------------------------------------------------------------------------------
YESNO_DIALOG = 10100          # Kodi's yes/no dialog
KEEP_HEADING, KEEP_TEXT = 13123, 13111   # Kodi's own strings: "Keep skin?" / "Would you like to keep this change?"


def _dialog_texts():
    """(heading, text) of the yes/no dialog on screen, '' for a part that cannot be read."""
    heading = text = ''
    try:
        window = xbmcgui.Window(YESNO_DIALOG)
        try:
            heading = window.getControl(1).getLabel() or ''
        except Exception:
            heading = ''
        try:
            text = window.getControl(9).getText() or ''
        except Exception:
            text = ''
    except Exception:
        pass
    return heading.strip(), text.strip()


def is_keep_skin_question():
    """True only when the yes/no dialog on screen is Kodi's "Keep skin?" question, compared with Kodi's own strings in
    the language Kodi is running in. Any other yes/no dialog (another add-on's question, an update prompt), or a
    dialog whose heading cannot be read, is left for the person to answer: this script never clicks it."""
    want_heading, want_text = xbmc.getLocalizedString(KEEP_HEADING).strip(), xbmc.getLocalizedString(KEEP_TEXT).strip()
    heading, text = _dialog_texts()
    if not want_heading or heading != want_heading:
        return False
    return not text or not want_text or text == want_text


def _switch_once():
    """Change the skin setting and answer Kodi's "Keep skin?" question with Yes.

    The question must be answered by this script: on the first start of Arctic Fuse 3, Skin Variables
    rebuilds one of the skin's include files and reloads the skin about half a second after it appears,
    which closes the question unanswered and makes Kodi switch back (seen in every first switch in testing).
    The person already agreed to the switch in the dialog before this.
    """
    rpc('Settings.SetSettingValue', {'setting': 'lookandfeel.skin', 'value': SKIN})
    answered, skipped = False, False
    for _ in range(600):  # up to 60 s: a first load on a slow box can take a while
        xbmc.sleep(100)
        if not answered and xbmc.getSkinDir() == SKIN and xbmc.getCondVisibility('Window.IsActive(yesnodialog)'):
            # Only Kodi's own "Keep skin?" question is answered. Any other yes/no dialog that happens to be open now
            # is not touched (checked again on every pass: the keep-skin question may still follow it).
            if is_keep_skin_question():
                xbmc.executebuiltin('SendClick(%d,11)' % YESNO_DIALOG)  # 11 = Yes in Kodi's yes/no dialog
                answered = True
                log('keep-skin question answered: Yes')
            elif not skipped:
                skipped = True
                log('a yes/no dialog that is not the keep-skin question is open: left alone')
        if answered and not xbmc.getCondVisibility('Window.IsActive(yesnodialog)'):
            break
    # let any skin reload (Skin Variables) or a switch back finish before deciding
    xbmc.sleep(4000)
    for _ in range(40):
        if not xbmc.getCondVisibility('Window.IsActive(yesnodialog)') and \
                not xbmc.getCondVisibility('Window.IsActive(progressdialog)'):
            break
        xbmc.sleep(500)
    return xbmc.getSkinDir() == SKIN


DECLINED = 'declined'


def switch_skin():
    """True when Arctic Fuse 3 is the skin afterwards, DECLINED when the person chose Not now, else False."""
    if xbmc.getSkinDir() == SKIN:
        return True
    if not DIALOG.yesno(TITLE, 'Ready to switch Kodi to the new look.[CR][CR]'
                               'The screen goes dark for a few seconds while it loads. Switch now?',
                        nolabel='Not now', yeslabel='Switch'):
        return DECLINED
    kept = False
    for attempt in (1, 2):
        kept = _switch_once()
        log('skin switch attempt %d kept: %s' % (attempt, kept))
        if kept:
            break
    return kept


# 5. keymaps ----------------------------------------------------------------------------------------
def install_keymaps():
    os.makedirs(KEYMAPS_DST, exist_ok=True)
    changed = 0
    for name in CFG['keymaps']:
        src = os.path.join(KEYMAPS_SRC, name)
        dst = os.path.join(KEYMAPS_DST, name)
        if os.path.isfile(dst) and sha(dst) == sha(src):
            continue
        backup(dst, 'keymaps/' + name)
        shutil.copyfile(src, dst)
        changed += 1
    if changed:
        xbmc.executebuiltin('Action(reloadkeymaps)')
    log('keymaps changed: %d' % changed)
    return changed


def save_state(kept):
    os.makedirs(OWN_DATA, exist_ok=True)
    with open(os.path.join(OWN_DATA, 'last-run.json'), 'w', encoding='utf-8') as f:
        json.dump({'time': STAMP, 'version': ADDON.getAddonInfo('version'), 'skin_kept': kept}, f)


def _latest_backup(name, shipped_sha):
    """Newest backed-up copy of a keymap that differs from the one this setup ships, or None."""
    root = os.path.join(OWN_DATA, 'backups')
    if not os.path.isdir(root):
        return None
    for stamp in sorted(os.listdir(root), reverse=True):
        p = os.path.join(root, stamp, 'keymaps', name)
        if os.path.isfile(p) and sha(p) != shipped_sha and sha(p) not in CFG.get('keymaps_previous', {}).get(name, []):
            return p
    return None


def undo_keymaps():
    """Remove the four key files this setup installed and put back any file they replaced."""
    removed, restored, kept = [], [], []
    for name in CFG['keymaps']:
        dst = os.path.join(KEYMAPS_DST, name)
        shipped = sha(os.path.join(KEYMAPS_SRC, name))
        if os.path.isfile(dst):
            if sha(dst) != shipped and sha(dst) not in CFG.get('keymaps_previous', {}).get(name, []):
                kept.append(name)  # not ours (changed since): leave it alone
                continue
            os.remove(dst)
            removed.append(name)
        old = _latest_backup(name, shipped)
        if old:
            shutil.copyfile(old, dst)
            restored.append(name)
    xbmc.executebuiltin('Action(reloadkeymaps)')
    log('undo keymaps: removed %s, restored %s, left %s' % (removed, restored, kept))
    msg = 'Removed %d ChadFlix key file(s)' % len(removed)
    if restored:
        msg += ' and put back %d earlier file(s)' % len(restored)
    msg += '. Back works the normal way again.'
    if kept:
        msg += '[CR]Left alone (changed since setup): ' + ', '.join(kept)
    if xbmc.getSkinDir() == SKIN:
        msg += ('[CR][CR]Now switch the skin back: Settings > Interface > Skin > Skin > [B]Estuary[/B], '
                'and choose Yes to keep it.')
    DIALOG.ok(TITLE, msg)


def setup():
    progress = xbmcgui.DialogProgress()
    progress.create(TITLE, 'Checking add-ons...')
    problems = ensure_addons()
    progress.close()
    if problems:
        DIALOG.ok(TITLE, 'Some add-ons are not ready:[CR]' + '[CR]'.join(problems) +
                  '[CR][CR]Open Add-ons > Install from repository > ChadFlix Repository, '
                  'install or update them, then run this setup again.')
        return
    offer_youtube()

    on_skin = xbmc.getSkinDir() == SKIN
    if on_skin:
        apply_settings_live()
    else:
        apply_settings_file()

    kept = switch_skin()
    save_state(kept is True)
    if kept == DECLINED:
        DIALOG.ok(TITLE, 'The layout settings are saved, but the skin was not switched.[CR][CR]'
                         'Run ChadFlix Setup again when you are ready to switch.')
        return
    if not kept:
        DIALOG.ok(TITLE, 'Kodi went back to the old look, so the key files were not added.[CR][CR]'
                         'Restart Kodi and run ChadFlix Setup again.')
        return
    if not on_skin:
        apply_settings_live()  # in case the skin started before reading the file (no-op when it did)
    install_keymaps()

    if DIALOG.yesno(TITLE, 'All set.[CR][CR]Kodi needs to close once so every add-on restarts on its new version. '
                           'Close Kodi now? Then open Kodi again.[CR][CR]'
                           'After that: gear (top right) > Real-Debrid to sign in.',
                    nolabel='Later', yeslabel='Close Kodi'):
        xbmc.executebuiltin('Quit')


def main():
    choice = DIALOG.select(TITLE, ['Set up ChadFlix (sign-ins, keys and watch history are not touched)',
                                   'Undo ChadFlix keymaps (do this before going back to another skin)'])
    if choice == 0:
        setup()
    elif choice == 1:
        undo_keymaps()


if __name__ == '__main__':
    main()
