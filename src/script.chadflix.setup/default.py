# -*- coding: utf-8 -*-
"""ChadFlix Setup.

Run once after installing. Safe to run again: every step checks first and only changes what differs.

What it does, in order:
  1. checks that the add-ons the look needs are installed and enabled (installs or enables any that are not);
  2. offers YouTube (for trailers), optional: Kodi's own install question is the only one;
  3. initializes missing or zero-byte Skin Variables menus and writes the ChadFlix layout settings for Arctic Fuse 3, including the flag that skips the
     skin's own setup wizard;
  4. switches the skin to Arctic Fuse 3 after asking, and answers Kodi's own "Keep skin?" question
     itself (see _switch_once for why; it answers that question only, never another yes/no dialog);
  5. copies five remote-control key files into userdata/keymaps and reloads them;
  6. closes Kodi once so every add-on restarts on its new version, if the person chose Close Kodi. When the skin is
     switched, that question is asked BEFORE the switch (see ask_close_kodi); when Arctic Fuse 3 is already
     the skin, it is asked at the end, as before.

The first menu also offers "Undo ChadFlix Keymaps": it removes the five key files (putting back any file they
replaced) so Back works normally again in another skin.

Every file it replaces is backed up first under this add-on's own data folder
(userdata/addon_data/script.chadflix.setup/backups/<date-time>/). It never reads or writes any
other add-on's settings, sign-ins, keys or history.
"""
import hashlib
import json
import os
import shutil
import stat

if os.name == 'posix':
    import fcntl
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
NODE_PROFILE = 'special://profile/addon_data/script.skinvariables/nodes/%s/' % SKIN
NODE_DEFAULTS = os.path.join(RES, 'home-row-defaults')
HOME_GENERATOR_HASH = 'script-skinvariables-generator-hash'
HOME_NODES = tuple('skinvariables-shortcut-%s.json' % menu for menu in (
    '1101submenu', '1101widgets', 'homesubmenu', 'homewidgets', 'hubs', 'powermenu', 'searchwidgets',
))
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
    """One question, not two. Kodi's InstallAddon always asks its own "Add-on required ... Would you like to
    download this add-on?" (No focused; No skips YouTube), so Setup no longer asks first. A silent notification says what
    YouTube is for while Kodi's question is up. Setup never answers Kodi's question."""
    if addon_state('plugin.video.youtube') is not None:
        return
    DIALOG.notification(TITLE, 'Trailers Use YouTube (Optional)', xbmcgui.NOTIFICATION_INFO, 10000, False)
    xbmc.executebuiltin('InstallAddon(plugin.video.youtube)', True)


# 3. skin layout settings ---------------------------------------------------------------------------
def home_node_stat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def home_node_needs_seed(entry):
    return entry is None or (
        stat.S_ISREG(entry.st_mode) and entry.st_size == 0 and entry.st_nlink == 1
    )


def open_existing_home_node(path, flags):
    flags &= ~os.O_CREAT
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    return os.open(path, flags)


def seed_home_nodes(dst):
    """Initialize missing or zero-byte nodes with the original JSON bytes."""
    os.makedirs(dst, exist_ok=True)
    created = 0
    for name in HOME_NODES:
        target = os.path.join(dst, name)
        existing = home_node_stat(target)
        if not home_node_needs_seed(existing):
            continue
        with open(os.path.join(NODE_DEFAULTS, name), 'rb') as source:
            contents = source.read()

        def open_output(path, flags):
            if existing is None:
                return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o666)
            return open_existing_home_node(path, flags)

        try:
            # Append mode cannot overwrite bytes if another writer fills the inode.
            node = open(target, 'a+b', buffering=0, opener=open_output)
        except (FileExistsError, FileNotFoundError):
            continue
        identity = None
        try:
            with node as output:
                if os.name == 'posix':
                    fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                current = home_node_stat(target)
                opened = os.fstat(output.fileno())
                if (current is None or not os.path.samestat(opened, current)
                        or not home_node_needs_seed(opened)
                        or (existing is not None and not os.path.samestat(opened, existing))):
                    continue
                identity = opened
                if output.write(contents) != len(contents):
                    raise OSError('Short write seeding %s' % name)
                output.flush()
                os.fsync(output.fileno())
                output.seek(0)
                current = home_node_stat(target)
                if (current is None or not os.path.samestat(identity, current)
                        or output.read(len(contents) + 1) != contents):
                    # A writer outside Setup can ignore the lock. Preserve its bytes and stop.
                    raise ValueError('Node changed while seeding %s' % name)
        except OSError:
            # Discard incomplete output only if the path and bytes still belong to this attempt.
            if identity is not None:
                discard_incomplete_home_node(target, identity, contents)
            raise
        created += 1
    return created


