"""Local named playlists and POV's official local favorites, without playback."""
import contextlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

VERSION = 1
MAX_BYTES = 2 * 1024 * 1024
MAX_LISTS, MAX_ITEMS, MAX_TOTAL = 100, 500, 2000
CARD_LISTS = 12          # R10.22: the My Lists cards row shows this many lists (the most recently changed), then "See All (N)"
PREFIX = 'Netflix.Movie.'
DETAILS = 11167


class ListsError(Exception):
    pass


def clean_text(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ListsError('Invalid watchlist text.')
    value = value.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    if any(ord(c) < 32 for c in value):
        raise ListsError('Invalid watchlist text.')
    value = value.strip()
    if required and not value:
        raise ListsError('Enter a name.')
    return value


def media_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,11}', value):
        raise ListsError('This title has no valid TMDb ID.')
    return value


def list_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ListsError('Invalid watchlist ID.') from None
    return value


def media_item(item):
    if not isinstance(item, dict) or item.get('type') not in ('movie', 'tvshow'):
        raise ListsError('Invalid watchlist title.')
    result = {'type': item['type'], 'id': media_id(item.get('id')),
              'title': clean_text(item.get('title'), 300, True)}
    for key, limit in (('plot', 4000), ('meta', 300), ('poster', 2048), ('backdrop', 2048), ('logo', 2048)):
        result[key] = clean_text(item.get(key, ''), limit)
    return result


def season_number(value):
    if type(value) is not int or not 0 <= value <= 999:
        raise ListsError('This episode has no valid season.')
    return value


def episode_number(value):
    if type(value) is not int or not 1 <= value <= 9999:
        raise ListsError('This episode has no valid number.')
    return value


def episode_item(item):
    """R10.22: one saved episode of a watchlist (the list's separate `episodes` array: never an entry of `items`, whose validator would reject the whole
    file after a rollback to R10.13 - R10.18). Key: (show_id, season, episode)."""
    if not isinstance(item, dict) or item.get('type', 'episode') != 'episode':
        raise ListsError('Invalid watchlist episode.')
    minutes = item.get('minutes', 0)
    if type(minutes) is not int or not 0 <= minutes <= 9999:
        raise ListsError('Invalid watchlist episode.')
    result = {'type': 'episode', 'show_id': media_id(item.get('show_id')), 'season': season_number(item.get('season')),
              'episode': episode_number(item.get('episode')), 'show_title': clean_text(item.get('show_title', ''), 300),
              'title': clean_text(item.get('title'), 300, True)}
    for key, limit in (('still', 2048), ('poster', 2048), ('plot', 600)):
        result[key] = clean_text(item.get(key, ''), limit)
    result['minutes'] = minutes
    return result


def episode_key(item):
    return (item['show_id'], item['season'], item['episode'])


def is_episode(item):
    return isinstance(item, dict) and item.get('type') == 'episode'


def any_item(item):
    """A movie / show (media_item) or an episode (episode_item), validated."""
    return episode_item(item) if is_episode(item) else media_item(item)


def episode_id(show_id, season, episode):
    """The favorites table's tmdb_id text for an episode: '<show>:<season>:<episode>' (POV's column is TEXT; every POV reader filters by db_type)."""
    return '%s:%d:%d' % (media_id(str(show_id)), season_number(season), episode_number(episode))


def touch(collection):
    """R10.22: when a list last changed (the cards row shows the 12 most recent). An extra key: the R10.13 validator ignores it and an old write keeps it."""
    collection['updated'] = int(time.time())


def recent(lists):
    """Newest change first; lists without a stamp (made before R10.22) follow in reverse file order (the last created first)."""
    stamped = [(collection.get('updated') if type(collection.get('updated')) is int else 0, index, collection) for index, collection in enumerate(lists)]
    return [entry[2] for entry in sorted(stamped, key=lambda entry: (entry[0], entry[1]), reverse=True)]


