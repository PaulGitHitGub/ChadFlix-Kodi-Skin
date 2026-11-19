from threading import Thread
from caches.watched_cache import get_bookmarks, get_resumetime, get_watched_info_movie, get_watched_status_movie, set_resumetime
from indexers.metadata import art_infodict, movie_meta, movie_show_infodict, tmdb_image_base
from modules import kodi_utils, settings
from modules.utils import TaskPool, get_datetime, manual_function_import
# logger = kodi_utils.logger

KODI_VERSION, make_cast_list, default_duration = kodi_utils.get_kodi_version(), kodi_utils.make_cast_list, 3600
string, ls, build_url, get_infolabel = str, kodi_utils.local_string, kodi_utils.build_url, kodi_utils.get_infolabel
run_plugin, container_refresh, container_update = 'RunPlugin(%s)', 'Container.Refresh(%s)', 'Container.Update(%s)'
fanart_empty = kodi_utils.get_addoninfo('fanart')
poster_empty = kodi_utils.media_path('box_office.png')
item_jump = kodi_utils.media_path('item_jump.png')
item_next = kodi_utils.media_path('item_next.png')
watched_str, unwatched_str, favmanager_str, play_str = ls(32642), ls(32643), ls(32197), '[B]%s...[/B]' % ls(32174)
extras_str, options_str, exit_str, clearprog_str = ls(32645), ls(32646), ls(32649), ls(32651)
traktmanager_str, mdblmanager_str, tmdbmanager_str = ls(32198), ls(32200), ls(32199)
nextpage_str, jumpto_str = ls(32799), ls(32800)

ACCUMULATE_MIN, MAX_PAGES, ACCUMULATE_STEP, ACCUMULATE_CAP = 2, 10, 3, 400

def parse_accumulate_to(value):
	# Optional plugin param accumulate_to=N (integer 2..10). Anything else is ignored (None).
	if value is None: return None
	try: value = int(str(value).strip())
	except (TypeError, ValueError): return None
	if ACCUMULATE_MIN <= value <= MAX_PAGES: return value
	return None

def accumulate_genre_pages(fetch, key, accumulate_to):
	# fetch(page) -> (rows, total_pages). Fetches pages 1..accumulate_to (never more), appends rows in order,
	# de-duplicates by key(row) keeping the first occurrence, stops on an empty page, on total_pages, or at
	# ACCUMULATE_CAP rows (whole pages only; page 1 is truncated if it alone exceeds the cap).
	# Returns (rows, more). more is None (nothing further) or (param_name, page):
	#   ('accumulate_to', min(last + ACCUMULATE_STEP, MAX_PAGES)) -> load pages 1..that in place (last < MAX_PAGES)
	#   ('new_page', page)          -> ordinary single page; used once page MAX_PAGES was fetched, after a cap, or after a
	#                                  failed fetch, so selecting the tile always makes progress.
	items, seen, last, total, plain, done = [], set(), 0, 0, None, False
	for page in range(1, accumulate_to + 1):
		if len(items) >= ACCUMULATE_CAP: plain = page; break
		try: rows, page_total = fetch(page)
		except Exception:
			if page == 1: raise
			plain = page
			break
		if not rows: done = True; break
		fresh = []
		for row in rows:
			row_key = key(row)
			if row_key in seen: continue
			seen.add(row_key)
			fresh.append(row)
		last, total = page, page_total
		if len(items) + len(fresh) > ACCUMULATE_CAP:
			if page == 1: items.extend(fresh[:ACCUMULATE_CAP]); plain = 2
			else: last, plain = page - 1, page
			break
		items.extend(fresh)
		if page >= total: break
	if done: return items, None
	if plain: return items, (('new_page', plain) if plain <= total else None)
	if last and last < total:
		if last < MAX_PAGES: return items, ('accumulate_to', min(last + ACCUMULATE_STEP, MAX_PAGES))
		return items, ('new_page', last + 1)
	return items, None

