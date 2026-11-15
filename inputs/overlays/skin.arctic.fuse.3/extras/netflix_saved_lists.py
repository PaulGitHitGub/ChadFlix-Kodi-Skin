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

    def set_membership(self, item, wanted):
        """R10.18: the lists (by id) that hold `item` after this call are exactly `wanted`; one locked save, one revision bump."""
        item = media_item(item)
        wanted = {list_id(identifier) for identifier in wanted}
        def change(data):
            known = {c['id'] for c in data['lists']}
            if not wanted <= known:
                raise ListsError('This watchlist no longer exists.')
            for collection in data['lists']:
                has = any((i['type'], i['id']) == (item['type'], item['id']) for i in collection['items'])
                if collection['id'] in wanted and not has:
                    collection['items'].append(item)
                elif collection['id'] not in wanted and has:
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


# R10.18: the two buttons' labels. Title Case ("from" / "to" between words stay lower case, the last word is capitalised).
FAVORITE_LABEL, FAVORITE_ON_LABEL = 'Favorite', 'Remove from Favorites'
BOOKMARK_LABEL, BOOKMARK_MANY_LABEL = 'Add to Watchlist', 'Manage Watchlists'
NAME_PX = 300          # pixels (at the 30 px label font) a watchlist's name may take in "Remove from <name>": the label box is 560 px wide, "Remove from " is ~200
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


def holds(collection, item):
    return any((entry['type'], entry['id']) == (item['type'], item['id']) for entry in collection['items'])


def label_name(name):
    """The name as it appears after "Remove from ": whole when it fits NAME_PX, otherwise cut at a letter with an ellipsis."""
    if text_px(name) <= NAME_PX:
        return name
    keep = ''
    for char in name:
        if text_px(keep + char + '…') > NAME_PX:
            break
        keep += char
    return keep.rstrip() + '…'


def bookmark_label(holding):
    """The Watchlist button: none = Add to Watchlist; one list = Remove from <its name>; several = Manage Watchlists (a picker)."""
    if not holding:
        return BOOKMARK_LABEL
    if len(holding) == 1:
        return 'Remove from ' + label_name(holding[0]['name'])
    return BOOKMARK_MANY_LABEL


def saved_properties(item, store=None):
    with pov_api() as api:
        favorite = is_favorite(api, item)
    holding = [collection for collection in (store or default_store()).read()['lists'] if holds(collection, item)]
    return {'Favorite': 'true' if favorite else '', 'FavoriteLabel': FAVORITE_ON_LABEL if favorite else FAVORITE_LABEL,
            'Bookmarked': 'true' if holding else '', 'BookmarkLabel': bookmark_label(holding)}


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
    with pov_api() as api:
        favorite = is_favorite(api, item)
        action = api.remove_from_sync if favorite else api.add_to_sync
        if not action('favorites', item['type'], item['id'], item['title']):
            # A second press that raced the first (UNIQUE row): when the table is already in the state this press wanted, it is a success.
            if is_favorite(api, item) == favorite:
                raise ListsError('Could not update My Lists. Try again.')
    # Movies and shows have separate rows in My Lists; bump only the one that changed.
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
    store.remove(identifier, item)   # R10.18: straight away, no "Remove this title from the Watchlist?" question
    refresh_collection(identifier)


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
    if xbmcgui.getCurrentWindowDialogId() != DETAILS:
        return
    with one_at_a_time() as free:
        if free:
            item = current_movie()
            if bookmark_item(item, store):
                update_movie_state(item, store)   # R10.18: no "Added to Watchlist" notification


def another_movie(store=None):
    """R10.18: the Menu key / a long press of OK (userdata/keymaps/NetflixWatchlistMore.xml), only while the Watchlist button (18) has focus."""
    if xbmcgui.getCurrentWindowDialogId() != DETAILS or not xbmc.getCondVisibility('Control.HasFocus(18)'):
        return
    with one_at_a_time() as free:
        if free:
            item = current_movie()
            if more_watchlists(item, store):
                update_movie_state(item, store)


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
                  'bookmark-movie': bookmark_movie, 'another-movie': another_movie,
                  'restore-row': restore_collection_row}.get(command)
        if action:
            action()
    except (ListsError, OSError, RuntimeError, ImportError, KeyError) as error:
        notify(str(error) if isinstance(error, ListsError) else 'Could not load saved titles. Try again.')


if __name__ == '__main__' and len(sys.argv) >= 2:
    main(*sys.argv[1:])
