"""Local watchlist folders and mixed title catalogues; no metadata/API requests."""
from importlib import util
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
from modules import kodi_utils

POV = 'plugin.video.pov'
_SIZE = re.compile(r'/t/p/(?:original|w[1-9][0-9]{0,4}|h[1-9][0-9]{0,4})/([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:jpg|jpeg|png|webp))', re.I)


def backend():
    # Load the active skin's existing store without sharing a second JSON schema.
    if xbmc.getSkinDir() != 'skin.arctic.fuse.3':
        raise RuntimeError('Watchlists require the configured skin')
    source = Path(xbmcvfs.translatePath('special://skin/extras/netflix_saved_lists.py'))
    spec = util.spec_from_file_location('_pov_netflix_saved_lists', source)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route(mode, **params):
    return 'plugin://' + POV + '/?' + urlencode({'mode': mode, **params})


def script(command, *args):
    # Callers supply only validated UUIDs, media types and decimal TMDb IDs.
    return 'RunScript(special://skin/extras/netflix_saved_lists.py,' + ','.join((command, *args)) + ')'


def browse_art(url, size):
    if not isinstance(url, str) or not url:
        return ''
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    match = _SIZE.fullmatch(parsed.path)
    if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != 'image.tmdb.org' or not match:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, '/t/p/' + size + '/' + match[1], parsed.query, parsed.fragment))


def collection_rows(store, api):
    rows = []
    for collection in store.read()['lists']:
        identifier = api.list_id(collection['id'])
        count = len(collection['items'])
        name = api.clean_text(collection['name'], 80, True)
        item = xbmcgui.ListItem(label=name, label2='%d %s' % (count, 'title' if count == 1 else 'titles'))
        item.setProperties({'Netflix.Watchlist.ID': identifier, 'Netflix.Watchlist.Count': str(count),
                            'Netflix.Watchlist.Action': 'open'})
        landscape = next((row.get('backdrop') for row in collection['items'] if row.get('backdrop')), '')
        portrait = next((row.get('poster') for row in collection['items'] if row.get('poster')), '')
        poster = browse_art(portrait, 'w780') or 'DefaultFolder.png'
        thumb = browse_art(landscape, 'w1280') or poster
        item.setArt({'thumb': thumb, 'poster': poster})
        item.addContextMenuItems([('Rename Watchlist', script('rename', identifier)),
                                  ('Delete Watchlist', script('delete', identifier))])
        rows.append((route('netflix_watchlist', list_id=identifier), item, True))
    # "New Watchlist" sits at the end of the My Lists rail, so it is never the first thing focused.
    create = xbmcgui.ListItem(label='New Watchlist')
    create.setProperties({'Netflix.Watchlist.Action': 'create', 'Netflix.Watchlist.Count': ''})
    create.setArt({'thumb': 'DefaultAddSource.png', 'poster': 'DefaultAddSource.png'})
    rows.append((route('netflix_watchlists', action='create'), create, True))
    return rows


def title_rows(collection, api):
    identifier = api.list_id(collection['id'])
    rows = []
    for stored in collection['items']:
        data = api.media_item(stored)  # Ignore any untrusted stored URL/action keys.
        kind, media_id = data['type'], data['id']
        item = xbmcgui.ListItem(label=data['title'], label2=data['meta'])
        tag = item.getVideoInfoTag()
        tag.setTitle(data['title'])
        tag.setPlot(data['plot'])
        tag.setMediaType(kind)
        tag.setUniqueIDs({'tmdb': media_id})
        # The version-1 snapshot keeps display metadata, not a second remote
        # metadata cache. Recover only recognizable year/runtime/rating tokens.
        for part in re.split(r'\s+·\s+', data['meta']):
            part = part.strip()
            if re.fullmatch(r'(?:18|19|20|21)\d{2}', part):
                tag.setYear(int(part))
            else:
                duration = re.fullmatch(r'(?:(\d{1,2})h)?\s*(?:(\d{1,3})m)?', part)
                if duration and any(duration.groups()):
                    tag.setDuration(int(duration[1] or 0) * 3600 + int(duration[2] or 0) * 60)
                elif re.fullmatch(r'(?:G|PG|PG-13|R|NC-17|NR|TV-Y|TV-Y7|TV-G|TV-PG|TV-14|TV-MA|U|12|12A|15|18)', part):
                    tag.setMpaa(part)
        if kind == 'tvshow':
            tag.setTvShowTitle(data['title'])
        item.setProperties({'tmdb_id': media_id, 'IsPlayable': 'true' if kind == 'movie' else 'false',
                            'Netflix.Watchlist.ID': identifier, 'Netflix.Watchlist.ItemID': media_id,
                            'Netflix.Watchlist.ItemType': kind, 'Netflix.Watchlist.Meta': data['meta']})
        poster = browse_art(data['poster'], 'w780')
        item.setArt({'poster': poster, 'thumb': poster, 'icon': poster,
                     'fanart': browse_art(data['backdrop'], 'w1280'),
                     'detail_fanart': data['backdrop'], 'clearlogo': data['logo']})
        item.addContextMenuItems([('Remove from Watchlist', script('remove', identifier, kind, media_id))])
        if kind == 'movie':
            path = route('play_media', mediatype='movie', tmdb_id=media_id)
        else:
            path = route('build_season_list', tmdb_id=media_id)
        rows.append((path, item, kind == 'tvshow'))
    return rows


def finish(handle, rows, name, content='files', success=True):
    xbmcplugin.setPluginCategory(handle, name)
    xbmcplugin.setContent(handle, content)
    if success:
        xbmcplugin.addDirectoryItems(handle, rows)
    xbmcplugin.endOfDirectory(handle, succeeded=success, cacheToDisc=False)


def collections(params):
    handle = int(kodi_utils.argv1())
    try:
        api = backend()
        store = api.default_store()
        if params.get('action') == 'create':
            api.create_watchlist(store)
        rows = collection_rows(store, api)
        xbmcgui.Window(10000).clearProperty('Netflix.Watchlists.Error')
        finish(handle, rows, 'Watchlists')
    except Exception:
        # Never replace or silently repair an invalid profile file.
        message = 'Could not read Watchlists. Your data was left unchanged.'
        xbmcgui.Window(10000).setProperty('Netflix.Watchlists.Error', message)
        xbmcgui.Dialog().notification('Watchlists', message)
        finish(handle, [], 'Watchlists', success=False)


def contents(params):
    handle = int(kodi_utils.argv1())
    try:
        api = backend()
        store = api.default_store()
        data = store.read()
        collection = api.Store.find(data, params.get('list_id'))
        identifier = api.list_id(collection['id'])
        home = xbmcgui.Window(10000)
        home.setProperty('Netflix.Watchlists.Selected', identifier)
        home.setProperty('Netflix.Watchlists.Return', 'true')
        xbmcplugin.setProperty(handle, 'Netflix.Watchlist.ID', identifier)
        xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Count', str(len(collection['items'])))
        finish(handle, title_rows(collection, api), collection['name'], content='movies')
    except Exception:
        xbmcgui.Dialog().notification('Watchlists', 'This Watchlist could not be opened. Your data was left unchanged.')
        finish(handle, [], 'Watchlists', content='movies', success=False)