def discard_incomplete_home_node(path, identity, contents):
    try:
        current = home_node_stat(path)
        if current is None or not os.path.samestat(identity, current):
            return
        with open(path, 'rb', opener=open_existing_home_node) as node:
            if os.name == 'posix':
                fcntl.flock(node.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            opened = os.fstat(node.fileno())
            if (not os.path.samestat(identity, opened) or opened.st_nlink != 1
                    or opened.st_size > len(contents)
                    or not contents.startswith(node.read(len(contents) + 1))):
                return
            current = home_node_stat(path)
            if (current is not None and os.path.samestat(opened, current)
                    and current.st_size == opened.st_size and current.st_mtime_ns == opened.st_mtime_ns):
                os.unlink(path)
    except OSError:
        # Preserve the original write failure if the name cannot be checked or removed.
        pass


def check_home_nodes(dst):
    """Avoid asking the generator to replace unreadable nodes with stock menus."""
    for name in HOME_NODES:
        target = os.path.join(dst, name)
        entry = home_node_stat(target)
        if home_node_needs_seed(entry):
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError(name)
        try:
            with open(target, encoding='utf-8', opener=open_existing_home_node) as node:
                if (os.path.samestat(entry, os.fstat(node.fileno()))
                        and isinstance(json.load(node), list)):
                    continue
        except ValueError:
            pass
        raise ValueError(name)


def rebuild_home_nodes():
    # Same generator as Action_BuildShortcuts_OnLoad. The editor uses force
    # because the generator's cache key does not include the node bytes.
    last_build = CFG['skin_strings']['Shortcuts.RebuildDateTime']
    xbmc.executebuiltin(
        'RunScript(script.skinvariables,action=buildtemplate,lastbuildtime="%s",force=True)' % last_build
    )


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


def apply_settings_file(reset_home_build=False):
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
    if reset_home_build:
        # Home's onload will build the default includes from the new nodes.
        generator_hash = have.get(('string', HOME_GENERATOR_HASH))
        if generator_hash is not None:
            root.remove(generator_hash)
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
    """(heading, text) of the yes/no dialog on screen, '' for a part that cannot be read.
    Both are read as info labels (Control.GetLabel), which Kodi evaluates itself, and only while the yes/no dialog is the
    topmost window (the labels are then that dialog's own: control 9 is its text box, control 1 its heading).
    Never through xbmcgui.Window(10100).getControl(): on Kodi 21.3 getControl(9).getText() crashed Kodi (SIGSEGV in
    ControlTextBox.getText) when it ran while the dialog was still being loaded right after the skin switch, and
    getControl(1).getLabel() always returns '' for a label."""
    heading = text = ''
    try:
        if xbmc.getCondVisibility('Window.IsTopMost(yesnodialog)'):
            text = xbmc.getInfoLabel('Control.GetLabel(9)') or ''
            heading = xbmc.getInfoLabel('Control.GetLabel(1)') or ''
    except Exception:
        heading = text = ''
    return heading.strip(), text.strip()


def is_keep_skin_question():
    """True only when the yes/no dialog on screen is Kodi's "Keep skin?" question, compared with Kodi's own strings in
    the language Kodi is running in:
      - its text must be readable and equal string 13111 ("Would you like to keep this change?");
      - its heading must equal string 13123 ("Keep skin?") or be unreadable (empty).
    Any other yes/no dialog, and any dialog whose text cannot be read, is left for the person: this script never clicks it."""
    want_heading, want_text = xbmc.getLocalizedString(KEEP_HEADING).strip(), xbmc.getLocalizedString(KEEP_TEXT).strip()
    heading, text = _dialog_texts()
    if not want_text or not text or text != want_text:
        return False
    return not heading or heading == want_heading


def _switch_once():
    """Change the skin setting and answer Kodi's "Keep skin?" question with Yes.

    The question must be answered by this script: on the first start of Arctic Fuse 3, Skin Variables
    rebuilds one of the skin's include files and reloads the skin about half a second after it appears,
    which closes the question unanswered and makes Kodi switch back (seen in every first switch in testing).
    The person already agreed to the switch in the dialog before this.
    """
    rpc('Settings.SetSettingValue', {'setting': 'lookandfeel.skin', 'value': SKIN})
    answered, skipped, seen = False, False, False
    for _ in range(600):  # up to 60 s: a first load on a slow box can take a while
        xbmc.sleep(100)
        active = xbmc.getSkinDir() == SKIN and xbmc.getCondVisibility('Window.IsActive(yesnodialog)')
        if not answered and active and seen:
            # Only Kodi's own "Keep skin?" question is answered. Any other yes/no dialog that happens to be open now
            # is not touched (checked again on every pass: the keep-skin question may still follow it). The dialog is
            # read on the pass after it was first seen (0.1 s later), not in the instant it is being loaded.
            if is_keep_skin_question():
                xbmc.executebuiltin('SendClick(%d,11)' % YESNO_DIALOG)  # 11 = Yes in Kodi's yes/no dialog
                answered = True
                log('keep-skin question answered: Yes')
            elif not skipped:
                skipped = True
                log('a yes/no dialog that is not the keep-skin question is open: left alone')
        seen = active
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


def ask_close_kodi(before_switch):
    """True when the person chose Close Kodi.

    On the switch path this is asked BEFORE the switch. The first start of Arctic Fuse 3 is followed by skin
    reloads (Skin Variables builds the Home include and runs ReloadSkin()), and a reload closes every dialog on screen.
    Asked after the switch (Setup 1.0.5 and older), the question was closed by such a reload on an NVIDIA Shield and
    Python read the closed dialog as "Later", so nobody was told to restart. Asked here, nothing can close it; the answer is
    acted on only once the skin is kept and the key files are in. Already on the skin, nothing reloads before the
    question, so it is asked at the end with the old text."""
    if before_switch:
        text = ('When the new look has loaded, Kodi needs to close once so every add-on restarts on its new version. '
                'Close Kodi then? Open Kodi again afterwards.')
    else:
        text = ('All set.[CR][CR]Kodi needs to close once so every add-on restarts on its new version. '
                'Close Kodi now? Then open Kodi again.')
    return DIALOG.yesno(TITLE, text + '[CR][CR]After that: gear (top right) > Real-Debrid to sign in.',
                        nolabel='Later', yeslabel='Close Kodi')


def switch_skin():
    """(kept, close_now). kept: True when Arctic Fuse 3 is the skin afterwards, DECLINED when the person chose Not now,
    else False. close_now: the answer to ask_close_kodi, asked here before the switch; None when no switch was needed or
    the person chose Not now."""
    if xbmc.getSkinDir() == SKIN:
        return True, None
    if not DIALOG.yesno(TITLE, 'Ready to switch Kodi to the new look.[CR][CR]'
                               'The screen goes dark for a few seconds while it loads. Switch now?',
                        nolabel='Not now', yeslabel='Switch'):
        return DECLINED, None
    close_now = ask_close_kodi(before_switch=True)
    kept = False
    for attempt in (1, 2):
        kept = _switch_once()
        log('skin switch attempt %d kept: %s' % (attempt, kept))
        if kept:
            break
    return kept, close_now


# 5. keymaps ----------------------------------------------------------------------------------------
KEYMAPS_STATE = os.path.join(OWN_DATA, 'keymaps-state.json')   # shared with service.py (see there)


def _keymaps_state(**changes):
    """Update keymaps-state.json: what the start-up service may still add by itself."""
    try:
        state = json.load(open(KEYMAPS_STATE, encoding='utf-8'))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state.update(changes)
    os.makedirs(OWN_DATA, exist_ok=True)
    tmp = KEYMAPS_STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, KEYMAPS_STATE)


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
    # Setup ran: every key file of this version is on the device, Undo is no longer in force
    _keymaps_state(undone=False, offered=list(CFG.get('keymaps_new', [])))
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
    """Remove the key files this setup installed and put back any file they replaced."""
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
    # the start-up service adds nothing by itself until Setup is run again (a kept or restored file is not "Setup ran")
    _keymaps_state(undone=True)
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
    default_user = not on_skin or not xbmc.getInfoLabel('Skin.String(SkinVariables.SkinUser)')
    node_dir = xbmcvfs.translatePath(NODE_PROFILE)
    try:
        if default_user and (not on_skin or any(
                home_node_needs_seed(home_node_stat(os.path.join(node_dir, name))) for name in HOME_NODES)):
            check_home_nodes(node_dir)
        nodes_created = seed_home_nodes(node_dir)
    except (OSError, ValueError):
        log('Could not prepare the home row defaults.')
        DIALOG.ok(TITLE, 'Could Not Prepare Home Rows. Check The Skin Variables Menu Files And Run Setup Again.')
        return
    rebuild_on_active_skin = on_skin and default_user and nodes_created > 0
    if rebuild_on_active_skin:
        # Home can rebuild on restart even if this Setup run stops here.
        xbmc.executebuiltin('Skin.Reset(%s)' % HOME_GENERATOR_HASH, True)
    if on_skin:
        apply_settings_live()
    else:
        apply_settings_file(reset_home_build=bool(nodes_created))
        xbmc.executebuiltin('SetProperty(Netflix.MoviesBoot.SessionSeen,true,Home)', True)

    kept, close_now = switch_skin()
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

    if close_now is None:  # no switch happened, so no reload is pending: ask now, as before
        close_now = ask_close_kodi(before_switch=False)
    log('close Kodi now: %s' % bool(close_now))
    if close_now:
        xbmc.executebuiltin('Quit')
    elif rebuild_on_active_skin:
        # ReloadSkin can close Setup's dialog, so build after the prompt.
        rebuild_home_nodes()


def main():
    # short enough not to scroll (the old line did); same promise
    choice = DIALOG.select(TITLE, ['Set Up ChadFlix (Keeps Sign-Ins, Keys, History)',
                                   'Undo ChadFlix Keymaps (do this before going back to another skin)'])
    if choice == 0:
        setup()
    elif choice == 1:
        undo_keymaps()


if __name__ == '__main__':
    main()
