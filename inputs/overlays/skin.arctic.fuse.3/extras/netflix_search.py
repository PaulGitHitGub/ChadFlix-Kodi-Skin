"""Search-only edit actions using Kodi GUI messages, without native control bindings."""
import json
import sys
import time

import xbmc
import xbmcgui

SEARCH_WINDOW = 11105
EDIT_CONTROL = '3000'
CLEAR_CONTROL = '3062'
KEYBOARD_CONTROL = '3002'


def active_search():
    return (xbmcgui.getCurrentWindowId() == SEARCH_WINDOW
            and not xbmc.getCondVisibility('System.HasActiveModalDialog'))


def focus():
    return xbmc.getInfoLabel('System.CurrentControlID')


def clear_search():
    # A queued script must not clear a different field after the user moves away.
    if (not active_search() or focus() != CLEAR_CONTROL
            or not xbmc.getInfoLabel('Control.GetLabel(3000).index(1)')):
        return False
    monitor = xbmc.Monitor()
    xbmc.executebuiltin('SetFocus(3000)', wait=True)
    deadline = time.monotonic() + .35
    while active_search() and focus() == CLEAR_CONTROL and time.monotonic() < deadline:
        if monitor.waitForAbort(.01):
            return False
    if not active_search() or focus() != EDIT_CONTROL or monitor.abortRequested():
        return False

    # Input.SendText posts GUI_MSG_SET_TEXT to the focused edit control. It does
    # not dereference a Python ControlEdit wrapper for a skin-owned native field.
    result = json.loads(xbmc.executeJSONRPC(json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': 'Input.SendText',
        'params': {'text': '', 'done': False},
    })))
    if 'error' in result:
        xbmc.log('Netflix Search: Clear Search was rejected by Kodi.', xbmc.LOGWARNING)
        return False
    deadline = time.monotonic() + .35
    while active_search() and focus() == EDIT_CONTROL and not monitor.abortRequested():
        if not xbmc.getInfoLabel('Control.GetLabel(3000).index(1)'):
            xbmc.executebuiltin('SetFocus(' + KEYBOARD_CONTROL + ')', wait=True)
            return True
        if time.monotonic() >= deadline or monitor.waitForAbort(.01):
            break
    return False


if __name__ == '__main__' and sys.argv[1:] == ['clear']:
    clear_search()
