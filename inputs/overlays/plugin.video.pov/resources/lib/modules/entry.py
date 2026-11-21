from datetime import datetime
from threading import Thread
from modules import kodi_utils, settings

logger, path_exists, translate_path = kodi_utils.logger, kodi_utils.path_exists, kodi_utils.translate_path
get_property, set_property, clear_property = kodi_utils.get_property, kodi_utils.set_property, kodi_utils.clear_property
get_setting, set_setting, make_settings_dict = kodi_utils.get_setting, kodi_utils.set_setting, kodi_utils.make_settings_dict

POV_ROUTES = {
	'smart_play_media': lambda p: _import('modules.episode_tools', 'SmartPlay')(p),
	'play_media': lambda p: _import('modules.sources', 'Sources').factory(p),
	'netflix_quality_preview': lambda p: _import('modules.netflix_quality_preview', 'prefetch')(p),
	'media_play': lambda p: _import('modules.debrid', 'play_from_cloud')(p),
	'downloader': lambda p: _import('modules.downloader', 'factory')(p),

	'scraper_color_choice': lambda p: _import('modules.dialogs', 'scraper_color_choice')(p['setting']),
	'scraper_dialog_color_choice': lambda p: _import('modules.dialogs', 'scraper_dialog_color_choice')(p['setting']),
	'scraper_quality_color_choice': lambda p: _import('modules.dialogs', 'scraper_quality_color_choice')(p['setting']),
	'set_quality_choice': lambda p: _import('modules.dialogs', 'set_quality_choice')(p['quality_setting']),
	'results_sorting_choice': lambda p: _import('modules.dialogs', 'results_sorting_choice')(),
	'options_menu_choice': lambda p: _import('modules.dialogs', 'options_menu')(p),
	'meta_language_choice': lambda p: _import('modules.dialogs', 'meta_language_choice')(),
	'favorites_choice': lambda p: _import('modules.dialogs', 'favorites_choice')(p),
	'dropped_choice': lambda p: _import('modules.dialogs', 'dropped_choice')(p),
	'set_language_filter_choice': lambda p: _import('modules.dialogs', 'set_language_filter_choice')(p['filter_setting']),
	'extras_lists_choice': lambda p: _import('modules.dialogs', 'extras_lists_choice')(),
	'random_choice': lambda p: _import('modules.dialogs', 'random_choice')(p['mode'], p),
	'extras_menu_choice': lambda p: _import('windows.extras', 'extras_menu')(p),
	'trakt_manager_choice': lambda p: _import('menus.trakt', 'TraktManager')(p).manage(),
	'mdbl_manager_choice': lambda p: _import('menus.mdblist', 'MdbListManager')(p).manage(),
	'tmdb_manager_choice': lambda p: _import('menus.tmdb', 'TmdbManager')(p).manage(),

	'netflix_watchlists': lambda p: _import('menus.netflix_watchlists', 'collections')(p),
	'netflix_watchlist': lambda p: _import('menus.netflix_watchlists', 'contents')(p),
	'netflix_continue_watching': lambda p: _import('menus.netflix_continue', 'continue_watching')(p),
	'build_movie_list': lambda p: _import('menus.movies', 'Menu')(p).run(),
	'build_tvshow_list': lambda p: _import('menus.tvshows', 'Menu')(p).run(),
	'build_season_list': lambda p: _import('menus.seasons', 'Seasons')(p).run(),
	'build_episode_list': lambda p: _import('menus.seasons', 'Episodes')(p).run(),
	'build_last_episode': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_in_progress_episode': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_next_episode': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_my_calendar_trakt': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_my_anime_calendar': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_my_calendar_mdbl': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_anime_calendar': lambda p: _import('menus.episodes', 'Menu')(p).run(),
	'build_navigate_to_page': lambda p: _import('modules.dialogs', 'build_navigate_to_page')(p),
	'build_popular_people': lambda p: _import('menus.people', 'popular_people')(),

	'open_settings': lambda p: _import('modules.kodi_utils', 'open_settings')(p.get('query')),
	'clean_settings': lambda p: _import('modules.kodi_utils', 'clean_settings')(),
	'clean_settings_window_properties': lambda p: _import('modules.kodi_utils', 'clean_settings_window_properties')(),
	'clear_all_cache': lambda p: _import('modules.dialogs', 'clear_all_cache')(),
	'clear_cache': lambda p: _import('modules.cache', 'clear_cache')(p.get('cache')),
	'clean_databases': lambda p: _import('modules.cache', 'clean_databases')(),
	'clear_streams': lambda p: _import('modules.tuneup', 'clear_streams')(),
	'clear_thumbnails': lambda p: _import('modules.tuneup', 'clear_thumbnails')(),

	'search_history': lambda p: _import('menus.history', 'search_history')(p),
	'clear_search_history': lambda p: _import('menus.history', 'clear_search_history')(p),
	'remove_from_history': lambda p: _import('menus.history', 'remove_from_search_history')(p),
	'discover_remove_from_history': lambda p: _import('menus.discover', 'remove_from_history')(p),
	'discover_remove_all_history': lambda p: _import('menus.discover', 'remove_all_history')(p),
	'get_search_term': lambda p: _import('menus.history', 'get_search_term')(p),
	'person_search': lambda p: _import('menus.people', 'person_search')(p['query']),
	'person_data_dialog': lambda p: _import('menus.people', 'person_data_dialog')(p),

	'mark_as_watched_unwatched_episode': lambda p: _import('caches.watched_cache', 'mark_as_watched_unwatched_episode')(p),
	'mark_as_watched_unwatched_season': lambda p: _import('caches.watched_cache', 'mark_as_watched_unwatched_season')(p),
	'mark_as_watched_unwatched_tvshow': lambda p: _import('caches.watched_cache', 'mark_as_watched_unwatched_tvshow')(p),
	'mark_as_watched_unwatched_movie': lambda p: _import('caches.watched_cache', 'mark_as_watched_unwatched_movie')(p),
	'watched_unwatched_erase_bookmark': lambda p: _import('caches.watched_cache', 'erase_bookmark')(
		p.get('mediatype'), p.get('tmdb_id'), p.get('season', ''), p.get('episode', ''), p.get('refresh', 'false')
	),

	'choose_view': lambda p: _import('modules.kodi_utils', 'choose_view')(p['view_type'], p.get('content', '')),
	'set_view': lambda p: _import('modules.kodi_utils', 'set_view')(p['view_type']),
	'clear_view_modes': lambda p: _import('modules.kodi_utils', 'clear_view_modes')(),
	'show_text': lambda p: _import('modules.kodi_utils', 'show_text')(
		p.get('heading'), p.get('text'), p.get('file'), p.get('font_size', 'small'), p.get('kodi_log', 'false') == 'true'
	),

	'toggle_provider': lambda p: _import('modules.utils', 'toggle_provider')(),
	'toggle_language_invoker': lambda p: _import('modules.kodi_utils', 'toggle_language_invoker')(),
	'upload_logfile': lambda p: _import('modules.kodi_utils', 'upload_logfile')(),
	'myservices': lambda p: _import('modules.myservices', 'authorize')(),
	'refer_link': lambda p: _import('modules.myservices', 'refer_link')(p['query']),
	'undesirablesSelect': lambda p: _import('indexers.pov_api', 'undesirablesSelect')(),
	'undesirablesInput': lambda p: _import('indexers.pov_api', 'undesirablesInput')(),
	'undesirablesUserRemove': lambda p: _import('indexers.pov_api', 'undesirablesUserRemove')(),
	'healthTest': lambda p: _import('indexers.pov_api', 'health')(),
	'aioHelp': lambda p: _import('debrids.aiostreams', 'aio_help')(),
}

