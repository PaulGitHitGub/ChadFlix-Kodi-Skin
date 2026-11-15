# -*- coding: utf-8 -*-
"""ChadFlix Setup: keep the ChadFlix key files current after an update (script.chadflix.setup 1.0.3).

Runs once at every Kodi start and right after this add-on updates, then exits (no loop, no polling).
For each of the key files Setup installs:
  - missing                      -> nothing (Setup never ran, or the person chose "Undo ChadFlix Keymaps"), with one exception:
                                    a key file that is NEW in this Setup version (setup.json "keymaps_new") is added when at
                                    least one other ChadFlix key file is installed, i.e. Setup ran and was not undone. Someone
                                    who updated from an older Setup then gets the new key without running Setup again;
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


def main():
    cfg = json.load(open(os.path.join(RES, 'setup.json'), encoding='utf-8'))
    previous = cfg.get('keymaps_previous', {})
    stamp = time.strftime('%Y%m%d-%H%M%S')
    changed = []
    installed = [n for n in cfg['keymaps'] if os.path.isfile(os.path.join(KEYMAPS_DST, n))]
    for name in cfg['keymaps']:
        src = os.path.join(RES, 'keymaps', name)
        dst = os.path.join(KEYMAPS_DST, name)
        if not os.path.isfile(dst):
            # new in this Setup version, and the person has the ChadFlix key files (not undone, not a fresh device): add it
            if name in cfg.get('keymaps_new', []) and any(n != name for n in installed):
                tmp = dst + '.chadflix-tmp'
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
                changed.append(name)
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
    if changed:
        xbmc.executebuiltin('Action(reloadkeymaps)')
    log('key files updated: %s' % (changed or 'none'))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # never break Kodi start-up
        log('error: %r' % e)