def validate(data):
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != VERSION:
        raise ListsError('Unsupported or damaged watchlist file. Your file was left unchanged.')
    lists = data.get('lists')
    if not isinstance(lists, list) or len(lists) > MAX_LISTS:
        raise ListsError('Invalid watchlist collection.')
    ids, names, total = set(), set(), 0
    for collection in lists:
        if not isinstance(collection, dict):
            raise ListsError('Invalid watchlist collection.')
        identifier = list_id(collection.get('id'))
        name = clean_text(collection.get('name'), 80, True)
        if identifier in ids or name.casefold() in names:
            raise ListsError('Duplicate watchlist.')
        ids.add(identifier); names.add(name.casefold())
        items = collection.get('items')
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise ListsError('Watchlist capacity exceeded.')
        total += len(items)
        if total > MAX_TOTAL:
            raise ListsError('Watchlist capacity exceeded.')
        seen = set()
        for item in items:
            safe = media_item(item)
            key = (safe['type'], safe['id'])
            if key in seen:
                raise ListsError('Duplicate watchlist title.')
            seen.add(key)
        episodes = collection.get('episodes', [])
        if not isinstance(episodes, list) or len(items) + len(episodes) > MAX_ITEMS:
            raise ListsError('Watchlist capacity exceeded.')
        total += len(episodes)
        if total > MAX_TOTAL:
            raise ListsError('Watchlist capacity exceeded.')
        shown = set()
        for episode in episodes:
            key = episode_key(episode_item(episode))
            if key in shown:
                raise ListsError('Duplicate watchlist episode.')
            shown.add(key)
    return data


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {'version': VERSION, 'lists': []}
        if len(raw) > MAX_BYTES:
            raise ListsError('Watchlist file is too large. Your file was left unchanged.')
        try:
            return validate(json.loads(raw))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ListsError('Could not read watchlists. Your file was left unchanged.') from error

    @contextlib.contextmanager
    def locked(self):
        # Shield/Android and the Mac preview both provide POSIX advisory locks.
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / (self.path.name + '.lock')).open('a+b') as lock:
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ListsError('Watchlists are busy. Try again.')
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def update(self, change):
        with self.locked():
            data = self.read()  # Never replace an unreadable or unsupported file.
            result = change(data)
            validate(data)
            raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(raw) > MAX_BYTES:
                raise ListsError('Watchlist capacity exceeded.')
            descriptor, temporary = tempfile.mkstemp(prefix='.netflix-lists-', dir=self.path.parent)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            bump_watchlists_revision()
            return result

    @staticmethod
    def find(data, identifier):
        list_id(identifier)
        collection = next((c for c in data['lists'] if c['id'] == identifier), None)
        if collection is None:
            raise ListsError('This watchlist no longer exists.')
        return collection

    def create(self, name, item=None):
        name = clean_text(name, 80, True)
        identifier = str(uuid.uuid4())
        def change(data):
            collection = {'id': identifier, 'name': name, 'items': [], 'updated': int(time.time())}
            if item is not None:
                put(collection, any_item(item))
            data['lists'].append(collection)
            return identifier
        return self.update(change)

    def rename(self, identifier, name):
        name = clean_text(name, 80, True)
        def change(data):
            collection = self.find(data, identifier)
            collection.update(name=name)
            touch(collection)
        return self.update(change)

    def delete(self, identifier):
        def change(data):
            collection = self.find(data, identifier)
            data['lists'].remove(collection)
        return self.update(change)

    def add(self, identifier, item):
        item = any_item(item)
        def change(data):
            collection = self.find(data, identifier)
            if not holds(collection, item):
                put(collection, item)
                touch(collection)
        return self.update(change)

    def remove(self, identifier, item):
        item = any_item(item)
        def change(data):
            collection = self.find(data, identifier)
            if holds(collection, item):
                take(collection, item)
                touch(collection)
        return self.update(change)

    def toggle(self, identifier, item):
        """Toggle one list under the same lock used for every saved-list write."""
        item = any_item(item)

        def change(data):
            collection = self.find(data, identifier)
            selected = not holds(collection, item)
            if selected:
                put(collection, item)
            else:
                take(collection, item)
            touch(collection)
            return selected, data['lists']

        return self.update(change)

    def set_membership(self, item, wanted):
        """R10.18: the lists (by id) that hold `item` after this call are exactly `wanted`; one locked save, one revision bump."""
        item = any_item(item)    # R10.22: a movie, a show or an episode (an episode goes into the list's own `episodes` array)
        wanted = {list_id(identifier) for identifier in wanted}
        def change(data):
            known = {c['id'] for c in data['lists']}
            if not wanted <= known:
                raise ListsError('This watchlist no longer exists.')
            for collection in data['lists']:
                has = holds(collection, item)
                if collection['id'] in wanted and not has:
                    put(collection, item)
                    touch(collection)
                elif collection['id'] not in wanted and has:
                    take(collection, item)
                    touch(collection)
        return self.update(change)


def holds(collection, item):
    """True when the list holds `item`: a movie / show in `items`, an episode in `episodes` (R10.22)."""
    if is_episode(item):
        return any(episode_key(entry) == episode_key(item) for entry in collection.get('episodes', ()))
    return any((entry['type'], entry['id']) == (item['type'], item['id']) for entry in collection['items'])


def put(collection, item):
    if is_episode(item):
        collection.setdefault('episodes', []).append(item)
    else:
        collection['items'].append(item)


def take(collection, item):
    if is_episode(item):
        collection['episodes'] = [entry for entry in collection.get('episodes', ()) if episode_key(entry) != episode_key(item)]
    else:
        collection['items'] = [entry for entry in collection['items'] if (entry['type'], entry['id']) != (item['type'], item['id'])]


def notify(message):
    xbmcgui.Dialog().notification('Watchlists', message)


def default_store():
    profile = xbmcaddon.Addon(xbmc.getSkinDir()).getAddonInfo('profile')
    return Store(Path(xbmcvfs.translatePath(profile)) / 'netflix_saved_lists.json')


