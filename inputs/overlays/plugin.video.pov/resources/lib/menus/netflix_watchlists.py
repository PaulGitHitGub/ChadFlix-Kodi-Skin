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


CARD_ART, STILL_ART = 'w185', 'w300'     # R10.22: collage pieces are small (up to 4 textures per card; TMDb poster w185, still w300); a one-title collage is the wide backdrop


def collage_art(collection, api):
    """Up to four pictures for a card's collage: the posters of its titles, then the stills of its episodes. A list of exactly one title shows its backdrop."""
    items, episodes = collection['items'], collection.get('episodes', ())
    if len(items) + len(episodes) == 1 and items and items[0].get('backdrop'):
        return [browse_art(items[0]['backdrop'], 'w780')]
    pictures = [browse_art(row.get('poster'), CARD_ART) for row in items if row.get('poster')]
    pictures += [browse_art(row.get('still') or row.get('poster'), CARD_ART) for row in episodes if row.get('still') or row.get('poster')]
    return [picture for picture in pictures if picture][:4]


def card_item(label, label2, identifier, count, action, pictures, kind):
    item = xbmcgui.ListItem(label=label, label2=label2)
    item.setProperties({'Netflix.Watchlist.ID': identifier, 'Netflix.Watchlist.Count': str(count),
                        'Netflix.Watchlist.Action': action, 'Netflix.Watchlist.Kind': kind,
                        'Netflix.Watchlist.Posters': str(len(pictures))})
    art = {'thumb': pictures[0] if pictures else 'DefaultFolder.png', 'poster': pictures[0] if pictures else 'DefaultFolder.png'}
    art.update({'collage%d' % (index + 1): picture for index, picture in enumerate(pictures)})
    item.setArt(art)
    return item


def saved_episode_rows(api):
    """The saved (favorite) episodes as POV's favorites table holds them: [(id text, (show, season, episode), snapshot dict)] in the order they were saved."""
    from indexers import local_api
    rows = []
    for entry in local_api.local_favorites(None, 'episode', 'all'):
        parsed = api.parse_episode_id(entry['tmdb_id'])
        if parsed:
            rows.append((entry['tmdb_id'], parsed, api.parse_snapshot(entry['title'])))
    return rows


def episodes_card(rows):
    pictures = [browse_art(snap['i'] or snap['p'], CARD_ART) for _, _, snap in rows if snap['i'] or snap['p']]
    pictures = [picture for picture in pictures if picture][:4]
    count = len(rows)
    item = card_item('Saved Episodes', '%d %s' % (count, 'episode' if count == 1 else 'episodes'), 'episodes', count, 'episodes', pictures, 'episodes')
    return (route('netflix_watchlist', kind='episodes'), item, True)


def collection_rows(store, api, everything=False):
    """R10.22. The cards row (container 533): Saved Episodes first when any episode is saved, the CARD_LISTS most recently changed lists, then
    "See All (N)" when there are more, then "New Watchlist" last. everything=True is the See All grid (container 535): every list, no See All card.
    Returns (rows, total lists)."""
    lists = api.recent(store.read()['lists'])
    rows = []
    episodes = saved_episode_rows(api)
    if episodes:
        rows.append(episodes_card(episodes))
    for collection in (lists if everything else lists[:api.CARD_LISTS]):
        identifier = api.list_id(collection['id'])
        count = len(collection['items']) + len(collection.get('episodes', ()))
        name = api.clean_text(collection['name'], 80, True)
        item = card_item(name, '%d %s' % (count, 'title' if count == 1 else 'titles'), identifier, count, 'open',
                         collage_art(collection, api), 'list')
        item.addContextMenuItems([('Rename Watchlist', script('rename', identifier)),
                                  ('Delete Watchlist', script('delete', identifier))])
        rows.append((route('netflix_watchlist', list_id=identifier), item, True))
    if not everything and len(lists) > api.CARD_LISTS:
        more = card_item('See All (%d)' % len(lists), '', 'all', len(lists), 'all', [], 'all')
        rows.append((route('netflix_watchlists', view='all'), more, True))
    # "New Watchlist" sits at the end of the cards row, so it is never the first thing focused.
    create = xbmcgui.ListItem(label='New Watchlist')
    create.setProperties({'Netflix.Watchlist.Action': 'create', 'Netflix.Watchlist.Count': '', 'Netflix.Watchlist.Kind': 'create'})
    create.setArt({'thumb': 'DefaultAddSource.png', 'poster': 'DefaultAddSource.png'})
    rows.append((route('netflix_watchlists', action='create'), create, True))
    for _, row, _ in rows:
        # a container's plugin properties are not readable by the skin: the list total rides on every item (Container(533).ListItemAbsolute(0).Property(...))
        row.setProperty('Netflix.Watchlists.Total', str(len(lists)))
    return rows, len(lists)


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
    for entry in collection.get('episodes', ()):
        rows.append(episode_row(api.episode_item(entry), identifier=identifier))   # R10.22: a list's saved episodes follow its titles
    return rows