def trakt_ids_key(ids):
	# Stable de-duplication key for a Trakt ids dict (first available id wins); same rule as tvshows.show_ids_key.
	for name in ('trakt', 'tmdb', 'imdb', 'slug'):
		if ids.get(name): return (name, ids[name])
	return ('ids', tuple(sorted((str(k), str(v)) for k, v in ids.items())))

class Movies:
	def __init__(self, params):
		self.params = params
		self.id_type = self.params.get('id_type', 'tmdb_id')
		# R10.25 (inherited-audit L2): id_type becomes a column name in the metadata cache query: only the four real ones
		if self.id_type not in ('tmdb_id', 'imdb_id', 'tvdb_id', 'trakt_dict'): self.id_type = 'tmdb_id'
		self.list = self.params.get('list', [])
		self.action = self.params.get('action')
		self.exit_list_params = self.params.get('exit_list_params')
		self.items, self.new_page, self.total_pages = [], {}, None
		self.append = self.items.append
		self.current_date = get_datetime()
		self.meta_user_info = settings.metadata_user_info()
		self.watched_indicators = settings.watched_indicators()
		self.watched_title = settings.watched_title(self.watched_indicators)
		self.watched_info = get_watched_info_movie(self.watched_indicators)
		self.bookmarks = get_bookmarks(self.watched_indicators, 'movie')
		self.include_year_in_title = settings.include_year_in_title('movie')
		self.open_extras = settings.extras_open_action('movie')
		self.cm_sort = settings.context_menu_sort()
		self.is_widget = kodi_utils.external_browse()
		self.widget_hide_watched = self.is_widget and self.meta_user_info['widget_hide_watched']
		if not self.exit_list_params: self.exit_list_params = get_infolabel('Container.FolderPath')
		self.art_provider = (*settings.get_art_provider(), poster_empty, fanart_empty)

	def build_movie_content(self, position, tag, *, memory_only=False):
		try:
			meta = movie_meta(self.id_type, tag, self.meta_user_info, self.current_date, memory_only=memory_only)
			if memory_only and meta is None: return False
			meta_get = meta.get
			if not meta or meta_get('blank_entry', False): return
			playcount, overlay = get_watched_status_movie(self.watched_info, string(meta['tmdb_id']))
			if self.widget_hide_watched and playcount: return
			meta.update({'playcount': playcount, 'overlay': overlay})
			resumetime, progress = get_resumetime(self.bookmarks, string(meta['tmdb_id']))
			cm = []
			cm_append = cm.append
			rootname, title, year = meta_get('rootname'), meta_get('title'), meta_get('year')
			display = rootname if self.include_year_in_title else title
			tmdb_id, imdb_id = meta_get('tmdb_id'), meta_get('imdb_id')
			try: tags = [i for i in (imdb_id, string(tmdb_id)) if i]
			except: tags = []
			play_params = build_url({
				'mode': 'play_media', 'mediatype': 'movie', 'tmdb_id': tmdb_id
			})
			extras_params = build_url({
				'mode': 'extras_menu_choice', 'mediatype': 'movie',
				'tmdb_id': tmdb_id, 'is_widget': self.is_widget
			})
			options_params = build_url({
				'mode': 'options_menu_choice', 'mediatype': 'movie',
				'tmdb_id': tmdb_id, 'is_widget': self.is_widget
			})
			recommended_params = build_url({
				'mode': 'build_movie_list', 'action': 'tmdb_movies_recommendations', 'tmdb_id': tmdb_id
			})
			trakt_manager_params = build_url({
				'mode': 'trakt_manager_choice', 'mediatype': 'movie',
				'tmdb_id': tmdb_id, 'imdb_id': imdb_id, 'tvdb_id': 'None'
			})
			mdbl_manager_params = build_url({
				'mode': 'mdbl_manager_choice', 'mediatype': 'movie',
				'tmdb_id': tmdb_id, 'imdb_id': imdb_id, 'tvdb_id': 'None'
			})
			tmdb_manager_params = build_url({
				'mode': 'tmdb_manager_choice', 'mediatype': 'movie',
				'tmdb_id': tmdb_id, 'imdb_id': imdb_id, 'tvdb_id': 'None'
			})
			fav_manager_params = build_url({
				'mode': 'favorites_choice', 'mediatype': 'movie',
				'tmdb_id': tmdb_id, 'title': title
			})
			cm_append((self.cm_sort['options'], options_str, run_plugin % options_params))
			if self.open_extras:
				url_params = extras_params
				cm_append((self.cm_sort['extras'], play_str, run_plugin % play_params))
			else:
				url_params = play_params
				cm_append((self.cm_sort['extras'], extras_str, run_plugin % extras_params))
			cm_append((self.cm_sort['trakt'], traktmanager_str, run_plugin % trakt_manager_params))
			cm_append((self.cm_sort['mdblist'], mdblmanager_str, run_plugin % mdbl_manager_params))
			cm_append((self.cm_sort['tmdblist'], tmdbmanager_str, run_plugin % tmdb_manager_params))
			cm_append((self.cm_sort['favorites'], favmanager_str, run_plugin % fav_manager_params))
			if progress != '0' or resumetime != '0': cm_append((
				self.cm_sort['mark'], clearprog_str, run_plugin % build_url({
					'mode': 'watched_unwatched_erase_bookmark', 'mediatype': 'movie',
					'tmdb_id': tmdb_id, 'refresh': 'true'
			})))
			if playcount: cm_append((
				self.cm_sort['mark'], unwatched_str % self.watched_title, run_plugin % build_url({
					'mode': 'mark_as_watched_unwatched_movie', 'action': 'mark_as_unwatched',
					'tmdb_id': tmdb_id, 'title': title, 'year': year
			})))
			else: cm_append((
				self.cm_sort['mark'], watched_str % self.watched_title, run_plugin % build_url({
					'mode': 'mark_as_watched_unwatched_movie', 'action': 'mark_as_watched',
					'tmdb_id': tmdb_id, 'title': title, 'year': year
			})))
			cm_append((self.cm_sort['exit'], exit_str, container_refresh % self.exit_list_params))
			cm.sort(key=lambda k: k[0])
			cm = [v for k, *v in cm if k]
			if self.action == 'favorites_movies' and self.params.get('netflix_saved_revision'):
				saved_id = string(tmdb_id).lstrip('0')
				if saved_id.isascii() and saved_id.isdecimal():
					cm.insert(0, ('Remove From Row',
					              'RunScript(special://skin/extras/netflix_row_remove.py,favorite,movie,%s)' % saved_id))
			props = {'pov_sort_order': string(position), 'watchedprogress': progress}
			listitem = kodi_utils.make_listitem()
			listitem.addContextMenuItems(cm)
			listitem.setProperties(props)
			listitem.setLabel(display)
			listitem.setArt(art_infodict(meta, self.art_provider, self.meta_user_info))
			if KODI_VERSION < 20:
				listitem.setUniqueIDs({'imdb': imdb_id, 'tmdb': string(tmdb_id)})
				listitem.setInfo('video', movie_show_infodict(meta))
				listitem.setCast(meta_get('cast', []))
				listitem.setProperty('resumetime', resumetime)
			else:
				infotag = listitem.getVideoInfoTag(offscreen=True)
				infotag.setTitle(display)
				infotag.setUniqueIDs({'imdb': imdb_id, 'tmdb': string(tmdb_id)})
				infotag.setCast(make_cast_list(meta_get('cast', [])))
				infotag.setCountries(meta_get('country'))
				infotag.setDirectors(meta_get('director').split(', '))
				infotag.setDuration(int(meta_get('duration') or default_duration))
				set_resumetime(resumetime, progress, infotag)
				infotag.setGenres(meta_get('genre').split(', '))
				infotag.setIMDBNumber(imdb_id)
				infotag.setMediaType('movie')
				infotag.setMpaa(meta_get('mpaa'))
				infotag.setPlaycount(playcount)
				infotag.setPlot(meta_get('plot'))
				infotag.setPremiered(meta_get('premiered'))
				infotag.setRating(meta_get('rating'))
				infotag.setStudios((meta_get('studio'),))
				infotag.setTagLine(meta_get('tagline'))
				infotag.setTags(tags)
				infotag.setTrailer(meta_get('trailer'))
				infotag.setVotes(meta_get('votes'))
				infotag.setWriters(meta_get('writer').split(', '))
			self.append((url_params, listitem, False))
		except: pass