def bump_watchlists_revision():
    # A stable collection URL uses this revision only to refresh the local row.
    # Storage remains version 1 and never rewrites or migrates existing records.
    try:
        xbmcgui.Window(10000).setProperty('Netflix.Watchlists.Revision', str(time.time_ns()))
    except RuntimeError:
        pass  # A successful atomic save must not fail because a window closed.


VIEW = 'Netflix.MyLists.View'              # R10.22: '' (the page) | list (container 534: one list open) | all (container 535: See All)
VIEW_FROM, VIEW_ID, VIEW_TOKEN = 'Netflix.MyLists.ViewFrom', 'Netflix.MyLists.ViewID', 'Netflix.MyLists.ViewToken'
ALL_TOKEN = 'Netflix.MyLists.AllToken'      # See All's own token: only create from See All bumps it (a list's reload must not reload the hidden See All grid too: two calls)
VIEW_PATH, VIEW_ERROR = 'Netflix.MyLists.ViewPath', 'Netflix.MyLists.ViewError'
VIEW_INDEX = 'Netflix.MyLists.ViewIndex'
VIEW_GATES = ('Netflix.MyLists.Go.list', 'Netflix.MyLists.Go.all', 'Netflix.MyLists.Loaded.list', 'Netflix.MyLists.Loaded.all')   # what the tab's Back handler clears too
MY_LISTS = 11165


def close_view():
    """Back to the cards row (focus on card 0: the open list was deleted). The same steps as the tab's Back handler in Includes_Home.xml."""
    xbmc.executebuiltin('ClearProperty(%s,Home)' % VIEW)
    for key in (VIEW_FROM, VIEW_ID, VIEW_PATH, VIEW_ERROR) + VIEW_GATES:
        xbmc.executebuiltin('ClearProperty(%s,Home)' % key)
    xbmc.executebuiltin('SetFocus(533,0,absolute)')


def refresh_open_view(identifier, deleted=False, item_removed=False):
    """Refill the current list through its existing serial view loader."""
    home = xbmcgui.Window(10000)
    if home.getProperty(VIEW) != 'list' or home.getProperty(VIEW_ID) != identifier:
        return False
    if deleted:
        close_view()
        return True

    has_focus = xbmc.getInfoLabel('System.CurrentControlID') == '534'
    if has_focus:
        index = xbmc.getInfoLabel('Container(534).CurrentItem')
        count = xbmc.getInfoLabel('Container(534).NumItems')
        if index.isdecimal():
            position = max(0, int(index) - 1)
            if item_removed and count.isdecimal():
                position = min(position, max(0, int(count) - 2))
            home.setProperty(VIEW_INDEX, str(position))
    home.clearProperty('Netflix.MyLists.Go.list')
    home.clearProperty('Netflix.MyLists.Loaded.list')
    if has_focus:
        # The existing timer restores 534 when the new answer is available.
        xbmc.executebuiltin('SetFocus(8534)', True)
    return True


def refresh_collection(identifier, deleted=False, item_removed=False):
    list_id(identifier)
    if xbmcgui.getCurrentWindowId() == MY_LISTS and refresh_open_view(identifier, deleted, item_removed):
        return
    try:
        path = urlsplit(xbmc.getInfoLabel('Container.FolderPath'))
        params = parse_qs(path.query)
        matches = (path.scheme == 'plugin' and path.netloc == 'plugin.video.pov'
                   and params.get('mode') == ['netflix_watchlist']
                   and params.get('list_id') == [identifier])
    except ValueError:
        return
    if not matches or xbmcgui.getCurrentWindowDialogId() == DETAILS:
        return
    if deleted:
        xbmc.executebuiltin('ActivateWindow(1165)')
    else:
        xbmc.executebuiltin('Container.Refresh')


def restore_collection_row(store=None):
    home = xbmcgui.Window(10000)
    if home.getProperty('Netflix.Watchlists.Return') != 'true':
        return
    try:
        selected = list_id(home.getProperty('Netflix.Watchlists.Selected'))
        collections = (store or default_store()).read()['lists']
        # A deleted watchlist lands on the rail's first item. Otherwise find the row by its ID, so the
        # position of the "New Watchlist" item (last since the rail layout) does not matter.
        wanted = selected if any(collection['id'] == selected for collection in collections) else None
        monitor = xbmc.Monitor()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not monitor.waitForAbort(.05):
            if xbmcgui.getCurrentWindowId() != 11165:
                return
            focus = xbmc.getInfoLabel('System.CurrentControlID')
            if focus not in ('533', '3734', '8533', ''):
                return  # Do not take focus after the user chooses another row.
            if xbmc.getCondVisibility('Container(533).IsUpdating'):
                continue
            count = xbmc.getInfoLabel('Container(533).NumItems')
            if not count.isdecimal() or not int(count):
                continue
            index = 0 if wanted is None else next(
                (i for i in range(int(count)) if xbmc.getInfoLabel(
                    'Container(533).ListItemAbsolute(%d).Property(Netflix.Watchlist.ID)' % i) == wanted), None)
            if index is not None:
                xbmc.executebuiltin('SetFocus(533,%d,absolute)' % index)
                return
    finally:
        home.clearProperty('Netflix.Watchlists.Return')


