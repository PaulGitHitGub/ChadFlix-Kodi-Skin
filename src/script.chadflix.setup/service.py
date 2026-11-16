# -*- coding: utf-8 -*-
"""ChadFlix Setup: keep the ChadFlix key files current after an update (script.chadflix.setup 1.0.4).

Runs once at every Kodi start and right after this add-on updates, then exits (no loop, no polling).
For each of the key files Setup installs:
  - missing                      -> nothing (Setup never ran, or the person chose "Undo ChadFlix Keymaps"), with one exception:
                                    a key file that is NEW in this Setup version (setup.json "keymaps_new") is added, ONCE,
                                    when Setup ran and was not undone: at least one other key file on the device is a file
                                    ChadFlix shipped (same content as the shipped one or an older shipped one; a file the
                                    person edited, or a foreign file Undo put back, does not count), and Undo has not been
                                    used since. Someone who updated from an older Setup then gets the new key without
                                    running Setup again. What was offered is recorded in this add-on's own data folder
                                    (keymaps-state.json), so a new key file the person deletes does not come back;
  - equal to the shipped file    -> nothing;
  - equal to a file an OLDER ChadFlix Setup shipped (hash in setup.json "keymaps_previous")
                                 -> back it up (same backup folder as Setup) and replace it with the shipped file;
  - anything else                -> nothing (the person changed it; it is theirs).
Reloads keymaps only when something changed. Never reads or writes any settings, sign-ins or other add-on data.
"""
import hashlib
import json
import os
import shutil
import time

import xbmc
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon()
HERE = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
OWN_DATA = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
RES = os.path.join(HERE, 'resources')
KEYMAPS_DST = xbmcvfs.translatePath('special://masterprofile/keymaps/')


def log(msg):
    xbmc.log('[%s service] %s' % (ADDON.getAddonInfo('id'), msg), xbmc.LOGINFO)


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


STATE = os.path.join(OWN_DATA, 'keymaps-state.json')   # {"offered": [key files added or seen once], "undone": true after Undo}


def read_state():
    try:
        state = json.load(open(STATE, encoding='utf-8'))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state):
    os.makedirs(OWN_DATA, exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def main():
    cfg = json.load(open(os.path.join(RES, 'setup.json'), encoding='utf-8'))
    previous = cfg.get('keymaps_previous', {})
    stamp = time.strftime('%Y%m%d-%H%M%S')
    changed = []
    state = read_state()
    offered = list(state.get('offered', []))

    def ours(name):
        """The file on the device is one ChadFlix shipped: the current one or an older shipped one."""
        dst = os.path.join(KEYMAPS_DST, name)
        return os.path.isfile(dst) and sha(dst) in [sha(os.path.join(RES, 'keymaps', name))] + list(previous.get(name, []))
    installed = [n for n in cfg['keymaps'] if ours(n)]
    for name in cfg['keymaps']:
        src = os.path.join(RES, 'keymaps', name)
        dst = os.path.join(KEYMAPS_DST, name)
        if name in cfg.get('keymaps_new', []) and name not in offered and os.path.isfile(dst):
            offered.append(name)            # already there (Setup installed it): from now on a deletion is the person's choice
        if not os.path.isfile(dst):
            # new in this Setup version: add it once, for a person who has ChadFlix's own key files and has not used Undo
            if (name in cfg.get('keymaps_new', []) and name not in offered and not state.get('undone')
                    and any(n != name for n in installed)):
                tmp = dst + '.chadflix-tmp'
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
                changed.append(name)
                offered.append(name)
            continue
        have = sha(dst)
        if have == sha(src) or have not in previous.get(name, []):
            continue
        bdir = os.path.join(OWN_DATA, 'backups', stamp + '-update', 'keymaps')
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(dst, os.path.join(bdir, name))
        tmp = dst + '.chadflix-tmp'
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        changed.append(name)
    if offered != list(state.get('offered', [])):
        write_state(dict(state, offered=offered))
    if changed:
        xbmc.executebuiltin('Action(reloadkeymaps)')
    log('key files updated: %s' % (changed or 'none'))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # never break Kodi start-up
        log('error: %r' % e)
