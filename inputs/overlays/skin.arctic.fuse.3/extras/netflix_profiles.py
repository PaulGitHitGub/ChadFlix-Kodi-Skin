"""Per-person profiles for the Arctic Fuse 3 Netflix-style skin (Kodi 21). STAGED, NOT INSTALLED.

Planned location: skin.arctic.fuse.3/extras/netflix_profiles.py, run by the skin as
    RunScript(special://skin/extras/netflix_profiles.py,list)
    RunScript(special://skin/extras/netflix_profiles.py,switch,<display name or Kodi name or index=N>)
    RunScript(special://skin/extras/netflix_profiles.py,add)
    RunScript(special://skin/extras/netflix_profiles.py,edit-open,index=N)   fills Edit.*, opens 1175
    RunScript(special://skin/extras/netflix_profiles.py,rename)              keyboard for Edit.KodiName
    RunScript(special://skin/extras/netflix_profiles.py,set-color,<id>)      colour for Edit.KodiName

Kodi 21 cannot create a profile at runtime (no JSON-RPC, builtin or Python API; profiles.xml is read
once at startup and rewritten from memory). So `make-pool` creates hidden spare profiles while Kodi
is stopped, and `add` claims one: it records the person's display name in the store, re-copies the
sign-ins from the main (master) profile, then switches. Kodi keeps calling that profile
"Profile N" until `make-profile sync-names` runs during a later planned stop.

Display-name store: special://masterprofile/netflix_profiles.json (no secrets), shared by all
profiles, written atomically under an flock:
    {"version": 1, "people": [
        {"dir": "",        "kodi": "Main",      "name": "Main"  , "state": "master"},
        {"dir": "spare-1", "kodi": "Profile 2", "name": "Alice",  "state": "claimed", "claimed_at": 1762786000},
        {"dir": "spare-2", "kodi": "Profile 3", "name": "",       "state": "free"}, ...]}
`kodi` is the name Kodi knows (LoadProfile uses it); `name` is what people see.

Home-window property contract for the future menu (window 10000). See PLAN.md section 7.
    Netflix.Profiles.Count            people to show (master + claimed), "1".."5"
    Netflix.Profiles.<i>.Name         display name, i = 1..Count, master first, then claim order
    Netflix.Profiles.<i>.Initial      first letter or digit, upper case ("?" if none)
    Netflix.Profiles.<i>.KodiName     name for LoadProfile / switch
    Netflix.Profiles.<i>.IsCurrent    "true" or ""
    Netflix.Profiles.<i>.IsMaster     "true" or ""
    Netflix.Profiles.<i>.Color        avatar colour as Kodi ARGB hex ("FF1D68B0"), for colordiffuse
    Netflix.Profiles.<i>.ColorId      palette id ("ocean")
    Netflix.Profiles.Current.Index    i of the current person ("" if unknown)
    Netflix.Profiles.Current.Name     display name of the current person
    Netflix.Profiles.Current.Initial / .Color / .ColorId
    Netflix.Profiles.Current.Pos      zero-based list position of the current person (for SetFocus)
    Netflix.Profiles.AddPos           zero-based position of the "+" tile, "" when nothing is free
    Netflix.Profiles.Edit.Index / .Name / .Initial / .Color / .ColorId / .ColorPos / .KodiName
                                      the person open in the Edit screen (set by edit-open)
    Netflix.Profiles.FreeSpares       unclaimed, unused spares
    Netflix.Profiles.CanAdd           "true" when FreeSpares > 0
    Netflix.Profiles.Max              people + free spares
    Netflix.Profiles.Error            "" or a short message for the menu to show
    Netflix.Profiles.Busy             "true" while add/switch runs
    Netflix.Profiles.Revision         changes on every list (use it to refresh a static list)
Indexes above Count (up to MAX_SLOTS) are cleared on every list.

Privacy: sign-in files are copied byte for byte and compared by sha256 in this process; their
contents are never logged or shown.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unicodedata

try:
    import xbmc
    import xbmcgui
    import xbmcvfs
except ImportError:  # unit tests inject stubs
    xbmc = xbmcgui = xbmcvfs = None

STORE = 'special://masterprofile/netflix_profiles.json'
PROP = 'Netflix.Profiles.'
MAX_SLOTS = 10
NAME_MAX = 20
FORBIDDEN = set(',()<>&"[]$|\\/')
RESERVED = {'master user'}

# Keep in step with the "signin" group in make-profile.
SIGNIN_FILES = (
    'addon_data/plugin.video.pov/settings.xml',
    'addon_data/plugin.video.themoviedb.helper/settings.xml',
    'addon_data/script.module.tmdbhelper.sync.mdblist/settings.xml',
    'addon_data/plugin.video.youtube/settings.xml',
    'addon_data/plugin.video.youtube/api_keys.json',
    'addon_data/plugin.video.youtube/access_manager.json',
)
# Present only once a profile has been loaded: a spare that has them is not "fresh".
USED_MARKERS = (
    'addon_data/plugin.video.pov/watched.db',
    'addon_data/skin.arctic.fuse.3/netflix_saved_lists.json',
)
SEEDED_MARKER = 'addon_data/skin.arctic.fuse.3/settings.xml'

# Original flat avatar colours (no Netflix artwork). White initials on each are >= 4.5:1.
# The order is the default assignment order; Custom_1176_Netflix_Profile_Icon.xml lists the same
# ids and hex values (a unit test checks they match).
PALETTE = (
    ('ocean', 'FF1D68B0'), ('ruby', 'FFC8343F'), ('moss', 'FF4E7D2A'), ('amber', 'FF9E6A06'),
    ('violet', 'FF7A3FB5'), ('teal', 'FF0F766E'), ('ember', 'FFC2541B'), ('orchid', 'FFA8327F'),
    ('indigo', 'FF4549C2'), ('slate', 'FF4F5B66'),
)
COLOR_HEX = dict(PALETTE)
COLOR_IDS = tuple(c for c, _ in PALETTE)
EDIT_WINDOW = 1175   # Custom_1175_Netflix_Profile_Edit.xml

# ---------------------------------------------------------------------------------------------
# Home-window state cleared before LoadProfile. Kodi's profile load does not clear window
# properties (CProfileManager::LoadProfile never calls ClearProperties; the skin's Home.xml says
# "Home properties survive skin reload"), so without this the new person inherits the old
# person's session state. Names come from reading the skin XML and extras/*.py (2026-11-10).
MOVIE_ROWS = (541, 542, 543, 544, 545, 546, 641, 643, 647, 648)   # netflix_browse_state.MOVIES
SHOW_ROWS = tuple(range(561, 571))                                # netflix_browse_state.SHOWS
SAVED_ROWS = (531, 532)                                           # My Lists rows (per person)
MODES = ('movies', 'shows', 'saved')


def stale_home_keys():
    keys = [
        # Boot gate: Home.xml raises Netflix.MoviesBoot.Pending only while SessionSeen is empty, so
        # clearing it makes the new profile get the same masked row warm-up as an app start.
        'Netflix.MoviesBoot.SessionSeen', 'Netflix.MoviesBoot.Pending',
        'Netflix.MoviesLanding.Pending', 'Netflix.HomeGenre.Loading',
        # Tab switching latches (Includes_Home.xml, Timers.xml).
        'Netflix.TabArrival', 'Netflix.TabDwellTarget', 'Netflix.TabDwellWindow', 'Netflix.TabDwellAuto',
        # Genre drawer state (Includes_Home.xml "drawer state lives on Home").
        'Netflix.GenreMenu.Open', 'Netflix.GenreMenu.Origin', 'Netflix.GenreMenu.Type',
        'Netflix.GenreMenu.Window', 'Netflix.GenreMenu.Action', 'Netflix.GenreMenu.Genre',
        'Netflix.GenreMenu.Grid', 'Netflix.GenreMenu.Title', 'Netflix.GenreMenu.Token',
        'Netflix.GenreMenu.PendingAction', 'Netflix.GenreMenu.PendingGrid',
        'Netflix.GenreMenu.PendingTitle', 'Netflix.GenreMenu.PendingToken',
        'Netflix.GenreMenu.LoadMore.Busy', 'Netflix.GenreMenu.LoadMore.Count',
        'Netflix.GenreMenu.LoadMore.Idx', 'Netflix.GenreMenu.LoadMore.Path',
        'Netflix.GenreMenu.LoadMore.Sent', 'Netflix.GenreTitle', 'Netflix.GenreActionToken',
        'Netflix.GenreIDToken',
        # Details page cache (netflix_movie_details.py / netflix_saved_lists.py PREFIX
        # "Netflix.Movie."): Bookmarked and Favorite belong to the previous person.
        'Netflix.Movie.ID', 'Netflix.Movie.Title', 'Netflix.Movie.Plot', 'Netflix.Movie.Meta',
        'Netflix.Movie.Poster', 'Netflix.Movie.Backdrop', 'Netflix.Movie.Logo', 'Netflix.Movie.Trailer',
        'Netflix.Movie.Action', 'Netflix.Movie.Origin', 'Netflix.Movie.FromPlaylist',
        'Netflix.Movie.Bookmarked', 'Netflix.Movie.BookmarkLabel', 'Netflix.Movie.Favorite',
        'Netflix.Movie.FavoriteLabel', 'Netflix.Movie.Remux', 'Netflix.Movie.QualityOpen',
        'Netflix.Movie.QualityState', 'Netflix.Movie.QualityToken', 'Netflix.Movie.QualityAllSpecs',
        'Netflix.PlaybackCover',
        # My Lists navigation (netflix_saved_lists.py restore_collection_row).
        'Netflix.Watchlists.Return', 'Netflix.Watchlists.Selected',
        # Voice search latches.
        'Netflix.SearchVoiceActive', 'Netflix.SearchVoiceArmed', 'Netflix.SearchVoicePending',
        # POV caches its settings JSON on Home; its service rebuilds it after the switch, but a
        # route run in between would otherwise use the previous profile's cached settings.
        'pov_settings', 'pov_kodi_menu_cache', 'pov_rli_fix',
        # TMDb Helper pause (also cleared on Videos, see below).
        'TMDbHelper.ServicePause',
    ]
    for tier in ('Highest', 'Medium', 'Lowest'):
        for field in ('Specs', 'Video', 'Audio'):
            keys.append('Netflix.Movie.Quality%s%s' % (tier, field))
    # Browse coordinator row state (netflix_browse_state.py). The previous person's rows are
    # gone after the skin reload; stale Completed/NonEmpty/EmptyConfirmed flags would make the
    # coordinator treat the new person's rows (My Lists in particular) as settled. Clearing them
    # is the same state an app start has; the coordinator then re-enables rows one at a time.
    # NOT cleared: Netflix.Browse.Running / Heartbeat / HeartbeatSeen and
    # Netflix.CatalogGenre.Loading (observer liveness and its provider lease). Clearing Running
    # while the old observer lives would start a second observer: its lock file is per profile.
    for row in MOVIE_ROWS + SHOW_ROWS + SAVED_ROWS:
        for name in ('Completed', 'NonEmpty', 'EmptyConfirmed', 'Failed', 'Retry', 'RetryAttempts'):
            keys.append('Netflix.Browse.%s.%d' % (name, row))
    for row in MOVIE_ROWS + SHOW_ROWS:
        keys.append('Netflix.CatalogGenre.Enabled.%d' % row)
    keys += ['Netflix.Browse.Enabled.531', 'Netflix.Browse.SavedShowsEnabled']
    rows = {'movies': MOVIE_ROWS, 'shows': SHOW_ROWS, 'saved': SAVED_ROWS}
    for mode in MODES:
        keys += ['Netflix.Browse.%s.Complete' % mode, 'Netflix.Browse.%s.Started' % mode,
                 'Netflix.Browse.%s.Row' % mode]
        keys += ['Netflix.Browse.%s.Index.%d' % (mode, row) for row in rows[mode]]
    return keys


# Window id -> keys, for properties that live on other windows.
STALE_OTHER = {10025: ('TMDbHelper.ServicePause',)}   # staged tmdbhelper-pause observer sets it on Videos
# Bumped (not cleared) so lists rebuild for the new person.
BUMP_HOME = ('Netflix.MyList.Revision', 'Netflix.Watchlists.Revision')


# ---------------------------------------------------------------------------------------------
# Pure logic (unit-tested with stub modules).

class ProfilesError(Exception):
    pass


def clean_name(raw):
    return ' '.join(unicodedata.normalize('NFC', raw or '').split())


def validate_name(raw, store, kodi_names=(), exclude=None):
    """Returns (name, '') or ('', reason). Same rules make-profile applies to Kodi names.
    `exclude` is the person being renamed: their own current names do not count as taken."""
    name = clean_name(raw)
    if not name:
        return '', 'Enter a name.'
    if len(name) > NAME_MAX:
        return '', 'Use %d characters or fewer.' % NAME_MAX
    if any(c in FORBIDDEN or unicodedata.category(c).startswith('C') for c in name):
        return '', 'Letters, numbers, spaces and . \' - only.'
    own = {exclude['kodi'].casefold(), exclude.get('name', '').casefold()} if exclude else set()
    taken = {n.casefold() for n in RESERVED}
    taken |= {n.casefold() for n in kodi_names}
    for p in store['people']:
        if exclude is not None and p['kodi'] == exclude['kodi']:
            continue
        taken.add(p['kodi'].casefold())
        if p.get('name'):
            taken.add(p['name'].casefold())
    if name.casefold() in taken - own:
        return '', 'That name is already used.'
    return name, ''


def validate_store(data):
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('people'), list):
        raise ProfilesError('Profile list is unreadable.')
    seen_dirs, seen_kodi = set(), set()
    for p in data['people']:
        if not isinstance(p, dict) or p.get('state') not in ('master', 'claimed', 'free'):
            raise ProfilesError('Profile list is unreadable.')
        for key in ('dir', 'kodi', 'name'):
            if not isinstance(p.get(key), str):
                raise ProfilesError('Profile list is unreadable.')
        if p['state'] == 'master' and p['dir'] != '':
            raise ProfilesError('Profile list is unreadable.')
        if p['state'] != 'master' and (not p['dir'] or p['dir'] in seen_dirs):
            raise ProfilesError('Profile list is unreadable.')
        if 'color' in p and p['color'] not in COLOR_HEX:
            raise ProfilesError('Profile list is unreadable.')
        if p['kodi'].casefold() in seen_kodi:
            raise ProfilesError('Profile list is unreadable.')
        seen_dirs.add(p['dir'])
        seen_kodi.add(p['kodi'].casefold())
    if [p['state'] for p in data['people']].count('master') != 1 or data['people'][0]['state'] != 'master':
        raise ProfilesError('Profile list is unreadable.')
    return data


def visible_people(store):
    master = [p for p in store['people'] if p['state'] == 'master']
    claimed = sorted((p for p in store['people'] if p['state'] == 'claimed'),
                     key=lambda p: (p.get('claimed_at', 0), p['dir']))
    return master + claimed


def display(p):
    return p['name'] or p['kodi']


def initial(name):
    for c in name:
        if c.isalnum():
            return c.upper()
    return '?'


def color_id(store, p):
    """The person's chosen colour, else a default from their position in the store."""
    if p.get('color') in COLOR_HEX:
        return p['color']
    return COLOR_IDS[store['people'].index(p) % len(COLOR_IDS)]