def _import(module_path, attr_name):
	mod = __import__(module_path, fromlist=[attr_name])
	return getattr(mod, attr_name)

def _run_class_method(cls_module, cls_name, params, mode):
	cls = _import(cls_module, cls_name)
	method_name = mode.split('.')[-1]
	method = getattr(cls(params), method_name, None)
	if callable(method): return method()

def _run_debrid_method(cls_module, cls_name, params, mode):
	cls = _import(cls_module, cls_name)
	method_name = mode.split('.')[-1]
	method = getattr(cls(), method_name, None)
	if callable(method): return method(params)

def _run_dynamic_func(module_path, mode, params):
	from modules.utils import manual_function_import
	func_name = mode.split('.')[-1]
	function = manual_function_import(module_path, func_name)
	return function(params)

def _open_native_movie_card(params, handle):
	# The native Videos view consumes Select before skin list actions can run.
	if (params.get('mediatype') != 'movie'
			or set(params) != {'mode', 'mediatype', 'tmdb_id'}
			or kodi_utils.current_skin() != 'skin.arctic.fuse.3'):
		return False
	from xbmcgui import getCurrentWindowId, ListItem
	if getCurrentWindowId() != 10025: return False
	control = kodi_utils.get_infolabel('System.CurrentControlID')
	# sweep 2 #1 (R10.55.4): a Back or arrow that lands before this runs has moved focus off 590/591 or to another title. If
	# the selected title of 590 or 591 is still this movie, the request came from an OK on it: swallow it (never play), and
	# open the page only while focus is still on that title. Back on the row also moves the row back to its first title and
	# Left/Right select another one, so when the selected title is not this movie any title of 590/591 counts (swallowed,
	# no page).
	def is_this(prefix):
		path = kodi_utils.get_infolabel(prefix + 'FileNameAndPath')
		return (kodi_utils.get_infolabel(prefix + 'DBType') == 'movie' and path.startswith('plugin://plugin.video.pov/')
				and kodi_utils.parsed_query(path) == params)
	for listed in ('590', '591'):
		selected = is_this('Container(%s).ListItem.' % listed)
		found = selected
		if not found:
			try: count = min(int(kodi_utils.get_infolabel('Container(%s).NumAllItems' % listed) or 0), 60)
			except ValueError: count = 0
			found = any(is_this('Container(%s).ListItemAbsolute(%d).' % (listed, i)) for i in range(count))
		if not found: continue
		if handle >= 0:
			from xbmcplugin import setResolvedUrl
			setResolvedUrl(handle, False, ListItem())
		if control == listed and selected:
			kodi_utils.execute_builtin('RunScript(special://skin/extras/netflix_movie_details.py,%s)' % control)
		else:
			logger('POV', 'ChadFlix: OK on a movie ignored, focus moved to %s first' % control)
		return True
	return False