def focus_card(identifier, container=533, seconds=8):
    """R10.22: put focus on the card / tile of list `identifier` once its container shows it (the card row refreshes after a change: the observer
    reloads it as soon as My Lists is quiet). Passive polling of info labels only; no add-on call. A key press elsewhere in the meantime wins."""
    monitor = xbmc.Monitor()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not monitor.waitForAbort(.1):
        if xbmcgui.getCurrentWindowId() != MY_LISTS:
            return False
        if xbmc.getInfoLabel('System.CurrentControlID') not in (str(container), str(container + 8000), '3734', '8533', '8535', ''):
            return False   # the owner moved on (a key press elsewhere wins): never take focus back (review F9)
        if xbmc.getCondVisibility('Container(%d).IsUpdating' % container):
            continue
        count = xbmc.getInfoLabel('Container(%d).NumItems' % container)
        if not count.isdecimal() or not int(count):
            continue
        index = next((i for i in range(int(count)) if xbmc.getInfoLabel(
            'Container(%d).ListItemAbsolute(%d).Property(Netflix.Watchlist.ID)' % (container, i)) == identifier), None)
        if index is not None:
            xbmc.executebuiltin('SetFocus(%d,%d,absolute)' % (container, index))
            return True
    return False


@contextlib.contextmanager
def pov_api():
    path = xbmcvfs.translatePath(xbmcaddon.Addon('plugin.video.pov').getAddonInfo('path'))
    library = str(Path(path) / 'resources/lib')
    sys.path.insert(0, library)
    try:
        from indexers import local_api
        yield local_api
    finally:
        sys.path.remove(library)


def current_movie():
    home = xbmcgui.Window(10000)
    return media_item({'type': 'movie', 'id': home.getProperty(PREFIX + 'ID'),
                       **{key.lower(): home.getProperty(PREFIX + key)
                          for key in ('Title', 'Plot', 'Meta', 'Poster', 'Backdrop', 'Logo')}})


def favorite_key(item):
    """(db_type, tmdb_id text, title column) of the favorites row for `item`. Movies and shows as always; an episode is db_type 'episode', id
    '<show>:<season>:<episode>' and a short JSON snapshot in the title column (so My Lists needs no TMDb call): {"s": show, "t": title, "i": still, "p": poster}."""
    if is_episode(item):
        return 'episode', episode_id(item['show_id'], item['season'], item['episode']), episode_snapshot(item)
    return item['type'], item['id'], item['title']


SNAPSHOT_MAX = 600


def episode_snapshot(item):
    fields = {'s': item.get('show_title', ''), 't': item.get('title', ''), 'i': item.get('still', ''), 'p': item.get('poster', '')}
    text = json.dumps(fields, ensure_ascii=False, separators=(',', ':'))
    while len(text) > SNAPSHOT_MAX:   # the long fields give way first: the poster, then the still, then the long texts
        for key in ('p', 'i'):
            if fields[key]:
                fields[key] = ''
                break
        else:
            fields['t'], fields['s'] = fields['t'][:max(0, len(fields['t']) - 40)], fields['s'][:max(0, len(fields['s']) - 40)]
        text = json.dumps(fields, ensure_ascii=False, separators=(',', ':'))
    return text


def parse_snapshot(text):
    """The snapshot back as a dict with all four keys (a damaged one reads as empty strings)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {key: data.get(key) if isinstance(data.get(key), str) else '' for key in ('s', 't', 'i', 'p')}


def parse_episode_id(text):
    """'<show>:<season>:<episode>' -> (show_id, season, episode) or None."""
    parts = str(text).split(':')
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    try:
        return media_id(parts[0]), season_number(int(parts[1])), episode_number(int(parts[2]))
    except ListsError:
        return None


def is_favorite(api, item):
    db_type, identifier, _ = favorite_key(item)
    return any(str(entry['tmdb_id']) == identifier for entry in api.local_favorites(None, db_type, 'all'))


# R10.18: the two buttons' labels. Use Title Case for the actions.
FAVORITE_LABEL, FAVORITE_ON_LABEL = 'Favorite', 'Remove From Favorites'
BOOKMARK_LABEL, BOOKMARK_MANY_LABEL = 'Add To Watchlist', 'Manage Watchlists'
NAME_PX = 300          # pixels (at the 30 px label font) a watchlist's name may take in "Remove From <name>": the label box is 560 px wide
_NARROW, _SLIM = set("ilj.,'!:;|`"), set('ftrI()[]- ')
_WIDE = {'W': 1.0, 'M': 0.9, 'm': 0.9, 'w': 0.78, '@': 1.0, '%': 0.95}


def text_px(text, size=30):
    """Rough pixel width of `text` in the skin's label font (Inter-like proportions, calibrated on Mac renders: NOTES.md)."""
    total = 0.0
    for char in text:
        if char in _WIDE:
            total += _WIDE[char]
        elif char in _NARROW:
            total += 0.27
        elif char in _SLIM:
            total += 0.34
        elif char.isdigit():
            total += 0.62
        elif char.isupper():
            total += 0.72
        else:
            total += 0.58
    return total * size