def next_free_color(store):
    used = {color_id(store, p) for p in visible_people(store)}
    return next((c for c in COLOR_IDS if c not in used), COLOR_IDS[0])


def rename(store, kodi, raw, kodi_names=()):
    person = next((p for p in store['people'] if p['kodi'] == kodi and p['state'] != 'free'), None)
    if person is None:
        raise ProfilesError('That profile no longer exists.')
    name, reason = validate_name(raw, store, kodi_names, exclude=person)
    if not name:
        raise ProfilesError(reason)
    person['name'] = name
    return person


def set_color(store, kodi, cid):
    if cid not in COLOR_HEX:
        raise ProfilesError('Unknown colour.')
    person = next((p for p in store['people'] if p['kodi'] == kodi and p['state'] != 'free'), None)
    if person is None:
        raise ProfilesError('That profile no longer exists.')
    person['color'] = cid
    return person


def edit_properties(store, person):
    if person is None:
        return {PROP + 'Edit.' + k: '' for k in ('Index', 'Name', 'Initial', 'Color', 'ColorId', 'ColorPos', 'KodiName')}
    people = visible_people(store)
    cid = color_id(store, person)
    return {PROP + 'Edit.Index': str(people.index(person) + 1), PROP + 'Edit.Name': display(person),
            PROP + 'Edit.Initial': initial(display(person)), PROP + 'Edit.Color': COLOR_HEX[cid],
            PROP + 'Edit.ColorId': cid, PROP + 'Edit.ColorPos': str(COLOR_IDS.index(cid)),
            PROP + 'Edit.KodiName': person['kodi']}