class Menu(Movies):
	personal_dict = {'watched_movies': ('caches.watched_cache', 'get_watched_movie_tvshow'), 'in_progress_movies': ('caches.watched_cache', 'get_in_progress_items'), 'favorites_movies': ('indexers.local_api', 'local_favorites')}
	tmdb_special_key_dict = {'tmdb_movies_networks': 'company', 'tmdb_movies_year': 'year', 'tmdb_moviesanime_year': 'year'}
	tmdb_main = ('tmdb_movies_trending', 'tmdb_movies_popular', 'tmdb_movies_latest_releases', 'tmdb_movies_premieres', 'tmdb_movies_upcoming', 'tmdb_movies_blockbusters', 'tmdb_oscar_winners', 'tmdb_moviesanime_popular', 'tmdb_moviesanime_latest_releases')
	trakt_main = ('trakt_movies_trending', 'trakt_movies_trending_recent', 'trakt_movies_most_watched', 'trakt_moviesanime_trending', 'trakt_moviesanime_most_watched')
	tmdb_personal = ('tmdb_watchlist', 'tmdb_favorites', 'tmdb_recommendations')
	trakt_personal = ('trakt_collection', 'trakt_watchlist', 'trakt_favorites', 'trakt_collection_lists', 'trakt_watchlist_lists')
	mdblist_personal = ('mdblist_collection', 'mdblist_watchlist')
	similar = ('tmdb_movies_similar', 'tmdb_movies_recommendations')

	def build_movies_results(self):
		# R10.11a: the genre view route and the anime genre route take the same ChadFlix tab-cache path as the genre rows
		if (self.action == 'trakt_movies_trending'
			or (self.action in ('tmdb_movies_genres', 'tmdb_moviesanime_genres', 'netflix_movie_views') and self.params.get('netflix_tab_genre') in ('movies', 'home'))):
			from menus.netflix_tab_cache import build_cached_page
			build_cached_page(self.build_movie_content, self.list)
		else:
			for i in TaskPool().tasks_enumerate(self.build_movie_content, self.list, Thread): i.join()
		self.items.sort(key=lambda k: int(k[1].getProperty('pov_sort_order')))
		if self.action == 'netflix_movie_views':   # R10.11a: count + state for the skin's "Only N titles" note
			from indexers.netflix_api import view_report
			view_report('movies', self.params, len(self.items), self.new_page, getattr(self, 'view_error', ''), len(self.list))
		return self.items

	def build_collections_results(self):
		image_resolution = self.meta_user_info['image_resolution']
		for item in self.list:
			try:
				url_params = build_url({'mode': 'build_movie_list', 'action': 'tmdb_movies_collection', 'collection_id': item['id']})
				poster_path, backdrop_path = item['poster_path'], item['backdrop_path']
				if poster_path: poster = tmdb_image_base % (image_resolution['poster'], poster_path)
				else: poster = poster_empty
				if backdrop_path: fanart = tmdb_image_base % (image_resolution['fanart'], backdrop_path)
				else: fanart = fanart_empty
				listitem = kodi_utils.make_listitem()
				listitem.setLabel(item['name'])
				listitem.setInfo('Video', {'plot': item['overview']})
				listitem.setArt({'icon': poster, 'fanart': fanart})
				self.append((url_params, listitem, True))
			except: pass
		return self.items

	def run(self):
		__handle__ = int(kodi_utils.argv1())
		if self.params.get('netflix_grid_shell') and not self.params.get('netflix_grid_items'):
			# Netflix grid page: the window's own directory is an empty shell. The skin's static grid list
			# requests the titles itself (same route plus netflix_grid_items=1), so it can grow in place.
			# Same closing calls as a normal list, with no items and no API call.
			kodi_utils.set_category(__handle__, ls(self.params.get('name')))
			kodi_utils.set_sort_method(__handle__, 'movies')
			kodi_utils.set_content(__handle__, 'movies')
			kodi_utils.end_directory(__handle__, False if self.is_widget else None)
			kodi_utils.set_view_mode('view.movies', 'movies', self.is_widget)
			return
		try:
			params_get = self.params.get
			worker, view_type, content_type = self.build_movies_results, 'view.movies', 'movies'
			mode, category = params_get('mode'), ls(params_get('name'))
			try: page_no = int(params_get('new_page', '1'))
			except ValueError: page_no = params_get('new_page')
			if self.action in Menu.personal_dict: var_module, import_function = Menu.personal_dict[self.action]
			else: var_module, import_function = 'indexers.%s_api' % self.action.split('_')[0], self.action
			try: function = manual_function_import(var_module, import_function)
			except: pass
			if self.action in Menu.tmdb_main:
				data = function(page_no)
				self.list = [i['id'] for i in data['results']]
				total_pages = data['total_pages']
				if total_pages > page_no: self.new_page = {'new_page': string(data['page'] + 1)}
			elif self.action in Menu.trakt_main:
				self.id_type = 'trakt_dict'
				# staged/trending-grid: the skin's grid (list 593) asks for Trending with accumulate_to, like a genre.
				accumulate_to = parse_accumulate_to(params_get('accumulate_to')) if self.action == 'trakt_movies_trending' and page_no == 1 else None
				if accumulate_to:
					def fetch(page):
						data, total_pages = function(page)
						return [i['movie']['ids'] for i in data], total_pages
					self.list, more = accumulate_genre_pages(fetch, trakt_ids_key, accumulate_to)
					if more:
						self.new_page = {more[0]: string(more[1])}
						if params_get('netflix_tab_genre'): self.new_page['netflix_tab_genre'] = params_get('netflix_tab_genre')
						if params_get('netflix_grid_items'): self.new_page['netflix_grid_items'] = params_get('netflix_grid_items')
				else:
					data, total_pages = function(page_no)
					self.list = [i['movie']['ids'] for i in data]
					if total_pages > page_no: self.new_page = {'new_page': string(page_no + 1)}
			elif self.action in Menu.tmdb_personal:
				data, total_pages = function('movie', page_no)
				self.list = [i['id'] for i in data]
				if total_pages > page_no: self.new_page = {'new_page': string(page_no + 1)}
			elif self.action in Menu.trakt_personal:
				self.id_type = 'trakt_dict'
				data, total_pages = function('movies', page_no)
				self.list = [i['media_ids'] for i in data]
				if total_pages > 2: self.total_pages = total_pages
				if isinstance(page_no, int) and total_pages > page_no:
					self.new_page = {'new_page': string(page_no + 1)}
			elif self.action in Menu.mdblist_personal:
				data, total_pages = function('movies', page_no)
				self.list = [i['id'] for i in data]
				if total_pages > 2: self.total_pages = total_pages
				if total_pages > page_no: self.new_page = {'new_page': string(page_no + 1)}
			elif self.action in Menu.personal_dict:
				watched_info = self.bookmarks if self.action == 'in_progress_movies' else self.watched_info
				data, total_pages = function(watched_info, 'movie', page_no)
				self.list = [i['media_id'] for i in data]
				if total_pages > 2: self.total_pages = total_pages
				if total_pages > page_no: self.new_page = {'new_page': string(page_no + 1)}
			elif self.action in Menu.similar:
				tmdb_id = params_get('tmdb_id')
				data = function(tmdb_id, page_no)
				self.list = [i['id'] for i in data['results']]
				if data['page'] < data['total_pages']:
					self.new_page = {'new_page': string(data['page'] + 1), 'tmdb_id': tmdb_id}
			elif self.action in Menu.tmdb_special_key_dict:
				key = Menu.tmdb_special_key_dict[self.action]
				function_var = params_get(key)
				if not function_var: return
				data = function(function_var, page_no)
				self.list = [i['id'] for i in data['results']]
				if data['page'] < data['total_pages']:
					self.new_page = {'new_page': string(data['page'] + 1), key: function_var}
			elif self.action == 'netflix_movie_views':
				# R10.11a genre views: fixed route per view and genre; POV builds the query (indexers/netflix_api.view_list). No set_history.
				from indexers.netflix_api import view_list
				self.list, self.id_type, self.new_page, self.view_error = view_list('movies', self.params, page_no, parse_accumulate_to, accumulate_genre_pages, trakt_ids_key)
			elif self.action in ('tmdb_media_discover', 'tmdb_movies_discover'):
				from menus.discover import set_history
				name, query = params_get('name'), params_get('query')
				if page_no == 1: set_history('movie', name, query)
				data = function(query, page_no)
				self.list = [i['id'] for i in data['results']]
				if data['page'] < data['total_pages']:
					self.new_page = {'query': query, 'name': name, 'new_page': string(data['page'] + 1)}
			elif self.action in ('tmdb_movies_genres', 'tmdb_moviesanime_genres'):
				genre_id = params_get('genre_id')
				if not genre_id: return
				accumulate_to = parse_accumulate_to(params_get('accumulate_to')) if self.action in ('tmdb_movies_genres', 'tmdb_moviesanime_genres') and page_no == 1 else None
				if accumulate_to:
					def fetch(page):
						data = function(genre_id, page)
						return [i['id'] for i in data['results']], data['total_pages']
					self.list, more = accumulate_genre_pages(fetch, lambda k: k, accumulate_to)
					if more:
						self.new_page = {more[0]: string(more[1]), 'genre_id': genre_id}
						if params_get('netflix_tab_genre'): self.new_page['netflix_tab_genre'] = params_get('netflix_tab_genre')
						if params_get('netflix_grid_items'): self.new_page['netflix_grid_items'] = params_get('netflix_grid_items')
				else:
					data = function(genre_id, page_no)
					self.list = [i['id'] for i in data['results']]
					if data['page'] < data['total_pages']:
						self.new_page = {'new_page': string(data['page'] + 1), 'genre_id': genre_id}
			elif self.action == 'tmdb_movies_search':
				query = params_get('query')
				data = function(query, page_no)
				self.list = [i['id'] for i in data['results']]
				total_pages = data['total_pages']
				if total_pages > page_no: self.new_page = {'new_page': string(page_no + 1), 'query': query}
			elif self.action == 'tmdb_movies_search_collections':
				worker, view_type, content_type = self.build_collections_results, 'view.main', ''
				query = params_get('query')
				data = function(query, page_no)
				self.list = data['results']
				total_pages = data['total_pages']
				if total_pages > page_no: self.new_page = {'new_page': string(page_no + 1), 'query': query}
			elif self.action == 'tmdb_movies_collection':
				data = function(params_get('collection_id'))['parts']
				data.sort(key=lambda k: k['release_date'] or '2050')
				self.list = [i['id'] for i in data]
			elif self.action == 'trakt_recommendations':
				self.id_type = 'trakt_dict'
				data = function('movies')
				self.list = [i['ids'] for i in data]
			if self.total_pages and not self.is_widget and settings.nav_jump_use_alphabet():
				url_params = {
					'mode': 'build_navigate_to_page', 'current_page': page_no, 'total_pages': self.total_pages,
					'query': params_get('search_name', ''), 'actor_id': params_get('actor_id', ''),
					'transfer_mode': mode, 'transfer_action': self.action, 'mediatype': 'Movies'
				}
				kodi_utils.add_dir(__handle__, url_params, jumpto_str, item_jump, isFolder=False)
			kodi_utils.add_items(__handle__, worker())
			if self.new_page:
				self.new_page.update({'mode': mode, 'action': self.action, 'exit_list_params': self.exit_list_params, 'name': category})
				kodi_utils.add_dir(__handle__, self.new_page, nextpage_str, item_next)
		except: pass
		kodi_utils.set_category(__handle__, category)
		kodi_utils.set_sort_method(__handle__, content_type)
		kodi_utils.set_content(__handle__, content_type)
		kodi_utils.end_directory(__handle__, False if self.is_widget else None)
		kodi_utils.set_view_mode(view_type, content_type, self.is_widget)