def label_name(name):
    """The name as it appears after "Remove From ": whole when it fits NAME_PX, otherwise cut at a letter with an ellipsis."""
    if text_px(name) <= NAME_PX:
        return name
    keep = ''
    for char in name:
        if text_px(keep + char + '…') > NAME_PX:
            break
        keep += char
    return keep.rstrip() + '…'


def bookmark_label(holding):
    """The Watchlist button: none = Add To Watchlist; one list = Remove From <its name>; several = Manage Watchlists (a picker)."""
    if not holding:
        return BOOKMARK_LABEL
    if len(holding) == 1:
        return 'Remove From ' + label_name(holding[0]['name'])
    return BOOKMARK_MANY_LABEL


# R10.22 (design B, Save Lane): the episode lane's labels. Same states and verbs as the show page; "Episode" says what the press saves.
EPISODE_FAVORITE_LABEL, EPISODE_BOOKMARK_LABEL = 'Favorite Episode', 'Add Episode to Watchlist'


def episode_bookmark_label(holding):
    if not holding:
        return EPISODE_BOOKMARK_LABEL
    if len(holding) == 1:
        return 'Remove From ' + label_name(holding[0]['name'])
    return BOOKMARK_MANY_LABEL


def episode_properties(favorite, holding):
    """The four ListItem properties of an episode row of the show page's episode list (the lane's icons and labels)."""
    return {'Fav': 'true' if favorite else '', 'FavLabel': FAVORITE_ON_LABEL if favorite else EPISODE_FAVORITE_LABEL,
            'Bm': 'true' if holding else '', 'BmLabel': episode_bookmark_label(holding)}


def episode_states(show_id, store=None):
    """One favorites SELECT and one JSON read for the whole show: ({(season, episode)} favorites, {(season, episode): [lists holding it]}).
    Called once per season refresh of the episode list, never per key press."""
    show_id = media_id(str(show_id))
    with pov_api() as api:
        rows = api.local_favorites(None, 'episode', 'all')
    favorites = set()
    for row in rows:
        parsed = parse_episode_id(row['tmdb_id'])
        if parsed and parsed[0] == show_id:
            favorites.add(parsed[1:])
    holding = {}
    for collection in (store or default_store()).read()['lists']:
        for entry in collection.get('episodes', ()):
            if entry['show_id'] == show_id:
                holding.setdefault((entry['season'], entry['episode']), []).append(collection)
    return favorites, holding


def episode_saved_count(show_id, store=None):
    """How many distinct episodes of the show are saved (a favorite, or on a watchlist): the show page's "N Saved"."""
    favorites, holding = episode_states(show_id, store)
    return len(favorites | set(holding))


def bookmark_properties(item, collections, holding):
    label = bookmark_label(holding)
    if holding and item['type'] == 'movie' and len(collections) > 1:
        count = len(holding)
        label = 'Watchlist · In %d %s' % (count, 'List' if count == 1 else 'Lists')
    return {'Bookmarked': 'true' if holding else '', 'BookmarkLabel': label}


def saved_properties(item, store=None):
    with pov_api() as api:
        favorite = is_favorite(api, item)
    collections = (store or default_store()).read()['lists']
    holding = [collection for collection in collections if holds(collection, item)]
    if is_episode(item):
        return episode_properties(favorite, holding)
    return {'Favorite': 'true' if favorite else '', 'FavoriteLabel': FAVORITE_ON_LABEL if favorite else FAVORITE_LABEL,
            **bookmark_properties(item, collections, holding)}


def sync_movie(store=None):
    home = xbmcgui.Window(10000)
    for key in ('Favorite', 'Bookmarked'):
        home.setProperty(PREFIX + key, '')
    try:
        item = current_movie()
    except ListsError:
        return
    claim = home.getProperty(BUSY)
    properties = saved_properties(item, store)
    if home.getProperty(PREFIX + 'ID') != item['id']:
        return  # A later details page owns the state after local I/O completes.
    if home.getProperty(BUSY) != claim:
        return  # R10.18: a Favorite / Watchlist click started or finished while this read ran: its state is the newer one
    for key, value in properties.items():
        home.setProperty(PREFIX + key, value)


def favorite_item(item):
    """R10.18: Favorite toggles. A favorite is removed at once (no confirmation), a new one is added; nothing is announced."""
    db_type, identifier, title = favorite_key(item)
    with pov_api() as api:
        favorite = is_favorite(api, item)
        action = api.remove_from_sync if favorite else api.add_to_sync
        if not action('favorites', db_type, identifier, title):
            # A second press that raced the first (UNIQUE row): when the table is already in the state this press wanted, it is a success.
            if is_favorite(api, item) == favorite:
                raise ListsError('Could not update My Lists. Try again.')
    # Movies and shows have separate rows in My Lists; bump only the one that changed.
    if is_episode(item):
        # R10.22: Saved Episodes is a card of the Watchlists row (container 533): that row's revision is the one that refreshes it
        bump_watchlists_revision()
        return True
    revision = 'Netflix.MyList.Revision' if item['type'] == 'movie' else 'Netflix.MyList.ShowsRevision'
    xbmcgui.Window(10000).setProperty(revision, str(time.time_ns()))
    return True