def resolve(store, arg):
    arg = clean_name(arg)
    people = visible_people(store)
    if arg.startswith('index='):
        try:
            i = int(arg[6:])
        except ValueError:
            return None
        return people[i - 1] if 1 <= i <= len(people) else None
    for p in people:
        if arg.casefold() in (display(p).casefold(), p['kodi'].casefold()):
            return p
    return None


def current_person(store, profile_path, master_path, profile_name=''):
    """Match special://profile against the master folder or masterprofile/profiles/<dir>/."""
    norm = lambda s: os.path.normpath(s or '').rstrip(os.sep)
    for p in store['people']:
        path = master_path if p['state'] == 'master' else os.path.join(master_path, 'profiles', p['dir'])
        if norm(path) == norm(profile_path):
            return p
    for p in store['people']:
        if profile_name and p['kodi'].casefold() == profile_name.casefold():
            return p
    return None


def spare_usable(p, kodi_names, folder_files):
    """folder_files(rel) -> bool exists, for this spare's folder."""
    if p['state'] != 'free':
        return False
    if p['kodi'].casefold() not in {n.casefold() for n in kodi_names}:
        return False
    if not folder_files(SEEDED_MARKER):
        return False
    return not any(folder_files(rel) for rel in USED_MARKERS)


def list_properties(store, current, free_spares, error=''):
    people = visible_people(store)
    props = {PROP + 'Count': str(len(people)), PROP + 'FreeSpares': str(free_spares),
             PROP + 'CanAdd': 'true' if free_spares > 0 else '', PROP + 'Max': str(len(people) + free_spares),
             PROP + 'Error': error, PROP + 'Current.Index': '', PROP + 'Current.Name': '',
             PROP + 'Current.Initial': '', PROP + 'Current.Color': '', PROP + 'Current.ColorId': '',
             PROP + 'Current.Pos': '', PROP + 'Busy': '', PROP + 'AddPos': str(len(people)) if free_spares > 0 else ''}
    for i in range(1, MAX_SLOTS + 1):
        for field in ('Name', 'Initial', 'KodiName', 'IsCurrent', 'IsMaster', 'Color', 'ColorId'):
            props['%s%d.%s' % (PROP, i, field)] = ''
    for i, p in enumerate(people, 1):
        is_current = current is not None and p['kodi'] == current['kodi']
        props.update({'%s%d.Name' % (PROP, i): display(p), '%s%d.Initial' % (PROP, i): initial(display(p)),
                      '%s%d.KodiName' % (PROP, i): p['kodi'],
                      '%s%d.IsCurrent' % (PROP, i): 'true' if is_current else '',
                      '%s%d.IsMaster' % (PROP, i): 'true' if p['state'] == 'master' else '',
                      '%s%d.Color' % (PROP, i): COLOR_HEX[color_id(store, p)],
                      '%s%d.ColorId' % (PROP, i): color_id(store, p)})
        if is_current:
            props.update({PROP + 'Current.Index': str(i), PROP + 'Current.Pos': str(i - 1),
                          PROP + 'Current.Name': display(p), PROP + 'Current.Initial': initial(display(p)),
                          PROP + 'Current.Color': COLOR_HEX[color_id(store, p)],
                          PROP + 'Current.ColorId': color_id(store, p)})
    return props