def routing(sys_obj):
	params = kodi_utils.parsed_query(sys_obj.argv[2])
	mode = params.get('mode', 'navigator.main')

	if mode == 'play_media' and _open_native_movie_card(params, int(sys_obj.argv[1])): return
	if mode in POV_ROUTES: return POV_ROUTES[mode](params)

	if mode.startswith('navigator.'): return _run_class_method('menus.navigator', 'Navigator', params, mode)

	if mode.startswith('discover.'): return _run_class_method('menus.discover', 'Discover', params, mode)

	if mode.startswith('menu_editor.'): return _run_class_method('modules.menu_editor', 'MenuEditor', params, mode)

	if '_image' in mode: return _import('menus.images', 'Images')().run(params)

	if mode.startswith('trakt.'):
		if mode == 'trakt.trakt_account_info': return _import('menus.trakt', 'trakt_account_info')()
		return _run_dynamic_func('indexers.trakt_api', mode, params)

	if mode.startswith('mdblist.'):
		if mode == 'mdblist.mdbl_account_info': return _import('menus.mdblist', 'mdbl_account_info')()
		return _run_dynamic_func('indexers.mdblist_api', mode, params)

	if mode.startswith('tmdb.'):
		if mode == 'tmdb.edit_tmdb_list': return _import('menus.tmdb', 'edit_tmdb_list')(params)
		if mode == 'tmdb.update_tmdb_list': return _import('menus.tmdb', 'update_tmdb_list')(params)
		return _run_dynamic_func('indexers.tmdb_api', mode, params)

	if mode.startswith('build_'):
		if mode.startswith('build_trakt_'): return _run_dynamic_func('menus.trakt', mode, params)
		if mode.startswith('build_mdbl_'): return _run_dynamic_func('menus.mdblist', mode, params)
		if mode.startswith('build_tmdb_'): return _run_dynamic_func('menus.tmdb', mode, params)

	if mode.startswith('premiumize.'): return _run_debrid_method('debrids.pm_cloud', 'Menu', params, 'run')
	if mode.startswith('offcloud.'): return _run_debrid_method('debrids.oc_cloud', 'Menu', params, 'run')
	if mode.startswith('torbox.'): return _run_debrid_method('debrids.tb_cloud', 'Menu', params, 'run')
	if mode.startswith('real_debrid.'): return _run_debrid_method('debrids.rd_cloud', 'Menu', params, 'run')
	if mode.startswith('alldebrid.'): return _run_debrid_method('debrids.ad_cloud', 'Menu', params, 'run')
	if mode.startswith('easynews.'): return _run_dynamic_func('debrids.easynews', mode, params)