BUSY = 'Netflix.Saved.Busy'      # "run|<token>|<time>" while a click works, "done|<token>|<time>" after it
BUSY_SECONDS = 15           # a run claim older than this belongs to a killed script
COOLDOWN = 0.7              # seconds after a click finished in which further clicks (a held OK repeating, the OK that closed the picker) are refused
KEY_GUARD = 1.0             # ... and never less than this long after the click STARTED: a held key's repeats begin ~0.75 s after the press (Mac EventServer; a script that
                            # finishes in 0.1 s would otherwise let the first repeat through, measured), so the lock runs to start + KEY_GUARD
SETTLE = 0.05               # claim, wait, check: two scripts that start in the same instant cannot both pass (netflix_series.py stake / holds)


@contextlib.contextmanager
def one_at_a_time():
    """R10.18: yields True for one Favorite / Watchlist click at a time. False while another click runs, and for COOLDOWN seconds after one
    finished: every refused repeat renews the cooldown, so a held OK (the key repeats) is ONE toggle however long it is held, and the OK that
    picked a list in the picker cannot reach the button behind it. Claim-and-wait like the show page: write our token, wait SETTLE, go on only if it is still ours."""
    home = xbmcgui.Window(10000)
    state, _, rest = home.getProperty(BUSY).partition('|')
    owner, _, stamp = rest.partition('|')
    try:
        age = time.time() - float(stamp)
    except ValueError:
        age = None
    if state == 'run' and age is not None and 0 <= age < BUSY_SECONDS:
        yield False
        return
    if state == 'done' and age is not None and age < COOLDOWN:      # a negative age: the lock still runs to start + KEY_GUARD
        home.setProperty(BUSY, 'done|%s|%s' % (owner, max(float(stamp), time.time())))
        yield False
        return
    token = uuid.uuid4().hex
    started = time.time()
    home.setProperty(BUSY, 'run|%s|%s' % (token, started))
    time.sleep(SETTLE)
    if home.getProperty(BUSY).split('|')[1:2] != [token]:
        yield False
        return
    try:
        yield True
    finally:
        if home.getProperty(BUSY).split('|')[1:2] == [token]:
            home.setProperty(BUSY, 'done|%s|%s' % (token, max(time.time(), started + KEY_GUARD - COOLDOWN)))


def update_movie_state(item, store=None):
    """After a Favorite / Watchlist click: recompute both buttons from the sources of truth and write the page's four properties one after the other
    (no clearing first, so the icons and the labels change in place: a single frame can still show the new icon with the old label). A later page owns the state if the movie changed."""
    properties = saved_properties(item, store)
    home = xbmcgui.Window(10000)
    if home.getProperty(PREFIX + 'ID') != item['id']:
        return
    for key, value in properties.items():
        home.setProperty(PREFIX + key, value)


def favorite_movie(store=None):
    if xbmcgui.getCurrentWindowDialogId() != DETAILS:
        return
    with one_at_a_time() as free:
        if free:
            item = current_movie()
            if favorite_item(item):
                update_movie_state(item, store)


def prompt_name(heading, name=''):
    # The keyboard owns its microphone button; no input mode is chosen here.
    return xbmcgui.Dialog().input(heading, defaultt=name).strip()


def create_watchlist(store=None):
    store = store or default_store()
    name = prompt_name('Create Watchlist')
    if not name:
        return None
    identifier = store.create(name)   # R10.18: no "Watchlist created" notification (errors still notify)
    if xbmcgui.getCurrentWindowId() == MY_LISTS:
        # R10.22: the new list is the most recent, so the refreshed cards row lists it first: focus goes to its card (not opened).
        # From the See All grid the grid reloads by a new token and the focus goes to its tile.
        grid = xbmcgui.Window(10000).getProperty(VIEW) == 'all'
        if grid:
            xbmcgui.Window(10000).setProperty(ALL_TOKEN, str(time.time_ns()))
        focus_card(identifier, 535 if grid else 533)
    return identifier


def change_watchlist(command, identifier, store=None):
    store = store or default_store()
    identifier = list_id(identifier)
    collection = Store.find(store.read(), identifier)
    if command == 'rename':
        name = prompt_name('Rename Watchlist', collection['name'])
        if not name or name == collection['name']:
            return
        if not xbmcgui.Dialog().yesno('Rename Watchlist', 'Save this new name?'):
            return
        store.rename(identifier, name)
        home = xbmcgui.Window(10000)
        if home.getProperty(VIEW) == 'list' and home.getProperty(VIEW_ID) == identifier:
            home.setProperty('Netflix.MyLists.ViewName', name)   # R10.22: the open list's header shows the new name at once (the card row follows by its revision)
    elif command == 'delete':
        if not xbmcgui.Dialog().yesno('Delete Watchlist', 'Delete this Watchlist and its saved titles?'):
            return
        store.delete(identifier)
    else:
        return
    refresh_collection(identifier, deleted=command == 'delete')