def claim(store, name, chosen_dir, now):
    for p in store['people']:
        if p['dir'] == chosen_dir and p['state'] == 'free':
            p.update(state='claimed', name=name, claimed_at=int(now), color=next_free_color(store))
            return p
    raise ProfilesError('That profile slot is no longer free.')


# ---------------------------------------------------------------------------------------------
# Store I/O (atomic, locked). Never repairs or replaces an unreadable file.

class Store:
    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path, 'rb') as f:
                raw = f.read(65536)
        except FileNotFoundError:
            raise ProfilesError('Profiles are not set up on this device.')
        try:
            return validate_store(json.loads(raw.decode('utf-8')))
        except (ValueError, UnicodeError):
            raise ProfilesError('Profile list is unreadable.')

    def update(self, change):
        import fcntl
        with open(self.path + '.lock', 'a+b') as lock:
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ProfilesError('Profiles are busy. Try again.')
                    time.sleep(0.05)
            try:
                data = self.read()
                result = change(data)
                validate_store(data)
                raw = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True).encode('utf-8')
                fd, tmp = tempfile.mkstemp(prefix='.netflix-profiles-', dir=os.path.dirname(self.path))
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw + b'\n')
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def copy_signins(master_dir, spare_dir):
    """Copy the master's sign-in files into a spare that is not loaded. Returns copied count."""
    copied = 0
    for rel in SIGNIN_FILES:
        src = os.path.join(master_dir, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(spare_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.netflix-signin-', dir=os.path.dirname(dst))
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as inp:
            for block in iter(lambda: inp.read(65536), b''):
                out.write(block)
        if sha256(tmp) != sha256(src):
            os.replace(tmp, dst + '.failed-copy')  # keep evidence, never delete
            raise ProfilesError('Could not copy sign-ins.')
        os.replace(tmp, dst)
        copied += 1
    return copied


# ---------------------------------------------------------------------------------------------
# Kodi glue.

class Kodi:
    def __init__(self):
        self.home = xbmcgui.Window(10000)

    def path(self, special):
        return xbmcvfs.translatePath(special)

    def label(self, name):
        return xbmc.getInfoLabel(name)

    def visible(self, cond):
        return xbmc.getCondVisibility(cond)

    def kodi_profiles(self):
        reply = json.loads(xbmc.executeJSONRPC(json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'Profiles.GetProfiles'})))
        return [p['label'] for p in reply.get('result', {}).get('profiles', [])]

    def home_get(self, key):
        return self.home.getProperty(key)

    def set(self, props):
        for key, value in props.items():
            if value:
                self.home.setProperty(key, value)
            else:
                self.home.clearProperty(key)

    def clear_other(self, window_id, keys):
        try:
            window = xbmcgui.Window(window_id)
        except RuntimeError:
            return
        for key in keys:
            window.clearProperty(key)

    def keyboard(self, heading, default=''):
        return xbmcgui.Dialog().input(heading, defaultt=default, type=xbmcgui.INPUT_ALPHANUM)

    def notify(self, message):
        xbmcgui.Dialog().notification('Profiles', message, time=3000)

    def builtin(self, command):
        xbmc.executebuiltin(command)

    def log(self, message):
        xbmc.log('Netflix Profiles: ' + message, xbmc.LOGINFO)


