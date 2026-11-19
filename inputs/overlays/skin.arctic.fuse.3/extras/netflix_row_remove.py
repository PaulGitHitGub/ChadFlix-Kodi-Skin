"""Commit a single Continue Watching or Favorites removal from the native context dialog."""
import re
import sys
import time

import xbmc
import xbmcgui

from netflix_saved_lists import pov_api


CONTINUE_TYPES = {594: 'movie', 595: 'tvshow'}
FAVORITE_REVISIONS = {'movie': 'Netflix.MyList.Revision', 'tvshow': 'Netflix.MyList.ShowsRevision'}
REFRESH_REQUEST = 'Netflix.Browse.RefreshRequest'
REMOVE_LABEL = 'Remove From Row'
REMOVE_ERROR = 'Could Not Remove. Try Again.'


def title_id(value):
    return bool(re.fullmatch(r'[1-9][0-9]{0,11}', value))


def continue_title(row):
    """Read the typed identity only from the focused, loaded Continue Watching provider."""
    if row not in CONTINUE_TYPES or not xbmc.getCondVisibility('Window.IsActive(Videos)'):
        return None
    if xbmc.getInfoLabel('System.CurrentControlID') != str(row):
        return None
    if xbmc.getCondVisibility('Container(%d).IsUpdating' % row):
        return None
    info = 'Container(%d).ListItem.' % row
    kind = xbmc.getInfoLabel(info + 'DBType')
    if (kind != CONTINUE_TYPES[row]
            or xbmc.getInfoLabel(info + 'Property(Netflix.CW.Kind)') not in ('resume', 'next')):
        return None
    media_id = xbmc.getInfoLabel(info + 'UniqueID(tmdb)') or xbmc.getInfoLabel(info + 'Property(tmdb_id)')
    return (kind, media_id) if title_id(media_id) else None


def continue_remove(row):
    target = continue_title(row)
    if target is None or xbmc.getCondVisibility('System.HasActiveModalDialog'):
        return
    if xbmcgui.Dialog().contextmenu([REMOVE_LABEL]) != 0 or continue_title(row) != target:
        return
    with pov_api() as api:
        removed = api.hide_continue(*target)
    if removed:
        # The existing browse coordinator owns the row's provider refresh and focus.
        xbmcgui.Window(10000).setProperty(REFRESH_REQUEST, '%d|cw-remove:%d' % (time.time_ns(), row))
    else:
        xbmcgui.Dialog().notification(REMOVE_LABEL, REMOVE_ERROR)


def favorite_remove(kind, media_id):
    if kind not in FAVORITE_REVISIONS or not title_id(media_id):
        return
    home = xbmcgui.Window(10000)
    if not xbmc.getCondVisibility('Window.IsActive(1165)') or home.getProperty('Netflix.MyLists.View'):
        return
    # The provider's native context action carries the exact item it was opened on.
    # Delete only the local Favorite. The idempotent writer cannot toggle it back on.
    with pov_api() as api:
        removed = api.remove_from_sync('favorites', kind, media_id, '')
    if removed:
        home.setProperty(FAVORITE_REVISIONS[kind], str(time.time_ns()))
    else:
        xbmcgui.Dialog().notification(REMOVE_LABEL, REMOVE_ERROR)


def main(args):
    if len(args) == 1 and args[0] in ('594', '595'):
        continue_remove(int(args[0]))
    elif len(args) == 3 and args[0] == 'favorite':
        favorite_remove(args[1], args[2])


if __name__ == '__main__':
    main(sys.argv[1:])