def minutes_text(minutes):
    return '%dm' % minutes if minutes else ''


def episode_row(entry, favorite_id=None, identifier=''):
    """One saved episode as a grid cell (still on top, a panel with the show, "S1: E3" and the title below). `entry` has the shape of a watchlist's
    `episodes` item (a favorites snapshot is turned into it by saved_episode_entries)."""
    code = 'S%d: E%d' % (entry['season'], entry['episode'])
    item = xbmcgui.ListItem(label=entry['title'], label2=code)
    tag = item.getVideoInfoTag()
    tag.setTitle(entry['title'])
    tag.setMediaType('episode')
    tag.setTvShowTitle(entry['show_title'])
    tag.setSeason(entry['season'])
    tag.setEpisode(entry['episode'])
    if entry.get('plot'):
        tag.setPlot(entry['plot'])
    if entry.get('minutes'):
        tag.setDuration(entry['minutes'] * 60)
    item.setProperties({'tmdb_id': entry['show_id'], 'IsPlayable': 'false', 'Netflix.Episode': '1', 'Netflix.Episode.Code': code,
                        'Netflix.Episode.Show': entry['show_title'], 'Netflix.Episode.ShowID': entry['show_id'],
                        'Netflix.Episode.Season': str(entry['season']), 'Netflix.Episode.Number': str(entry['episode']),
                        'Netflix.Episode.Minutes': minutes_text(entry.get('minutes', 0)), 'Netflix.Watchlist.ID': identifier})
    still = browse_art(entry.get('still') or entry.get('poster'), STILL_ART)
    item.setArt({'thumb': still, 'icon': still, 'poster': still, 'landscape': still})
    if favorite_id:
        item.addContextMenuItems([('Remove from Favorites', script('unfavorite-episode', favorite_id))])
    else:
        item.addContextMenuItems([('Remove from Watchlist', script('remove-episode', identifier, entry['show_id'], str(entry['season']), str(entry['episode'])))])
    return (route('build_season_list', tmdb_id=entry['show_id']), item, True)


def saved_episode_entries(rows):
    entries = []
    for text, (show_id, season, number), snapshot in rows:
        entries.append((text, {'show_id': show_id, 'season': season, 'episode': number, 'show_title': snapshot['s'],
                               'title': snapshot['t'] or 'Episode %d' % number, 'still': snapshot['i'], 'poster': snapshot['p'],
                               'plot': '', 'minutes': 0}))
    return entries


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
        everything = params.get('view') == 'all'
        rows, total = collection_rows(store, api, everything)
        xbmcgui.Window(10000).clearProperty('Netflix.Watchlists.Error')
        xbmcplugin.setProperty(handle, 'Netflix.Watchlists.Total', str(total))
        xbmcplugin.setProperty(handle, 'Netflix.Watchlists.View', 'all' if everything else 'cards')
        if everything:
            loaded_all(opened=params.get('netflix_open'))   # the See All grid's answer is in (the skin shows its tiles)
        finish(handle, rows, 'All Watchlists' if everything else 'Watchlists')
    except Exception:
        # Never replace or silently repair an invalid profile file.
        message = 'Could not read Watchlists. Your data was left unchanged.'
        xbmcgui.Window(10000).setProperty('Netflix.Watchlists.Error', message)
        xbmcgui.Dialog().notification('Watchlists', message)
        if params.get('view') == 'all':
            loaded_all(message, params.get('netflix_open'))     # the See All layer shows the message instead of flat cards forever (review F8)
        finish(handle, [], 'Watchlists', success=False)