class App:
    def __init__(self, kodi):
        self.kodi = kodi
        self.master_dir = kodi.path('special://masterprofile/')
        self.store = Store(kodi.path(STORE))

    def spare_dir(self, p):
        return os.path.join(self.master_dir, 'profiles', p['dir'])

    def free_spares(self, data, kodi_names):
        return [p for p in data['people']
                if spare_usable(p, kodi_names, lambda rel, p=p: os.path.exists(os.path.join(self.spare_dir(p), rel)))]

    def current(self, data):
        return current_person(data, self.kodi.path('special://profile/'), self.master_dir,
                              self.kodi.label('System.ProfileName'))

    def list(self, error=''):
        try:
            data = self.store.read()
            names = self.kodi.kodi_profiles()
            missing = [p['kodi'] for p in visible_people(data) if p['kodi'].casefold() not in {n.casefold() for n in names}]
            if missing and not error:
                error = 'A profile is missing from Kodi.'
            props = list_properties(data, self.current(data), len(self.free_spares(data, names)), error)
        except ProfilesError as e:
            props = list_properties({'people': []}, None, 0, str(e))
        props[PROP + 'Revision'] = str(time.time_ns())
        self.kodi.set(props)
        return props

    def switch(self, arg):
        try:
            data = self.store.read()
        except ProfilesError as e:
            self.kodi.notify(str(e)); return self.list(str(e))
        target = resolve(data, arg)
        if target is None:
            self.kodi.notify('No profile called %s.' % clean_name(arg)); return self.list()
        if target['kodi'].casefold() not in {n.casefold() for n in self.kodi.kodi_profiles()}:
            self.kodi.notify('That profile is missing from Kodi.'); return self.list()
        current = self.current(data)
        if current is not None and current['kodi'] == target['kodi']:
            self.kodi.builtin('Dialog.Close(all,true)'); return self.list()
        if self.kodi.visible('Player.HasMedia'):
            self.kodi.notify('Stop playback to switch profile.'); return self.list()
        self.kodi.set({PROP + 'Busy': 'true'})
        self.prepare_switch(data, target)
        self.kodi.log('switch to dir=%s' % (target['dir'] or 'master'))
        self.kodi.builtin('Dialog.Close(all,true)')
        self.kodi.builtin('LoadProfile(%s)' % target['kodi'])
        return target

    def prepare_switch(self, data, target):
        self.kodi.set({key: '' for key in stale_home_keys()})
        for window_id, keys in STALE_OTHER.items():
            self.kodi.clear_other(window_id, keys)
        stamp = str(time.time_ns())
        self.kodi.set({key: stamp for key in BUMP_HOME})
        # Pre-set the menu's view of the new person: the skin reloads before `list` can run again.
        props = list_properties(data, target, len(self.free_spares(data, self.kodi.kodi_profiles())))
        props[PROP + 'Busy'] = 'true'
        props[PROP + 'Revision'] = stamp
        self.kodi.set(props)

    def add(self):
        try:
            data = self.store.read()
            names = self.kodi.kodi_profiles()
        except ProfilesError as e:
            self.kodi.notify(str(e)); return self.list(str(e))
        if not self.free_spares(data, names):
            self.kodi.notify('No free profile slots.'); return self.list('No free profile slots.')
        raw = self.kodi.keyboard('Name for the new profile')
        if not raw:
            return self.list()
        name, reason = validate_name(raw, data, names)
        if not name:
            self.kodi.notify(reason); return self.list(reason)
        self.kodi.set({PROP + 'Busy': 'true'})
        try:
            def change(fresh):
                again, reason = validate_name(name, fresh, names)
                if not again:
                    raise ProfilesError(reason)
                free = self.free_spares(fresh, names)
                if not free:
                    raise ProfilesError('No free profile slots.')
                spare = free[0]
                copy_signins(self.master_dir, self.spare_dir(spare))   # before the claim is recorded
                return dict(claim(fresh, name, spare['dir'], time.time()))
            person = self.store.update(change)
        except (ProfilesError, OSError) as e:
            message = str(e) if isinstance(e, ProfilesError) else 'Could not add the profile.'
            self.kodi.set({PROP + 'Busy': ''})
            self.kodi.notify(message); return self.list(message)
        self.kodi.log('claimed dir=%s' % person['dir'])
        return self.switch(person['kodi'])