# R10.28: one entry per running call in this folder (special://temp is the same for every profile, as the skin observer's
# lock). Netflix.Pov.Running alone is last-setter-wins: a call that starts during another one and ends first clears it while
# the other still runs. The entry is the call's own: nothing else adds or removes it, so overlapping calls cannot hide each
# other. Name: <Kodi pid>~<monotonic ms at start>~<the marker token, '|' as '~'>[+n]. The skin's observer reads the folder
# (extras/netflix_browse_state.py, POV_CALLS_DIR); an entry of another pid (an earlier Kodi run) or older than 60 s is dead.
POV_CALLS_DIR = 'special://temp/netflix_pov_calls/'

def _pov_call_begin(token):
	try:
		os_mod, time_mod = __import__('os'), __import__('time')
		folder = translate_path(POV_CALLS_DIR)
		os_mod.makedirs(folder, exist_ok=True)
		name = '%d~%d~%s' % (os_mod.getpid(), int(time_mod.monotonic() * 1000), token.replace('|', '~'))
		for n in range(8):
			path = os_mod.path.join(folder, name + ('+%d' % n if n else ''))
			try: os_mod.close(os_mod.open(path, os_mod.O_CREAT | os_mod.O_EXCL | os_mod.O_WRONLY, 0o644))
			except FileExistsError: continue
			return path
	except Exception: pass
	return None

def _pov_call_end(path):
	if not path: return
	try: __import__('os').remove(path)
	except Exception: pass

class Router:
	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		if get_property('pov_rli_fix') != 'true' or not kodi_utils.external_browse(): return
		message = f"pov not in '{kodi_utils.get_infolabel('Container.PluginName')}'"
		raise SystemExit(message)

	def __call__(self):
		# R10.5 (re-review F1): tell the skin's row observer a POV call is running (positive evidence that a list job it
		# lost track of after a tab switch has ended). Cleared only if no later call has taken it over.
		sys_obj = __import__('sys')
		token = '%s|%s' % (sys_obj.argv[1] if len(sys_obj.argv) > 1 else '', __import__('time').time())
		entry = None
		try:
			entry = _pov_call_begin(token)   # R10.28: this call's own entry, added BEFORE the marker is set (inside the try: no leak) ...
			set_property('Netflix.Pov.Running', token)
			with self: return routing(sys_obj)
		finally:
			_pov_call_end(entry)   # ... and removed BEFORE the marker is cleared (the folder never shows less than the marker)
			if get_property('Netflix.Pov.Running') == token: clear_property('Netflix.Pov.Running')

def initializeDatabases():
	from modules.cache import check_databases
	logger('POV', 'InitializeDatabases Service Starting')
	check_databases()
	return logger('POV', 'InitializeDatabases Service Finished')

def checkSettingsFile():
	logger('POV', 'CheckSettingsFile Service Starting')
	profile_dir = kodi_utils.get_addoninfo('profile')
	profile_xml = profile_dir + 'settings.xml'
	if not path_exists(profile_xml):
		kodi_utils.make_directorys(profile_dir)
		kodi_utils.addon().setSetting('kodi_menu_cache', 'true')
		kodi_utils.sleep(500)
	clear_property('pov_settings')
	make_settings_dict()
	set_property('pov_kodi_menu_cache', get_setting('kodi_menu_cache'))
	set_property('pov_rli_fix', get_setting('rli_fix'))
	return logger('POV', 'CheckSettingsFile Service Finished')

def databaseMaintenance():
	from caches.meta_cache import MetaCache
	MetaCache().prefetch()
	current_time = int(datetime.now().timestamp())
	next_clean = current_time + 259200 # 3 days
	due_clean = int(get_setting('database.maintenance.due', '0'))
	if current_time < due_clean: return
	logger('POV', 'Database Maintenance Service Starting')
	kodi_utils.clean_settings(silent=True)
	from modules.cache import clean_databases
	clean_databases(current_time, database_check=False, silent=True)
	set_setting('database.maintenance.due', str(next_clean))
	return logger('POV', 'Database Maintenance Service Finished')