def remove_watchlist_item(identifier, kind, item_id, store=None):
    identifier, item_id = list_id(identifier), media_id(item_id)
    if kind not in ('movie', 'tvshow'):
        raise ListsError('Invalid Watchlist title.')
    store = store or default_store()
    collection = Store.find(store.read(), identifier)
    item = next((i for i in collection['items'] if (i['type'], i['id']) == (kind, item_id)), None)
    if item is None:
        return
    store.remove(identifier, item)   # R10.18: straight away, no "Remove this title from the Watchlist?" question
    refresh_collection(identifier, item_removed=True)


def remove_watchlist_episode(identifier, show_id, season, episode, store=None):
    """R10.22: "Remove From Watchlist" on an episode card of an opened list. Straight away, like a title (R10.18: no questions)."""
    identifier = list_id(identifier)
    key = (media_id(show_id), season_number(int(season)), episode_number(int(episode)))
    store = store or default_store()
    collection = Store.find(store.read(), identifier)
    entry = next((e for e in collection.get('episodes', ()) if episode_key(e) == key), None)
    if entry is None:
        return
    store.remove(identifier, entry)
    refresh_collection(identifier, item_removed=True)


def unfavorite_episode(identifier):
    """R10.22: "Remove From Favorites" on a card of the opened Saved Episodes list."""
    parsed = parse_episode_id(identifier)
    if parsed is None:
        raise ListsError('Invalid episode.')
    with pov_api() as api:
        if not api.remove_from_sync('favorites', 'episode', episode_id(*parsed), ''):
            raise ListsError('Could not update My Lists. Try again.')
    bump_watchlists_revision()
    if xbmcgui.getCurrentWindowId() == MY_LISTS:
        refresh_open_view('episodes', item_removed=True)


def choose_watchlists(item, collections, holding, store, heading='Manage Watchlists'):
    """R10.18: the title is on several watchlists. The picker lists every watchlist with a check on those that hold it; OK saves
    the checks (adds to newly checked lists, removes from unchecked ones, in one save); Back changes nothing."""
    ids = [collection['id'] for collection in collections]
    before = {collection['id'] for collection in holding}
    chosen = xbmcgui.Dialog().multiselect(
        heading, [collection['name'] for collection in collections],
        preselect=[index for index, identifier in enumerate(ids) if identifier in before])
    if chosen is None:
        return False
    after = {ids[index] for index in chosen if 0 <= index < len(ids)}
    if after == before:
        return False
    store.set_membership(item, after)
    return True


def more_watchlists(item, store=None):
    """R10.18: "Add to Another Watchlist", the Watchlist button's second input (the Menu key, or a long press of OK). A title that is on one or more
    lists gets the picker with checks (every list, the holders checked); a title on no list gets the ordinary add picker. True when something changed."""
    store = store or default_store()
    collections = store.read()['lists']
    holding = [collection for collection in collections if holds(collection, item)]
    if not holding:
        return bookmark_item(item, store)
    return choose_watchlists(item, collections, holding, store,
                             'Add to Another Watchlist' if len(holding) == 1 else 'Manage Watchlists')


def bookmark_item(item, store=None):
    """R10.18: the Watchlist button. On no list: pick one (or create one) as before. On exactly one list: remove the title from it
    at once. On several: choose_watchlists(). True when something changed."""
    store = store or default_store()
    collections = store.read()['lists']
    holding = [collection for collection in collections if holds(collection, item)]
    if len(holding) == 1:
        store.remove(holding[0]['id'], item)
        return True
    if holding:
        return choose_watchlists(item, collections, holding, store)
    create_options = [CREATE_WATCHLIST_LABEL]
    choice = xbmcgui.Dialog().select(BOOKMARK_LABEL, create_options + [c['name'] for c in collections])
    if choice < 0:
        return False
    if choice < len(create_options):
        name = prompt_name('Create Watchlist')
        if not name:
            return False
        identifier = store.create(name)
    elif choice < len(create_options) + len(collections):
        identifier = collections[choice - len(create_options)]['id']
    else:
        return False
    store.add(identifier, item)
    return True


PICKER_LIST = 4100
PICKER_EDGE = 4101
PICKER_SURFACE = 4102
PICKER_ERROR = 4103
PICKER_ROW_HEIGHT = 62
PICKER_MAX_ROWS = 4
WATCHLIST_BUTTON = 18
CREATE_WATCHLIST_LABEL = 'Create New Watchlist…'
WATCHLIST_UPDATE_ERROR = 'Could Not Update Watchlist. Try Again.'


def update_movie_bookmark(item, collections):
    home = xbmcgui.Window(10000)
    if home.getProperty(PREFIX + 'ID') != item['id']:
        return
    holding = [collection for collection in collections if holds(collection, item)]
    for key, value in bookmark_properties(item, collections, holding).items():
        home.setProperty(PREFIX + key, value)