def _edit_target(app):
    data = app.store.read()
    kodi = app.kodi.home_get(PROP + 'Edit.KodiName')
    person = next((p for p in data['people'] if p['kodi'] == kodi and p['state'] != 'free'), None)
    if person is None:
        raise ProfilesError('That profile no longer exists.')
    return data, person


def edit_open(app, arg):
    try:
        data = app.store.read()
    except ProfilesError as e:
        app.kodi.notify(str(e)); return app.list(str(e))
    person = resolve(data, arg)
    if person is None:
        app.kodi.notify('No profile called %s.' % clean_name(arg)); return app.list()
    app.kodi.set(edit_properties(data, person))
    app.kodi.builtin('ActivateWindow(%d)' % EDIT_WINDOW)
    return person


def edit_rename(app):
    try:
        data, person = _edit_target(app)
        names = app.kodi.kodi_profiles()
        raw = app.kodi.keyboard('Profile name', display(person))
        if not raw or clean_name(raw) == display(person):
            return app.list()
        kodi = person['kodi']
        app.store.update(lambda fresh: dict(rename(fresh, kodi, raw, names)))
        data = app.store.read()
        person = next(p for p in data['people'] if p['kodi'] == kodi)
    except ProfilesError as e:
        app.kodi.notify(str(e)); return app.list(str(e))
    app.kodi.set(edit_properties(data, person))
    return app.list()


def edit_color(app, cid):
    try:
        data, person = _edit_target(app)
        kodi = person['kodi']
        app.store.update(lambda fresh: dict(set_color(fresh, kodi, cid)))
        data = app.store.read()
        person = next(p for p in data['people'] if p['kodi'] == kodi)
    except ProfilesError as e:
        app.kodi.notify(str(e)); return app.list(str(e))
    app.kodi.set(edit_properties(data, person))
    return app.list()


def main(argv, kodi=None):
    app = App(kodi or Kodi())
    command = argv[1] if len(argv) > 1 else 'list'
    if command == 'list':
        return app.list()
    if command == 'switch' and len(argv) > 2:
        return app.switch(','.join(argv[2:]))
    if command == 'add':
        return app.add()
    if command == 'edit-open' and len(argv) > 2:
        return edit_open(app, argv[2])
    if command == 'rename':
        return edit_rename(app)
    if command == 'set-color' and len(argv) > 2:
        return edit_color(app, argv[2])
    return app.list('Unknown profile command.')


if __name__ == '__main__':
    main(sys.argv)
