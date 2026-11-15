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

    def create(self, name):
        name = clean_text(name, 80, True)
        identifier = str(uuid.uuid4())
        def change(data):
            data['lists'].append({'id': identifier, 'name': name, 'items': []})
            return identifier
        return self.update(change)

    def rename(self, identifier, name):
        name = clean_text(name, 80, True)
        return self.update(lambda data: self.find(data, identifier).update(name=name))

    def delete(self, identifier):
        def change(data):
            collection = self.find(data, identifier)
            data['lists'].remove(collection)
        return self.update(change)

    def add(self, identifier, item):
        item = media_item(item)
        def change(data):
            items = self.find(data, identifier)['items']
            if not any((i['type'], i['id']) == (item['type'], item['id']) for i in items):
                items.append(item)
        return self.update(change)

    def remove(self, identifier, item):
        item = media_item(item)
        def change(data):
            collection = self.find(data, identifier)
            collection['items'] = [i for i in collection['items']
                                   if (i['type'], i['id']) != (item['type'], item['id'])]
        return self.update(change)


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


def refresh_collection(identifier, deleted=False):
    list_id(identifier)
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


def is_favorite(api, item):
    return any(str(entry['tmdb_id']) == item['id']
               for entry in api.local_favorites(None, item['type'], 'all'))


def saved_properties(item, store=None):
    with pov_api() as api:
        favorite = is_favorite(api, item)
    bookmarked = any(
        (entry['type'], entry['id']) == (item['type'], item['id'])
        for collection in (store or default_store()).read()['lists']
        for entry in collection['items']
    )
    return {'Favorite': 'true' if favorite else '', 'FavoriteLabel': 'Favorite',
            'Bookmarked': 'true' if bookmarked else '', 'BookmarkLabel': 'Add to Watchlist'}


def sync_movie(store=None):
    home = xbmcgui.Window(10000)
    for key in ('Favorite', 'Bookmarked'):
        home.setProperty(PREFIX + key, '')
    try:
        item = current_movie()
    except ListsError:
        return
    properties = saved_properties(item, store)
    if home.getProperty(PREFIX + 'ID') != item['id']:
        return  # A later details page owns the state after local I/O completes.
    for key, value in properties.items():
        home.setProperty(PREFIX + key, value)


def favorite_item(item):
    with pov_api() as api:
        favorite = is_favorite(api, item)
        kind = 'movie' if item['type'] == 'movie' else 'show'
        if favorite and not xbmcgui.Dialog().yesno('My Lists', f'Remove this {kind} from My Lists?'):
            return False
        action = api.remove_from_sync if favorite else api.add_to_sync
        if not action('favorites', item['type'], item['id'], item['title']):
            raise ListsError('Could not update My Lists. Try again.')
    # Movies and shows have separate rows in My Lists; bump only the one that changed.
    revision = 'Netflix.MyList.Revision' if item['type'] == 'movie' else 'Netflix.MyList.ShowsRevision'
    xbmcgui.Window(10000).setProperty(revision, str(time.time_ns()))
    return True


def favorite_movie(store=None):
    if xbmcgui.getCurrentWindowDialogId() == DETAILS and favorite_item(current_movie()):
        sync_movie(store)


def prompt_name(heading, name=''):
    # The keyboard owns its microphone button; no input mode is chosen here.
    return xbmcgui.Dialog().input(heading, defaultt=name).strip()


def create_watchlist(store=None):
    store = store or default_store()
    name = prompt_name('Create Watchlist')
    if not name:
        return None
    identifier = store.create(name)
    notify('Watchlist created')
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
    if xbmcgui.Dialog().yesno('Remove Title', 'Remove this title from the Watchlist?'):
        store.remove(identifier, item)
        refresh_collection(identifier)


def bookmark_item(item, store=None):
    store = store or default_store()
    collections = store.read()['lists']
    create_options = ['Create New Watchlist…']
    choice = xbmcgui.Dialog().select('Add to Watchlist', create_options + [c['name'] for c in collections])
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


def bookmark_movie(store=None):
    if xbmcgui.getCurrentWindowDialogId() == DETAILS and bookmark_item(current_movie(), store):
        sync_movie(store)
        notify('Added to Watchlist')


def main(command, *arguments):
    try:
        if command == 'create' and not arguments:
            return create_watchlist()
        if command in ('rename', 'delete') and len(arguments) == 1:
            return change_watchlist(command, arguments[0])
        if command == 'remove' and len(arguments) == 3:
            return remove_watchlist_item(*arguments)
        if arguments:
            return
        action = {'sync-movie': sync_movie, 'favorite-movie': favorite_movie,
                  'bookmark-movie': bookmark_movie,
                  'restore-row': restore_collection_row}.get(command)
        if action:
            action()
    except (ListsError, OSError, RuntimeError, ImportError, KeyError) as error:
        notify(str(error) if isinstance(error, ListsError) else 'Could not load saved titles. Try again.')


if __name__ == '__main__' and len(sys.argv) >= 2:
    main(*sys.argv[1:])