class WatchlistPicker(xbmcgui.WindowXMLDialog):
    def onInit(self):
        self.rows = self.getControl(PICKER_LIST)
        self.error = self.getControl(PICKER_ERROR)
        self.fill(0)
        self.setFocusId(PICKER_LIST)

    def fill(self, selected):
        items = []
        for collection in self.collections:
            entry = xbmcgui.ListItem(label=collection['name'])
            entry.setArt({'icon': 'special://skin/extras/icons/square-regular.png'})
            entry.select(holds(collection, self.item))
            items.append(entry)
        create = xbmcgui.ListItem(label=CREATE_WATCHLIST_LABEL)
        create.setArt({'icon': 'special://skin/extras/icons/circle-plus.png'})
        items.append(create)
        height = PICKER_ROW_HEIGHT * min(len(items), PICKER_MAX_ROWS)
        self.rows.setHeight(height)
        self.rows.reset()
        self.rows.addItems(items)
        self.rows.selectItem(selected)
        self.error.setPosition(209, 704 + height + 8)
        self.panel_height = 68 + height + 16
        self.show_error('')

    def show_error(self, message):
        self.error.setLabel(message)
        height = self.panel_height + (28 if message else 0)
        self.getControl(PICKER_EDGE).setHeight(height)
        self.getControl(PICKER_SURFACE).setHeight(height - 2)

    def onClick(self, control):
        if control != PICKER_LIST:
            return
        index = self.rows.getSelectedPosition()
        if not 0 <= index <= len(self.collections):
            return
        if xbmcgui.Window(10000).getProperty(PREFIX + 'ID') != self.item['id']:
            self.close()
            return
        try:
            if index == len(self.collections):
                name = prompt_name('Create Watchlist')
                if not name or xbmcgui.Window(10000).getProperty(PREFIX + 'ID') != self.item['id']:
                    return
                identifier = self.store.create(name, self.item)
                collections = self.collections = self.store.read()['lists']
                self.fill(next((i for i, entry in enumerate(collections) if entry['id'] == identifier), 0))
            else:
                selected, collections = self.store.toggle(self.collections[index]['id'], self.item)
                self.rows.getListItem(index).select(selected)
        except (ListsError, OSError):
            self.show_error(WATCHLIST_UPDATE_ERROR)
            return
        self.show_error('')
        update_movie_bookmark(self.item, collections)

    def onAction(self, action):
        if action.getId() in (9, 10, 92):  # ParentDir, PreviousMenu, NavBack.
            self.close()


def bookmark_movie(store=None, *, force_picker=False):
    if xbmcgui.getCurrentWindowDialogId() != DETAILS or not xbmc.getCondVisibility('Control.HasFocus(18)'):
        return
    with one_at_a_time() as free:
        if not free:
            return
        item = current_movie()
        store = store or default_store()
        try:
            collections = store.read()['lists']
            if not collections:
                if bookmark_item(item, store):
                    update_movie_bookmark(item, store.read()['lists'])
                return
            if len(collections) == 1 and not force_picker:
                _, collections = store.toggle(collections[0]['id'], item)
                update_movie_bookmark(item, collections)
                return
        except (ListsError, OSError):
            notify(WATCHLIST_UPDATE_ERROR)
            return
        picker = WatchlistPicker('Netflix_Watchlist_Picker.xml', 'special://skin', 'Default', '1080i')
        picker.store, picker.item, picker.collections = store, item, collections
        try:
            picker.doModal()
        finally:
            del picker
            if (xbmcgui.getCurrentWindowDialogId() == DETAILS
                    and xbmcgui.Window(10000).getProperty(PREFIX + 'ID') == item['id']):
                xbmc.executebuiltin('SetFocus(%d)' % WATCHLIST_BUTTON, True)


def main(command, *arguments):
    try:
        if command == 'create' and not arguments:
            return create_watchlist()
        if command in ('rename', 'delete') and len(arguments) == 1:
            return change_watchlist(command, arguments[0])
        if command == 'remove' and len(arguments) == 3:
            return remove_watchlist_item(*arguments)
        if command == 'remove-episode' and len(arguments) == 4:
            return remove_watchlist_episode(*arguments)
        if command == 'unfavorite-episode' and len(arguments) == 1:
            return unfavorite_episode(*arguments)
        if arguments:
            return
        action = {'sync-movie': sync_movie, 'favorite-movie': favorite_movie,
                  'bookmark-movie': bookmark_movie,
                  'another-movie': lambda: bookmark_movie(force_picker=True),
                  'restore-row': restore_collection_row}.get(command)
        if action:
            action()
    except (ListsError, OSError, RuntimeError, ImportError, KeyError) as error:
        notify(str(error) if isinstance(error, ListsError) else 'Could not load saved titles. Try again.')


if __name__ == '__main__' and len(sys.argv) >= 2:
    main(*sys.argv[1:])