def viewsSetWindowProperties():
	logger('POV', 'ViewsSetWindowProperties Service Starting')
	kodi_utils.set_view_properties()
	return logger('POV', 'ViewsSetWindowProperties Service Finished')

CHADFLIX_DIR = 'special://profile/addon_data/plugin.video.pov/chadflix/'
CHADFLIX_REUSE_MARKER = CHADFLIX_DIR + 'reuse-off.done'

def chadflixReuseOff():
	# ChadFlix (one time per Kodi profile): interpreter reuse OFF, and with it rli_fix OFF, which only matters while
	# reuse is on and whose SystemExit path ChadFlix rows no longer exempt (R9.4). Runs right after checkSettingsFile,
	# before databaseMaintenance and reuseLanguageInvokerCheck, so the shipped addon.xml (false) and the setting agree:
	# no rewrite, no "reload profile" prompt. The marker makes it run once: a person who turns reuse back on later
	# (POV Tools) keeps their choice. It lives in a SUBFOLDER: remove_old_databases deletes every loose file in the
	# profile folder that is not one of POV's own databases, but leaves folders alone (as with quality_preview/).
	if path_exists(CHADFLIX_REUSE_MARKER): return
	logger('POV', 'ChadFlix reuse-off migration Starting')
	before = (get_setting('reuse_language_invoker', 'true'), get_setting('rli_fix', 'true'))
	if before[0] != 'false': set_setting('reuse_language_invoker', 'false')
	if before[1] != 'false': set_setting('rli_fix', 'false')
	addon = kodi_utils.addon()  # created after the writes, so the read-back sees them
	if (addon.getSetting('reuse_language_invoker'), addon.getSetting('rli_fix')) != ('false', 'false'):
		return logger('POV', 'ChadFlix reuse-off migration: setting not saved, will retry at next start')
	clear_property('pov_settings')
	make_settings_dict()
	set_property('pov_rli_fix', 'false')
	kodi_utils.make_directorys(CHADFLIX_DIR)
	f = kodi_utils.open_file(CHADFLIX_REUSE_MARKER, 'w')
	try: f.write('reuse_language_invoker %s -> false, rli_fix %s -> false\n' % before)
	finally: f.close()
	if not path_exists(CHADFLIX_REUSE_MARKER):  # xbmcvfs write / mkdirs fail silently: no marker = no "Finished", retry next start
		return logger('POV', 'ChadFlix reuse-off migration: marker not written, will retry at next start')
	return logger('POV', 'ChadFlix reuse-off migration Finished (was reuse=%s rli_fix=%s)' % before)

def chadflixPrivacyHousekeeping():
	# ChadFlix R10.25 (security audit F-07), once per Kodi start: finished source checks past their 10 minute reuse time are
	# deleted from quality_preview/ (before, they stayed until the next successful check), together with every file an older
	# version wrote there (those held playable links); a sign-in QR image left in temp/pov_qr/ by a crash is deleted too.
	from modules.netflix_quality_preview import housekeeping
	housekeeping()
	from modules import local_qr
	local_qr.startup_sweep()
	return logger('POV', 'ChadFlix privacy housekeeping Finished')

def reuseLanguageInvokerCheck():
	logger('POV', 'ReuseLanguageInvokerCheck Service Starting')
	import xml.etree.ElementTree as ET
	addon_xml = translate_path('special://home/addons/%s/addon.xml' % kodi_utils.get_addoninfo('id'))
	tree = ET.parse(addon_xml)
	root = tree.getroot()
	current_addon_setting = get_setting('reuse_language_invoker', 'true')
	text = '[B]Reuse Language Invoker[/B] SETTING/XML mismatch[CR]POV will reload your profile to refresh the addon.xml'
	item, refresh = next(root.iter('reuselanguageinvoker'), None), False
	if item is None: kodi_utils.notification(text.split('[CR]')[0])
	if item is not None and item.text != current_addon_setting:
		item.text = current_addon_setting
		tree.write(addon_xml)
		refresh = True
	if refresh and kodi_utils.confirm_dialog(text=text):
		kodi_utils.execute_builtin('LoadProfile(%s)' % kodi_utils.get_infolabel('system.profilename'))
	return logger('POV', 'ReuseLanguageInvokerCheck Service Finished')