def loaded(identifier, name='', error='', opened=None):
    """R10.22: the opened list's answer is in (or failed: `error`). Set just before the directory ends (Container.IsUpdating stays true until its items are in
    place), so the skin tells "still loading" (flat cards) from "loaded and empty" (the dashed hint, or the error text). Only for the list the owner is waiting
    for (Netflix.MyLists.ViewID, set at the click): the answer of a list he left meanwhile must not mark another one loaded (review F3). The header name is set
    here, by Python, so a name with commas or quotes never goes through a builtin's parameters (review F4)."""
    home = xbmcgui.Window(10000)
    if (not identifier or home.getProperty('Netflix.MyLists.ViewID') != identifier or home.getProperty('Netflix.MyLists.Go.list') != 'true'
            or (opened is not None and home.getProperty('Netflix.MyLists.OpenSeq.list') != opened)):
        return   # not the request the owner waits for, or the gate is closed (a quick Back + reopen: the old call's answer must not count; delta review D3)
    if name:
        home.setProperty('Netflix.MyLists.ViewName', name)
    if error:
        home.setProperty('Netflix.MyLists.ViewError', error)
    else:
        home.clearProperty('Netflix.MyLists.ViewError')
    home.setProperty('Netflix.MyLists.Loaded.list', 'true')


def loaded_all(error='', opened=None):
    home = xbmcgui.Window(10000)
    if (home.getProperty('Netflix.MyLists.View') != 'all' or home.getProperty('Netflix.MyLists.Go.all') != 'true'
            or (opened is not None and home.getProperty('Netflix.MyLists.OpenSeq.all') != opened)):
        return
    if error:
        home.setProperty('Netflix.MyLists.ViewError', error)
    else:
        home.clearProperty('Netflix.MyLists.ViewError')
    home.setProperty('Netflix.MyLists.Loaded.all', 'true')


def saved_episodes_view(handle, api, opened=None):
    entries = saved_episode_entries(saved_episode_rows(api))
    xbmcplugin.setProperty(handle, 'Netflix.Watchlist.ID', 'episodes')
    xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Name', 'Saved Episodes')
    xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Count', str(len(entries)))
    xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Kind', 'episodes')
    loaded('episodes', 'Saved Episodes', opened=opened)
    finish(handle, [episode_row(entry, favorite_id=text, identifier='episodes') for text, entry in entries], 'Saved Episodes', content='movies')


def contents(params):
    handle = int(kodi_utils.argv1())
    try:
        api = backend()
        if params.get('kind') == 'episodes':
            return saved_episodes_view(handle, api, params.get('netflix_open'))
        store = api.default_store()
        data = store.read()
        collection = api.Store.find(data, params.get('list_id'))
        identifier = api.list_id(collection['id'])
        if params.get('netflix_view') != 'page':
            # The Videos-window route of R10.13 (kept for any caller that still opens a list there): the old return-focus properties.
            # Inside My Lists (netflix_view=page, container 534) nothing needs restoring: the window is never left.
            home = xbmcgui.Window(10000)
            home.setProperty('Netflix.Watchlists.Selected', identifier)
            home.setProperty('Netflix.Watchlists.Return', 'true')
        count = len(collection['items']) + len(collection.get('episodes', ()))
        xbmcplugin.setProperty(handle, 'Netflix.Watchlist.ID', identifier)
        xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Name', api.clean_text(collection['name'], 80, True))
        xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Count', str(count))
        xbmcplugin.setProperty(handle, 'Netflix.Watchlist.Kind', 'list')
        rows = title_rows(collection, api)
        if params.get('netflix_view') == 'page':
            loaded(identifier, api.clean_text(collection['name'], 80, True), opened=params.get('netflix_open'))
        finish(handle, rows, collection['name'], content='movies')
    except Exception:
        message = 'This Watchlist could not be opened. Your data was left unchanged.'
        xbmcgui.Dialog().notification('Watchlists', message)
        if params.get('netflix_view') == 'page':
            loaded(params.get('list_id') or ('episodes' if params.get('kind') == 'episodes' else ''), error=message, opened=params.get('netflix_open'))   # the view shows the message (review F8)
        finish(handle, [], 'Watchlists', content='movies', success=False)
