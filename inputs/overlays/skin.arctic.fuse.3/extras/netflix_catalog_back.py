"""Compatibility stub (ChadFlix release 2): Back for devices that still have the FIRST release's key file.

The first public release (R8.6, ChadFlix Setup 1.0.1) installed userdata/keymaps/NetflixRootBack.xml, which runs
    RunScript(special://home/addons/skin.arctic.fuse.3/extras/netflix_catalog_back.py,<window id>)
on every Back in Home (10000), Videos (10025), Search (11105) and My Lists (11165). R9.2 replaced that key file with
plain actions and R9.3 deleted this script. Setup 1.0.2 swaps the old key file at start-up, but a device where Setup
was uninstalled, disabled or not updated keeps the old file; without this stub Back would do nothing there.

This stub does exactly what the CURRENT NetflixRootBack.xml does in each window, nothing else:
    10000 Home       -> nothing       (current file: noop)
    10025 Videos     -> Action(Back)  (current file: Back; the focused control's own <onback> runs)
    11105 Search     -> Action(Back)  (current file: Back)
    11165 My Lists   -> SetFocus(3734) (current file: SetFocus(3734), the My Lists tab)
It reads no settings or files and starts nothing else. It never runs on a device with the current key file.
Removable once a later release no longer needs to support updating straight from the first release.
"""
import sys

import xbmc
import xbmcgui

ACTIONS = {10025: 'Action(Back)', 11105: 'Action(Back)', 11165: 'SetFocus(3734)'}


def main(argv):
    try:
        window_id = int(argv[1])
    except (IndexError, ValueError):
        return
    action = ACTIONS.get(window_id)
    # a late key press must never act on another window or on a dialog that opened meanwhile (as the old script)
    if not action or xbmcgui.getCurrentWindowId() != window_id or xbmc.getCondVisibility('System.HasActiveModalDialog'):
        return
    xbmc.executebuiltin(action)


if __name__ == '__main__':
    main(sys.argv)