def autoRun():
	logger('POV', 'AutoRun Service Starting')
	if settings.auto_start_pov(): kodi_utils.execute_builtin('RunAddon(%s)' % kodi_utils.get_addoninfo('id'))
	return logger('POV', 'AutoRun Service Finished')

def clearSubs():
	logger('POV', 'Clear Subtitles Service Starting')
	sub_formats = ('.srt', '.ssa', '.smi', '.sub', '.idx')
	subtitle_path = 'special://temp/'
	for i in kodi_utils.list_dirs(subtitle_path)[1]:
		if i.startswith('POVSubs_') or i.endswith(sub_formats):
			kodi_utils.delete_file(subtitle_path + i)
	return logger('POV', 'Clear Subtitles Service Finished')

def premAccntNotification():
	logger('POV', 'Debrid Account Expiry Notification Service Starting')
	from importlib import import_module
	debrid_expiry_checks = settings.debrid_expiry_checks()
	for user, expires, module, cls in debrid_expiry_checks:
		try:
			if not get_setting(user): continue
			if (limit := int(get_setting(expires, '7'))) < 1: continue
			module = import_module('indexers.%s' % module)
			days_remaining = getattr(module, cls)().days_remaining()
			if days_remaining is None or days_remaining > limit: continue
			kodi_utils.notification('%s expires in %s days' % (cls, days_remaining))
		except: pass
	return logger('POV', 'Debrid Account Expiry Notification Service Finished')

def checkUndesirablesDatabase():
	logger('POV', 'CheckUndesirablesDatabase Service Starting')
	path = 'special://home/addons/%s/resources/unwanted.json' % kodi_utils.get_addoninfo('id')
	with kodi_utils.open_file(path) as file: set_property('pov_unwanted', str(file.read()))
	from indexers.pov_api import Undesirables, add_new_default_keywords
	old_database = Undesirables().check_database()
	if old_database: add_new_default_keywords()
	return logger('POV', 'CheckUndesirablesDatabase Service Finished')

class SyncMonitorService:
	def __init__(self, monitor_obj=None):
		self.monitor = monitor_obj or kodi_utils.xbmc_monitor()
		self.player = kodi_utils.xbmc_player()
		from indexers.trakt_api import trakt_sync_activities
		from indexers.mdblist_api import mdbl_sync_activities
		from indexers.tmdb_api import tmdb_clean_watchlist, clear_tmdbl_cache
		self.trakt_sync_activities = trakt_sync_activities
		self.mdbl_sync_activities = mdbl_sync_activities
		self.tmdb_clean_watchlist = tmdb_clean_watchlist
		self.clear_tmdbl_cache = clear_tmdbl_cache
		self.service_string = 'SyncMonitor Service Update %s - %s'
		self.update_string = 'Next Update in %s minutes...'

	def __call__(self):
		logger('POV', 'SyncMonitor Service Starting')
		if get_property('pov_traktmonitor_first_run') != 'true':
			set_property('pov_traktmonitor_first_run', 'true')
			self.clear_tmdbl_cache()
		while not self.monitor.abortRequested():
			if get_property('pov_traktmonitor_first_run') != 'true':
				self.monitor.waitForAbort(5)
			else: self.wait_if_busy()
			value, interval = settings.trakt_sync_interval()
			next_update_str = self.update_string % value
			self.sync_trakt(next_update_str)
			self.sync_mdblist(next_update_str)
			self.sync_tmdb()
			self.monitor.waitForAbort(interval)
		logger('POV', 'SyncMonitor Service Finished')

	def wait_if_busy(self):
		while (
			self.player.isPlaying() or
			get_property('pov_pause_services') == 'true' or
			kodi_utils.get_visibility('Container.isUpdating')
		): self.monitor.waitForAbort(10)

	def refresh_widgets(self, monitor_name):
		if settings.trakt_sync_refresh_widgets():
			kodi_utils.widget_refresh()
			logger('POV', self.service_string % ('POV %s - Widgets Refresh' % monitor_name, 'Setting Activated. Widget Refresh Performed'))
		else:
			logger('POV', self.service_string % ('POV %s - Widgets Refresh' % monitor_name, 'Setting Disabled. Skipping Widget Refresh'))

	def sync_trakt(self, next_update_str):
		try: status = self.trakt_sync_activities(init_callback=True, monitor=self.monitor)
		except: status = 'failed'
		if status == 'success':
			logger('POV', self.service_string % ('POV TraktMonitor - Success', 'Trakt Update Performed'))
			self.refresh_widgets('TraktMonitor')
		elif status == 'no account':
			logger('POV', self.service_string % ('POV TraktMonitor - Aborted. No Trakt Account Active', next_update_str))
		elif status == 'failed':
			logger('POV', self.service_string % ('POV TraktMonitor - Failed. Error from Trakt', next_update_str))
		else:
			logger('POV', self.service_string % ('POV TraktMonitor - Success. No Changes Needed', next_update_str))

	def sync_mdblist(self, next_update_str):
		try: status = self.mdbl_sync_activities(init_callback=True, monitor=self.monitor)
		except: status = 'failed'
		if status == 'success':
			logger('POV', self.service_string % ('POV MDBListMonitor - Success', 'MDBList Update Performed'))
			self.refresh_widgets('MDBListMonitor')
		elif status == 'no account':
			logger('POV', self.service_string % ('POV MDBListMonitor - Aborted. No MDBList Account Active', next_update_str))
		elif status == 'failed':
			logger('POV', self.service_string % ('POV MDBListMonitor - Failed. Error from MDBList', next_update_str))
		else:
			logger('POV', self.service_string % ('POV MDBListMonitor - Success. No Changes Needed', next_update_str))

	def sync_tmdb(self):
		try:
			if get_setting('tmdb.token') and get_setting('tmdblist.watchlist_sync') == 'true':
				status = self.tmdb_clean_watchlist(silent=True, monitor=self.monitor)
				if status: logger('POV', 'TMDb Lists Service Update - Success. %s' % status)
		except: pass

class SettingsMonitor(kodi_utils.xbmc_monitor):
	def onSettingsChanged(self):
		clear_property('pov_settings')
		kodi_utils.sleep(50)
		make_settings_dict()
		set_property('pov_kodi_menu_cache', get_setting('kodi_menu_cache'))
		set_property('pov_rli_fix', get_setting('rli_fix'))

	def onScreensaverActivated(self):
		set_property('pov_pause_services', 'true')

	def onScreensaverDeactivated(self):
		clear_property('pov_pause_services')

	def onNotification(self, sender, method, data):
		if method == 'System.OnSleep': set_property('pov_pause_services', 'true')
		elif method == 'System.OnWake': clear_property('pov_pause_services')
		# R10.55.4: the ChadFlix skin's RunScript holds (R10.52 show page, R10.55.2 Watchlist picker) never see
		# abortRequested; this tells them Kodi is quitting so they end at once instead of being killed after 5 s. Only on
		# System.OnQuit (Kodi quits or restarts): never when just this service stops (add-on update, disable, profile load),
		# which would end every later hold early for the rest of the session.
		elif method == 'System.OnQuit': set_property('Netflix.Quitting', 'true')

	def __call__(self):
		ver = '%s-%s' % (kodi_utils.get_addoninfo('id'), kodi_utils.get_addoninfo('version'))
		logger('POV', 'Settings Monitor Service Starting (%s)' % ver)
		clear_property('Netflix.Quitting')   # R10.55.4: a Kodi that is running is not quitting (see onNotification)
		initializeDatabases()
		checkSettingsFile()
		try: chadflixReuseOff()
		except Exception as e: logger('POV', 'ChadFlix reuse-off migration error: %s' % e)
		try: databaseMaintenance()
		except: pass
		try: viewsSetWindowProperties()
		except: pass
		try: reuseLanguageInvokerCheck()
		except: pass
		try: threads = (Thread(target=premAccntNotification), Thread(target=SyncMonitorService(self)))
		except: threads = ()
		for i in threads: i.start()
		try: autoRun()
		except: pass
		try: clearSubs()
		except: pass
		try: checkUndesirablesDatabase()
		except: pass
		try: chadflixPrivacyHousekeeping()
		except Exception as e: logger('POV', 'ChadFlix privacy housekeeping error: %s' % type(e).__name__)
		self.waitForAbort()
		for i in threads: i.join()
		logger('POV', 'Settings Monitor Service Finished')

