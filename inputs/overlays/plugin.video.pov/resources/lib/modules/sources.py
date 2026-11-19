import re
import json
import time
import pkgutil
import importlib
from concurrent.futures import ThreadPoolExecutor as TPE, as_completed
from threading import Event
from modules.netflix_next_context import PreparationScope, Thread, bind, current, ephemeral
from caches.providers_cache import ExternalProvidersCache
from indexers.pov_api import sources as pov_sources
from indexers.metadata import get_title, movie_meta, season_episodes_meta, tvshow_meta
from modules import kodi_utils, player, settings, source_utils
from modules.debrid import DebridCheck, Source, debrid_enabled, debrid_type_enabled
from modules.utils import get_datetime, safe_string, string_to_float
from modules.source_presentation import available_sources, play_facts, prefer_dynamic_range, rank_playback_sources, source_is_unchecked
from windows import create_window, open_window
# from modules.kodi_utils import logger

POVPlayer, monitor, progressDialogBG = player.POVPlayer, kodi_utils.xbmc_monitor(), kodi_utils.progressDialogBG()
ls, sleep, get_setting, notification = kodi_utils.local_string, kodi_utils.sleep, kodi_utils.get_setting, kodi_utils.notification
show_busy_dialog, hide_busy_dialog, close_all_dialog = kodi_utils.show_busy_dialog, kodi_utils.hide_busy_dialog, kodi_utils.close_all_dialog
get_property, set_property, clear_property = kodi_utils.get_property, kodi_utils.set_property, kodi_utils.clear_property
check_prescrape_sources, quality_filter, sort_to_top = settings.check_prescrape_sources, settings.quality_filter, settings.sort_to_top
results_xml_style, results_xml_window_number = settings.results_xml_style, settings.results_xml_window_number
default_internal_scrapers, cloud_scrapers = settings.default_internal_scrapers(), settings.cloud_scrapers()
pack_enable_check, sources_quality_count = source_utils.pack_enable_check, source_utils.sources_quality_count
get_cache_expiry, get_file_info = source_utils.get_cache_expiry, source_utils.get_file_info
quality_ranks = {'4K': 1, '1080p': 2, '720p': 3, 'SD': 4, 'SCR': 5, 'CAM': 5, 'TELE': 5}
av1_filter_key, hevc_filter_key, hdr_filter_key, dolby_vision_filter_key = '[B]AV1[/B]', '[B]HEVC[/B]', '[B]HDR[/B]', '[B]D/VISION[/B]'
total_format, int_format, ext_format = '[COLOR %s][B]%s[/B][/COLOR]', '[COLOR %s][B]Int: [/B][/COLOR]%s', '[COLOR %s][B]Ext: [/B][/COLOR]%s'
ext_scr_format, unfinshed_import_format, format_line = '[COLOR %s][B]%s[/B][/COLOR]', '[COLOR red]+%s[/COLOR]', '%s[CR]%s[CR]%s'
diag_format, resolutions, pack_display = '4K: %s | 1080p: %s | 720p: %s | SD: %s | Total: %s', '4K 1080p 720p SD total', '%s (%s)'
dialog_format = '[COLOR %s][B]%s[/B][/COLOR] 4K: %s | 1080p: %s | 720p: %s | SD: %s | Total: %s'
season_display, show_display, remaining_format, debrid_format = ls(32537), ls(32536), ls(32580), ls(32579)
pack_check = (season_display, show_display)
# R10-playback: a request from the Netflix skin (it carries netflix_playback_cover=<token>) loads on the title page
# itself (skin 1080i/Includes_Netflix_Playback.xml). POV then opens no loading window of its own, publishes what it is
# doing instead, and stops when the page withdraws the request (Back clears Netflix.Play.Loading). Every other caller
# keeps POV's own loading screens.
LOADING, PLAY_PREFIX, FLIGHT = 'Netflix.Play.Loading', 'Netflix.Play.', 'Netflix.Flight'

class QuietProgress:
	"""Stands in for POV's small background progress box on a Netflix-skin request."""
	def create(self, *args, **kwargs): pass
	def update(self, *args, **kwargs): pass
	def close(self): pass

quiet_progress = QuietProgress()

class PlayFlight:
	"""R10-playback: the title pages' single-flight entry (skin extras/netflix_movie_details.py, Netflix.Flight) names
	this request for as long as its interpreter runs: "play", renewed every 2 s and valid for 10 s (a request that
	died frees it), then "done" for 1.5 s while the interpreter leaves (the Mac copy measured up to 0.9 s from here to
	the interpreter's end after a cancel). A Play pressed again after Back waits for it, so two POV interpreters never
	run side by side. stop() also sets the 2 s alarm netflix_pov_leaving: the movie page ignores OK on Play while it
	runs, so no skin script starts in the instant this interpreter ends (review M4)."""
	RENEW, VALID, EXIT_ROOM = 2.0, 10.0, 1.5
	COVER_CAP = 90.0   # longest this request keeps the page's transition cover alive (a hung resolve cannot hold it)

	def __init__(self, media_id, token):
		self.media_id, self.token, self.stopping, self.thread = str(media_id or ''), token, Event(), None
		self.started = time.monotonic()

	def entry(self):
		parts = get_property(FLIGHT).split('|')
		try: return parts if time.time() < float(parts[4]) else None
		except (IndexError, ValueError): return None

	def write(self, kind, seconds):
		now = time.time()
		set_property(FLIGHT, '%s|%s|%s|%.2f|%.2f' % (kind, self.media_id, self.token, now, now + seconds))

	def owns_playback(self):
		entry = self.entry()
		return entry is not None and entry[:3] == ['play', self.media_id, self.token]

	def renew(self, starting=False):
		entry = self.entry()
		loader = (starting and get_property(LOADING) == self.token and entry
		          and entry[:2] == ['play', self.media_id])
		if entry is None or loader or entry[:3] == ['play', self.media_id, self.token]:
			self.write('play', self.VALID)
		# The movie page's transition cover carries this request's token until playback starts (on_ready clears it):
		# keep its fail-safe alarm alive only while the page still waits for this request (Back withdraws it) and for
		# at most COVER_CAP s, so a request that died, was withdrawn or hangs frees it within 20 s (review M2).
		if (self.token and get_property('Netflix.PlaybackCover') == self.token and get_property(LOADING) == self.token
				and time.monotonic() - self.started < self.COVER_CAP):
			kodi_utils.execute_builtin('AlarmClock(netflix_playback_cover,noop,00:20,silent)')

	def run(self):
		while not self.stopping.wait(self.RENEW): self.renew()

	def start(self):
		self.renew(starting=True)
		self.thread = Thread(target=self.run, name='netflix_play_flight', daemon=True)
		self.thread.start()

	def stop(self):
		self.stopping.set()
		if self.thread: self.thread.join(self.RENEW + 1)
		entry = self.entry()
		if entry is None or (entry[0] == 'play' and entry[2] == self.token): self.write('done', self.EXIT_ROOM)
		kodi_utils.execute_builtin('AlarmClock(netflix_pov_leaving,noop,00:02,silent)')

def prefer_available_quality(results, mediatype):
	"""Hide the fallback resolution only when its available replacement survives filtering."""
	qualities = {'movie': ('4K', '1080p'), 'episode': ('1080p', '720p')}.get(mediatype)
	if not qualities: return results
	preferred, fallback = qualities
	def available(item):
		cache_provider = (item.get('cache_provider') or '').lower()
		if 'uncached' in cache_provider or 'unchecked' in cache_provider: return False
		if item.get('scrape_provider') == 'aiostreams':
			return item.get('cached') is True or item.get('library') is True or item.get('source') == 'http'
		return True
	if not any(item.get('quality') == preferred and available(item) for item in results): return results
	return [item for item in results if item.get('quality') != fallback]

def all_sources_picks(results, mediatype):
	"""(top, medium) rows of All Sources (owner, 16 Nov); see quality_presets.all_sources_picks."""
	from modules.quality_presets import all_sources_picks as picks
	return picks(prefer_available_quality(available_sources(results), mediatype))

class Sources:
	nextep_params = []

	@classmethod
	def factory(cls, params):
		try: int(params['episode'])
		except: return cls().source_select(params)
		cls.nextep_callback(params)
		while cls.nextep_params:
			began = time.time()
			try: cls().source_select(cls.nextep_params.pop())
			except: pass
			# R9: the viewer left this episode with Exit / Stop (skin extras/netflix_exit_return.py stamps the
			# property just before it stops the player). A next episode queued by execute_nextep must not start.
			# A normal end, or "Play" on the Up Next pop-up, does not stamp it, so those still continue.
			if cls.exit_requested(began, params.get('netflix_playback_cover')): cls.nextep_params.clear()

	@staticmethod
	def exit_requested(since, play=None):
		fields = get_property('Netflix.Exit.NoNext').split('|')
		try: return since <= float(fields[0]) <= time.time() and (len(fields) == 1 or fields[1] == play)
		except ValueError: return False

	@classmethod
	def nextep_callback(cls, params):
		if not isinstance(params, dict): return
		cls.nextep_params.insert(0, params)

	@classmethod
	def prepare_next(cls, current_meta, cancelled, publish):
		"""Resolve one candidate into this request's memory and drain its workers."""
		from copy import deepcopy
		from urllib.parse import urlsplit
		from modules.episode_tools import nextep_playback_info
		from modules.netflix_next_model import Episode, PreparedEpisode

		scope = PreparationScope(cancelled)
		episode = prepared = None
		with bind(scope):
			try:
				if cancelled.is_set(): return None, None
				meta = deepcopy(current_meta)
				for field in ('url', 'bookmark', 'search_info'):
					meta.pop(field, None)
				meta, params = nextep_playback_info(meta, next_runtime=True)
				if not isinstance(params, dict) or cancelled.is_set(): return None, None
				show = str(params['tmdb_id'])
				if any(type(params[key]) is bool for key in ('season', 'episode')): return None, None
				season, number = int(params['season']), int(params['episode'])
				if (not show.isascii() or not show.isdecimal() or show != str(current_meta['tmdb_id']) or season < 0 or number < 1
						or (season, number) == (int(current_meta['season']), int(current_meta['episode']))):
					return None, None
				episode = Episode(show, season, number, int(meta.get('duration') or 0))
				publish(episode)
				source = cls()
				source.config_loader.apply(source, {**params, 'meta': json.dumps(meta),
					'background': 'true', 'autoplay': 'true'})
				source.full_screen, source.bg = False, quiet_progress
				source.meta = source.meta_builder.build(source)
				if (str(source.meta['tmdb_id']), int(source.meta['season']), int(source.meta['episode'])) != episode.identity:
					return episode, None
				results = rank_playback_sources(available_sources(source.get_sources() or []), quality_ranks)
				for item in results[:source.limit_resolve]:
					if cancelled.is_set(): break
					link = item.get('unrestricted_link')
					# R10.43: external (Torrentio etc.) results resolve too, also when an internal source is active.
					if not link and item.get('scrape_provider') in (*source.active_internal_scrapers, 'external'):
						link = Source(item, source.meta).resolve_sources()
					if not isinstance(link, str) or not link.strip() or '\n' in link or '\r' in link:
						continue
					try: scheme = urlsplit(link.split('|', 1)[0]).scheme.lower()
					except ValueError: continue
					if scheme not in ('', 'http', 'https', 'file', 'smb', 'nfs', 'special'): continue
					prepared = PreparedEpisode(episode, link, str(item.get('name') or ''), source.meta)
					break
			except Exception:
				# Provider failures can contain links in their tracebacks.
				scope.failed.set()
			finally:
				scope.drain()
		return episode, prepared if not cancelled.is_set() and not scope.failed.is_set() else None

	@classmethod
	def background_prep(cls, params):
		self = cls()
		self.source_select({**params, 'background': 'true'})
		return self.pov_background_url

	def __init__(self):
		self.params = {}
		self.clear_properties, self.filters_ignored = True, False
		self.progress_dialog, self.pov_background_url = None, None
		self.bg, self.quiet, self.flight = progressDialogBG, False, None
		self.threads, self.providers, self.sources, self.internal_scraper_names = [], [], [], []
		self.prescrape_scrapers, self.prescrape_threads, self.prescrape_sources = [], [], []
		self.remove_scrapers = ['external']# needs to be mutable so leave as list.
		self.exclude_list = ['easynews', 'library']# needs to be mutable so leave as list.
		self.internal_resolutions = dict.fromkeys('4K 1080p 720p SD total'.split(), 0)
		self.prescrape, self.disabled_ignored = 'true', 'false'
		self.config_loader = ConfigLoader()
		self.meta_builder = MetaBuilder()
		self.scraper_processor = ScraperProcessor(self)
		self.results_processor = ResultsProcessor(self)

	def source_select(self, params=None):
		try:
			if self.clear_properties: self._clear_properties()
			self.config_loader.apply(self, params)
			if self.quiet and self.flight is None:
				self.flight = PlayFlight(self.tmdb_id, self.params.get('netflix_playback_cover'))
				self.flight.start()
			self.quiet_busy()
			if self.cancelled(): return
			if not hasattr(self, 'meta'):
				self.meta = self.meta_builder.build(self)
			token = self.params.get('netflix_quality_token')
			preview = None
			if token and self.mediatype == 'movie' and not self.background:
				from modules.netflix_quality_preview import playback_preview
				preview = playback_preview(token, str(self.tmdb_id), self.timeout, monitor)
			if preview:
				results, self.meta['scrape_time'] = preview
				kodi_utils.logger('Netflix Quality', 'Reused details preload')
				hide_busy_dialog()
				self.prescrape = False
				self.progress_dialog = DialogProgress(self.full_screen)
			else: results = self.get_sources()
			if self.cancelled(): return
			if not results: return self._process_post_results()
			self.play_source(results)
		finally:
			if self.params.get('netflix_playback_cover'):
				if self.progress_dialog: self.progress_dialog.kill()
				self.clear_playback_cover()
				if get_property(LOADING) == self.params.get('netflix_playback_cover'):
					# Ended with no video while the page still waits (no source, a tier not available, every resolve
					# failed, All Sources closed, an error): the loader goes and the movie page's focus returns to Play
					# (it rested on the loader's catcher, which is hidden now). Review H1. The show page does its own.
					clear_property(LOADING)
					if kodi_utils.get_visibility('Window.IsVisible(1167)'):
						kodi_utils.execute_builtin('AlarmClock(netflix_loader_done,SetFocus(10),00:00,silent)')
				if self.flight: self.flight.stop()

	def cancelled(self):
		"""R10-playback: the page withdrew this request (Back during loading, or the page went away)."""
		scope = current()
		if scope is not None: return scope.cancelled.is_set()
		return self.quiet and get_property(LOADING) != self.params.get('netflix_playback_cover')

	def publish_play(self, **fields):
		"""R10-playback: tell the page's loader what is happening, only while the page still waits for this request."""
		if not self.quiet or self.cancelled(): return
		self.quiet_busy()
		for key, value in fields.items(): set_property(PLAY_PREFIX + key, value)

	def quiet_busy(self):
		"""R10-playback: Kodi raises its own busy spinner while a PlayMedia plugin call runs. On a skin request the
		page's loader is the progress, and the spinner would also take the page's Back, so it is closed whenever seen."""
		if self.quiet and any(kodi_utils.get_visibility('Window.IsVisible(%s)' % i) for i in ('busydialog', 'busydialognocancel')):
			hide_busy_dialog()

	def clear_playback_cover(self):
		token = self.params.get('netflix_playback_cover')
		if token and get_property('Netflix.PlaybackCover') == token:
			clear_property('Netflix.PlaybackCover')

	def get_sources(self):
		start_time = time.monotonic()
		self.progress_dialog = DialogProgress(getattr(self, 'full_screen', False))
		self.publish_play(Phase='find', Res='', SpecA='', SpecB='')   # facts pre-filled from the page's check no longer hold
		if self.prescrape and any(x in self.active_internal_scrapers for x in default_internal_scrapers):
			results = self.collect_prescrape_results()
			if results: results = self.results_processor.process(results)
		else: results = []
		if not results:
			self.prescrape = False
			self.orig_results = self.collect_results()
			results = self.results_processor.process(self.orig_results)
		self.meta.update({'scrape_time': time.monotonic() - start_time})
		return results

	def collect_results(self):
		self.sources.extend(self.prescrape_sources)
		self.scraper_processor.activate_internal(False)
		if self.providers:
			threads = ((self.activate_providers, (i[0], i[1], False), i[2]) for i in self.providers)
			self.threads.extend([Thread(target=i[0], args=i[1], name=i[2]) for i in threads])
			for i in self.threads: i.start()
		self.scraper_processor.activate_external()
		if self.external_providers or self.background:
			if self.active_external:
				self.activate_providers('external', (ExternalManager, self), False)
			elif self.providers and self.background: [i.join() for i in self.threads]
		else: self.scrapers_dialog('internal')
		return self.sources

	def collect_prescrape_results(self):
		self.scraper_processor.activate_internal(True)
		if not self.prescrape_scrapers: return []
		threads = ((self.activate_providers, (i[0], i[1], True), i[2]) for i in self.prescrape_scrapers)
		self.prescrape_threads.extend([Thread(target=i[0], args=i[1], name=i[2]) for i in threads])
		for i in self.prescrape_threads: i.start()
		self.remove_scrapers.extend(i[2] for i in self.prescrape_scrapers)
		if self.background: [i.join() for i in self.prescrape_threads]
		else: self.scrapers_dialog('pre_scrape')
		return self.prescrape_sources

	def activate_providers(self, module_type, function, prescrape):
		if module_type == 'external': module = function[0](function[1])
		else: module = function()
		sources = module.results(self.meta['search_info'])
		if not sources: return
		if prescrape: self.prescrape_sources.extend(sources)
		else: self.sources.extend(sources)

	def scrapers_dialog(self, scrape_type):
		if scrape_type == 'internal':
			scraper_list, threads_list = self.providers, self.threads
			line1_inst, line2_inst = ls(32139), 'Int:'
		else:
			scraper_list, threads_list = self.prescrape_scrapers, self.prescrape_threads
			line1_inst, line2_inst = '%s %s' % (ls(32829), ls(32830)), 'Pre:'
		self.internal_scrapers = [i[2] for i in scraper_list]
		if not self.internal_scrapers: return
		int_dialog_hl = get_setting('int_dialog_highlight') or 'dodgerblue'
		line1 = total_format % (int_dialog_hl, line1_inst)
		_total_format = total_format % (int_dialog_hl, '%s')
		timeout = self.timeout
		start_time = time.monotonic()
		end_time = start_time + timeout + 1
		if self.progress_dialog.full_screen: self.progress_dialog.make(self.meta)
		else: self.bg.create('POV', 'POV loading...')
		while not monitor.abortRequested() and time.monotonic() <= end_time:
			alive_threads = [x.name for x in threads_list if x.is_alive()]
			if not alive_threads or self.cancelled(): break
			self.quiet_busy()
			try:
				self.results_processor.process_internal_results()
				int_totals = [_total_format % v for v in self.internal_resolutions.values()]
				current_progress = time.monotonic() - start_time
				line2 = dialog_format % (int_dialog_hl, line2_inst, *int_totals)
				line3 = remaining_format % ', '.join(alive_threads).upper()
				percent = int((current_progress/float(timeout))*100)
				if self.progress_dialog.full_screen:
					self.progress_dialog.update(format_line % (line1, line2, line3), percent)
				else: self.bg.update(percent, line3)
			except: pass
			sleep(self.sleep_time)
		if self.progress_dialog.full_screen: self.progress_dialog.kill()
		else: self.bg.close()

	def _process_post_results(self):
		if self.autoplay: return self._no_results()
		if self.ignore_results_filter and self.orig_results:
			return self._process_ignore_filters()
		return self._no_results()

	def _process_ignore_filters(self):
		if self.autoplay: notification(32686)
		self.autoplay = False
		self.filters_ignored = True
		results = self.results_processor.sort_results(self.orig_results)
		results = self.results_processor.sort_first(results)
		if not results: return self._no_results()
		return self.play_source(results)

	def _no_results(self):
		hide_busy_dialog()
		if self.background: return
		notification(32573)

	def _clear_properties(self):
		for item in default_internal_scrapers: clear_property('%s.internal_results' % item)

	def display_results(self, results):
		if self.cancelled(): return
		window_style = results_xml_style()
		chosen_item = open_window(
			('windows.sources', 'SourceResults'),
			'sources_results.xml',
			window_style=window_style,
			window_id=results_xml_window_number(window_style),
			results=results,
			meta=self.meta,
			scraper_settings=self.scraper_settings,
			prescrape=self.prescrape,
			filters_ignored=self.filters_ignored
		)
		if not chosen_item: return self.progress_dialog.kill()
		action, chosen_item = chosen_item
		if action == 'play':
			return self.play_file(results, chosen_item)
		if action == 'perform_full_search' and self.prescrape:
			self.prescrape, self.clear_properties = False, False
			return self.source_select()

	def play_source(self, results):
		# R10.50: only the All Sources list (not autoplay, not background) also lists unchecked Real-Debrid torrents;
		# Play, the movie quality tiers and background checks keep confirmed sources only.
		listing = not self.autoplay and not self.background
		results = available_sources(results, include_unchecked=listing)
		picks = all_sources_picks(results, self.mediatype) if self.autoplay and self.mediatype == 'movie' else None
		if self.autoplay:
			results = rank_playback_sources(results, quality_ranks)
		else:
			results = prefer_available_quality(results, self.mediatype)
			results = prefer_dynamic_range(results)
		if not results: return self._no_results()
		if self.background: self.pov_background_url = bool(results)
		elif self.autoplay:
			if self.mediatype == 'movie':
				results = self.movie_quality_candidates(results, picks)
				if not results: return
			return self.play_file(results)
		else: return self.display_results(results)

	def movie_quality_indices(self, results, picks=None):
		from modules.quality_presets import build_presets
		duration = string_to_float(str(self.meta.get('duration') or 0), '0') / 60
		find = lambda wanted: next((i for i, item in enumerate(results) if item is wanted), None) if wanted is not None else None
		top, medium = picks or (None, None)
		indices = {item['key']: item['original_index'] for item in build_presets(results, 'movie', duration, find(top), find(medium))}
		if indices['highest'] is None and results: indices['highest'] = 0
		return indices

	def movie_quality_candidates(self, results, picks=None):
		"""Plain Play = Medium (owner, 16 Nov); it falls back to Highest when no HDR/DV encode exists."""
		key = self.movie_quality
		indices = self.movie_quality_indices(results, picks)
		if key == 'play':
			index = indices['medium'] if indices['medium'] is not None else indices['highest']
			key = 'highest'
		else:
			index = indices[key]
		if index is None:
			notification('No distinct %s Quality 4K source available' % ('Medium' if key == 'medium' else 'Low'))
			return []
		if key != 'highest': return [results[index]]
		return [results[index], *results[:index], *results[index + 1:]]

	def play_file(self, results, source=None):
		def _fast_to_resolve(item):
			return 'unrestricted_link' in item or item.get('scrape_provider') == 'aiostreams'
		try:
			if source is None:
				source_index, items = 0, results
			elif source in results:
				source_index = results.index(source)
				# R10.50: the picked row first (it may be unchecked), then later rows as before, but never another
				# unchecked one: a failing unchecked torrent costs ~1.5 s and an add/delete in the debrid account.
				items = [source, *(i for i in results[source_index + 1:] if 'Uncached' not in i.get('cache_provider', '')
					and not source_is_unchecked(i))]
			else: source_index, items = None, [source]
			if self.progress_dialog.full_screen:
				self.progress_dialog.make(self.meta)
				progress_media = self.progress_dialog.kill
			else:
				self.bg.create('POV', 'POV loading...')
				progress_media = None
			facts = self.params.get('netflix_play_facts') == 'true'
			for count, item in enumerate(items[:self.limit_resolve], 1):
				link = None
				if self.cancelled(): break
				# R10-playback: the loader names the source being opened, again for each fall-back.
				if facts:
					res, spec_a, spec_b = play_facts(item)
					self.publish_play(Phase='open', Res=res, SpecA=spec_a, SpecB=spec_b)
				else: self.publish_play(Phase='open')
				try:
					if monitor.abortRequested(): break
					elif self.progress_dialog.full_screen and self.progress_dialog.iscanceled(): break
					percent = int(((total_items := len(items))-count)/total_items*100)
					from modules.source_presentation import build_source_groups, format_source_size
					group = next(iter(build_source_groups([item], include_unchecked=True)), {})
					line1 = group.get('label', 'Opening source')
					line2 = ' · '.join(value for value in (format_source_size(item),
						' / '.join(group.get('video_badges', ())), ' / '.join(group.get('audio_badges', ()))) if value)
					if self.progress_dialog.full_screen:
						self.progress_dialog.update('[CR]'.join(filter(None, (line1, line2))), percent)
					else: self.bg.update(percent, self.meta.get('title') or 'POV')
				except: pass
				if _fast_to_resolve(item): sleep(500)
				try:
					if 'unrestricted_link' in item: link = item['unrestricted_link']
					else: link = Source(item, self.meta).resolve_sources()
				except Exception:
					if not self.autoplay: raise
					continue
				if link is not None: break
			else:
				if self.progress_dialog.full_screen: self.progress_dialog.kill()
				else: self.bg.close()
				return self._no_results()
			if not self.progress_dialog.full_screen: self.bg.close()
			if self.cancelled(): return
			resume_action = None
			# R10-playback: a Netflix-skin request (movie page too, not only the show page) carries the page's choice,
			# netflix_resume=resume|restart, and POV then never asks "Resume / Start Over" itself. Other callers: unchanged.
			if self.quiet or (self.mediatype == 'episode' and self.params.get('netflix_series') == 'true'):
				resume_action = self.params.get('netflix_resume')
				if resume_action not in ('resume', 'restart'): resume_action = None
			# R10-playback (review M3): past this point the request is no longer cancelled by Back; the loader says so.
			self.publish_play(Phase='play')
			def finish_loading():
				if progress_media: progress_media()
				self.clear_playback_cover()
			on_ready = finish_loading if self.params.get('netflix_playback_cover') else progress_media
			if self.params.get('netflix_series') != 'true' or self.flight is None:
				return POVPlayer().run(link, item.get('name'), self.meta, on_ready, resume_action)
			from modules.netflix_next_model import PreparedEpisode
			from modules.player import HANDOFF_DEFERRED, HANDOFF_FAILED

			owner = POVPlayer()
			next_episode = owner.run(link, item.get('name'), self.meta, on_ready, resume_action, self.flight)
			while isinstance(next_episode, PreparedEpisode):
				replacement = POVPlayer()
				outcome = replacement.run(next_episode.link, next_episode.filename, next_episode.meta,
				                          resume_action='resume', next_flight=self.flight, previous=owner)
				if outcome is HANDOFF_DEFERRED:
					owner.next_episode.restore(next_episode)
					next_episode = owner.watch()
				elif outcome is HANDOFF_FAILED:
					original = owner.next_episode
					if original.owns_playback():
						self.update_series_return(self.flight, 'video', original.identity)
						original.model.fail(next_episode.episode)
						next_episode = owner.watch()
					else:
						owner.finish_watch()
						return
				else:
					owner, next_episode = replacement, outcome
		except: pass

	@staticmethod
	def update_series_return(flight, field, identity):
		if not flight.owns_playback() or field not in ('video', 'episodes'): return False
		raw = get_property('Netflix.Exit.Return')
		try: target = json.loads(raw)
		except ValueError: return False
		if not isinstance(target, dict) or (target.get('kind'), target.get('ID'), target.get('play')) != ('episode', identity[0], flight.token):
			return False
		if get_property('Netflix.Exit.Return') != raw: return False
		target[field] = list(identity[1:])
		set_property('Netflix.Exit.Return', json.dumps(target))
		return True

class ConfigLoader:
	def _as_bool(self, value, default=False):
		if value in (None, ''): value = default
		if isinstance(value, bool): return value
		return str(value).lower() == 'true'

	def _as_int(self, value, default=''):
		if value in (None, ''): return default
		return int(value)

	def quality_filter(self, source):
		if not source.autoplay: setting = 'results_quality_%s' % source.mediatype
		else: setting = 'autoplay_quality_%s' % source.mediatype
		filter_list = quality_filter(setting)
		if source.include_prerelease_results and 'SD' in filter_list: filter_list += ['SCR', 'CAM', 'TELE']
		return filter_list

	def apply(self, source, params=None):
		if params: source.params = params
		params_get = source.params.get
		if params: source.prescrape = self._as_bool(params_get('prescrape'), True)
		source.background = self._as_bool(params_get('background'), False)
		source.quiet = bool(params_get('netflix_playback_cover')) and not source.background
		source.bg = quiet_progress if source.quiet else progressDialogBG
		if not ephemeral() and not params_get('netflix_quality_preview') and not source.quiet:
			if source.background: hide_busy_dialog()
			else: show_busy_dialog()
		source.disabled_ignored = self._as_bool(params_get('disabled_ignored'), False)
		source.ignore_scrape_filters = self._as_bool(params_get('ignore_scrape_filters'), False)
		source.tmdb_id = params_get('tmdb_id')
		source.season = self._as_int(params_get('season'), '')
		source.episode = self._as_int(params_get('episode'), '')
		source.custom_title = params_get('custom_title')
		source.custom_year = params_get('custom_year')
		source.custom_season = self._as_int(params_get('custom_season'), None)
		source.custom_episode = self._as_int(params_get('custom_episode'), None)
		source.mediatype = 'episode' if source.episode else 'movie'
		if 'autoplay' in source.params: source.autoplay = self._as_bool(params_get('autoplay'), False)
		else: source.autoplay = settings.auto_play(source.mediatype)
		# An internal-only prescan can find a usable source before the best one.
		if source.autoplay: source.prescrape = False
		quality = params_get('movie_quality')
		source.movie_quality = quality if quality in ('highest', 'medium', 'lowest') else 'play'
		source.active_internal_scrapers = settings.active_internal_scrapers()
		source.active_external = 'external' in source.active_internal_scrapers
		source.display_uncached_torrents = settings.display_uncached_torrents()
		source.ignore_results_filter = settings.ignore_results_filter()
		source.provider_sort_ranks = settings.provider_sort_ranks()
		source.scraper_settings = settings.scraping_settings()
		source.sort_function = settings.results_sort_order()
		source.filter_av1 = settings.filter_status('av1')
		source.filter_hevc = settings.filter_status('hevc')
		source.filter_hdr = settings.filter_status('hdr')
		source.filter_dv = settings.filter_status('dv')
		source.hybrid_allowed = source.filter_hdr in (0, 2)
		source.include_prerelease_results, source.include_3D_results = settings.include_prerelease_3d_results()
		source.quality_filter = self.quality_filter(source)
		source.limit_resolve = max(int(get_setting('limit_resolve', '10')), 1)
		if get_property('pov_total_autoplays') != '' or source.quiet: source.full_screen = False
		else: source.full_screen = get_setting('load_action') == '1'
		source.size_filter = int(get_setting('results.size_filter', '0'))
		source.include_unknown_size = self._as_bool(get_setting('results.include.unknown.size'), False)
		source.sleep_time = settings.display_sleep_time()
		source.timeout = int(get_setting('scrapers_timeout', '10'))
		if source.disabled_ignored: source.timeout = source.timeout * 2
		if self._as_bool(get_setting('results.language_filter'), False):
			source.priority_language = get_setting('results.language')
		else: source.priority_language = None

class DialogProgress:
	def __init__(self, full_screen=True):
		self.full_screen = full_screen
		self.dialog_progress = None

	def make(self, meta):
		self.dialog_progress = create_window(('windows.progress', 'ProgressMedia'), 'progress_media.xml', meta=meta)
		self.iscanceled = self.dialog_progress.iscanceled
		self.update = self.dialog_progress.update
		Thread(target=self.dialog_progress.run).start()

	def kill(self):
		progress = self.dialog_progress
		self.dialog_progress = None
		if progress is None: return
		try: progress.close()
		except (RuntimeError, AttributeError): pass

class MetaBuilder:
	def build(self, source):
		if 'meta' in source.params: meta = json.loads(source.params['meta'])
		else: meta = self.get_meta(source)
		for i in ('custom_title', 'custom_year', 'custom_season', 'custom_episode'):
			if getattr(source, i, False): meta[i] = getattr(source, i, '')
		expiry_times = get_cache_expiry(source.mediatype, meta, source.season)
		title = get_title(meta)
		aliases = self.make_alias_dict(meta, title)
		year = self.get_search_year(source, meta)
		ep_name = self.get_ep_name(meta)
		search_info = {
			'scrape_timeout': source.timeout, 'expiry_times': expiry_times,
			'mediatype': source.mediatype, 'imdb_id': meta.get('imdb_id'),
			'tmdb_id': source.tmdb_id, 'tvdb_id': meta.get('tvdb_id'),
			'title': title, 'aliases': aliases, 'year': year, 'ep_name': ep_name,
			'total_seasons': meta.get('total_seasons', ''),
			'season': source.custom_season or source.season,
			'episode': source.custom_episode or source.episode,
		}
		meta.update({
			'search_info': search_info, 'mediatype': source.mediatype,
			'season': source.season, 'episode': source.episode
		})
		return meta

	def get_meta(self, source):
		meta_user_info, current_date = settings.metadata_user_info(), get_datetime()
		if source.mediatype == 'episode':
			meta = tvshow_meta('tmdb_id', source.tmdb_id, meta_user_info, current_date)
			try:
				episodes_data = season_episodes_meta(source.season, meta, meta_user_info)
				ep_data = next((i for i in episodes_data if i['episode'] == int(source.episode)))
				meta.update({
					'mediatype': 'episode', 'season': ep_data['season'], 'episode': ep_data['episode'],
					'ep_name': ep_data['title'], 'premiered': ep_data['premiered'], 'plot': ep_data['plot']
				})
			except: pass
		else: meta = movie_meta('tmdb_id', source.tmdb_id, meta_user_info, current_date)
		return meta

	def make_alias_dict(self, meta, title):
		seen = set()
		country_codes = (i.replace('GB', 'UK') for i in meta.get('country_codes', []))
		country_codes = tuple(dict.fromkeys(country_codes))
		raw_titles = (meta['title'], meta['original_title'], *meta.get('alternative_titles', []))
		raw_titles = tuple(dict.fromkeys(raw_titles))
		aliases = [{'title': i, 'country': ''} for i in raw_titles]
		aliases.extend({'title': '%s %s' % (title, i), 'country': ''} for i in country_codes)
		valid_titles = (i for i in aliases if i['title'] != title and safe_string(i['title']).strip())
		aliases = [i for i in valid_titles if i['title'] not in seen and not seen.add(i['title'])]
		return aliases

	def get_search_year(self, source, meta):
		if 'custom_year' in meta: return meta['custom_year']
		year = meta.get('year') or '0'
		return year

	def get_ep_name(self, meta):
		if meta.get('mediatype') == 'episode':
			ep_name = meta.get('ep_name') or ''
			try: ep_name = safe_string(ep_name)
			except: pass
		else: ep_name = None
		return ep_name

class ScraperProcessor:
	def __init__(self, source_instance):
		self.source = source_instance

	def prepare_internal(self, prescrape=False):
		if self.source.active_external and ''.join(self.source.active_internal_scrapers) == 'external': return
		if not prescrape and 'aiostreams' in self.source.active_internal_scrapers:
			self.source.active_internal_scrapers = ['aiostreams']
		active_internal_scrapers = [i for i in self.source.active_internal_scrapers if i not in self.source.remove_scrapers]
		self.source.internal_scraper_names = active_internal_scrapers[:]
		self.source.active_internal_scrapers = active_internal_scrapers

	def activate_internal(self, prescrape=False):
		self.prepare_internal(prescrape)
		append = self.source.prescrape_scrapers.append if prescrape else self.source.providers.append
		source_path = kodi_utils.translate_path(kodi_utils.internal_path)
		for loader, module_name, is_pkg in pkgutil.iter_modules([source_path]):
			if is_pkg: continue
			if module_name not in self.source.active_internal_scrapers: continue
			if prescrape and not check_prescrape_sources(module_name, self.source.mediatype): continue
			try: module_source = importlib.import_module('.' + module_name, package='debrids').source
			except Exception as e: kodi_utils.logger('POV', 'Error: Loading module: "%s": %s' % (module_name, e))
			else: append(('internal', module_source, module_name))

	def activate_external(self):
		self.source.external_providers = []
		# R10.43 (owner, 17 Nov): the Next Episode preparation searches the same external scrapers as Play (for most
		# setups Torrentio etc. are the only sources). Its links still never reach disk: ExternalManager.results binds the
		# scope in its pool workers, so ExternalProvidersCache/MainCache skip writes and kodi_utils stays quiet there.
		if 'aiostreams' in self.source.active_internal_scrapers: return
		self.source.debrid_enabled = debrid_enabled()
		self.source.debrid_torrent_enabled = debrid_type_enabled('torrent', self.source.debrid_enabled)
		if not self.source.debrid_torrent_enabled:
			self.source.progress_dialog.kill()
			self.source.active_external = False
			if not self.source.params.get('netflix_quality_preview') and ''.join(self.source.active_internal_scrapers) == 'external':
				notification(32130)
			return
#		if not self.source.debrid_torrent_enabled: self.source.exclude_list.extend(scraper_names('torrents'))
		external_providers = pov_sources(ret_all=self.source.disabled_ignored)
		self.source.external_providers.extend([
			i for i in external_providers if i[0] not in self.source.exclude_list
			and (i[1].hasEpisodes if self.source.mediatype == 'episode' else i[1].hasMovies)
		])
		if self.source.mediatype != 'episode': return
		self.source.external_providers = [(i[0], i[1], '') for i in self.source.external_providers]
		season_packs, show_packs = pack_enable_check(self.source.meta, self.source.season, self.source.episode)
		if not season_packs: return
		pack_capable = [i for i in self.source.external_providers if i[1].pack_capable]
		if pack_capable:
			self.source.external_providers.extend([(i[0], i[1], season_display) for i in pack_capable])
		if pack_capable and show_packs:
			self.source.external_providers.extend([(i[0], i[1], show_display) for i in pack_capable])

class ResultsProcessor:
	def __init__(self, source_instance):
		self.source = source_instance

	def process_internal_results(self):
		for i in self.source.internal_scrapers:
			win_property = get_property('%s.internal_results' % i)
			if win_property in ('checked', '', None): continue
			try: sources = json.loads(win_property)
			except: continue
			set_property('%s.internal_results' % i, 'checked')
			for k in self.source.internal_resolutions:
				self.source.internal_resolutions[k] += sources.get(k, 0)

	def process(self, results):
		if self.source.prescrape: self.source.all_scrapers = self.source.active_internal_scrapers
		elif 'aiostreams' in self.source.active_internal_scrapers: return results
		else: self.source.all_scrapers = list({*self.source.active_internal_scrapers, *self.source.remove_scrapers})
		if self.source.ignore_scrape_filters:
			self.source.filters_ignored = True
			results = self.sort_results(results)
			results = self.sort_first(results)
		else:
			results = self.filter_results(results)
			results = self.sort_results(results)
			results = self.special_filter(results, hevc_filter_key, self.source.filter_hevc)
			results = self.special_filter(results, hdr_filter_key, self.source.filter_hdr)
			results = self.special_filter(results, dolby_vision_filter_key, self.source.filter_dv)
			results = self.special_filter(results, av1_filter_key, self.source.filter_av1)
			results = self.sort_first(results)
		return results

	def filter_results(self, results):
		results = [i for i in results if i['quality'] in self.source.quality_filter]
		if not self.source.include_3D_results:
			results = [i for i in results if '3D' not in i['extraInfo']]
		if not self.source.size_filter:
			return results
		if self.source.size_filter == 1:
			duration = self.source.meta['duration'] or (3600 if self.source.mediatype == 'episode' else 5400)
			max_size = string_to_float(get_setting('results.size.speed', '20'), '20')
			max_size = ((0.125 * (0.90 * max_size)) * duration)/1000
		if self.source.size_filter == 2:
			max_size = string_to_float(get_setting('results.size.file', '10000'), '10000') / 1000
		if self.source.include_unknown_size:
			results = [i for i in results if i['scrape_provider'].startswith('folder') or i['size'] <= max_size]
		else: results = [
			i for i in results if i['scrape_provider'].startswith('folder') or 0.01 < i['size'] <= max_size
		]
		return results

	def sort_results(self, results):
		for item in results:
			provider, quality = item['scrape_provider'], item.get('quality', 'SD')
			account_type = item['debrid'].lower() if provider == 'external' else provider.lower()
			item['provider_rank'] = self.get_provider_rank(account_type)
			item['quality_rank'] = self.get_quality_rank(quality)
		results.sort(key=self.source.sort_function)
		if self.source.priority_language:
			results = self.sort_language_to_top(results)
		results = self.sort_uncached_torrents(results)
		clear_property('fs_filterless_search')
		return results

	def get_provider_rank(self, account_type):
		return self.source.provider_sort_ranks[account_type] or 11

	def get_quality_rank(self, quality):
		return quality_ranks[quality]

	def sort_language_to_top(self, results):
		from modules.meta_lists import meta_languages
		try:
			tokens = meta_languages[self.source.priority_language].values()
			tokens = [re.sub(r'\W', '', self.source.priority_language), *tokens]
			if 'Spanish' in self.source.priority_language: tokens += 'latino', 'lat', 'esp'
			if 'Brazil' in self.source.priority_language: tokens += 'portuguese', 'por', 'pt'
			pattern = re.compile(r'\b(%s)\b' % '|'.join(i for i in tokens if i), re.I)
			sort_first = [i for i in results if pattern.search(i.get('name_info', ''))]
			sort_last = [i for i in results if i not in sort_first]
			results = sort_first + sort_last
		except: pass
		return results

	def sort_uncached_torrents(self, results):
		results.sort(key=lambda k: 'Unchecked' in k.get('cache_provider', ''), reverse=False)
		if self.source.background or self.source.autoplay:
			return [i for i in results if 'Uncached' not in i.get('cache_provider', '')]
		if self.source.display_uncached_torrents or get_property('fs_filterless_search') == 'true':
			return sorted(results, key=lambda k: 'Uncached' in k.get('cache_provider', ''), reverse=False)
		return [i for i in results if 'Uncached' not in i.get('cache_provider', '')]

	def special_filter(self, results, key, enable_setting):
		if enable_setting == 1:
			if key == dolby_vision_filter_key and self.source.hybrid_allowed:
				results = [
					i for i in results if all(x in i['extraInfo']
					for x in (key, hdr_filter_key)) or key not in i['extraInfo']
				]
			else: results = [i for i in results if key not in i['extraInfo']]
		elif enable_setting == 2 and self.source.autoplay:
			priority_list = [i for i in results if key in i['extraInfo']]
			remainder_list = [i for i in results if i not in priority_list]
			results = priority_list + remainder_list
		elif enable_setting == 3:
			results.sort(
				key=lambda k: key in k['extraInfo'] and 'Uncached' not in k.get('cache_provider', ''),
				reverse=True
			)
		return results

	def sort_first(self, results):
		try:
			sort_first_scrapers = []
			sort_first_scrapers.extend([i for i in self.source.all_scrapers if i in cloud_scrapers and sort_to_top(i)])
			if not sort_first_scrapers: return results
			sort_first = [i for i in results if i['scrape_provider'] in sort_first_scrapers]
			sort_first.sort(key=lambda k: k['quality_rank'])
			sort_last = [i for i in results if i not in sort_first]
			results = sort_first + sort_last
		except: pass
		return results

class ExternalManager:
	def dialog_hook(function):
		def wrapper(instance, *args, **kwargs):
			if not instance.background:
				if instance.internal_activated or instance.internal_prescraped:
					instance.string3 = int_format % (instance.int_dialog_highlight, '%s')
					instance.string4 = ext_format % (instance.ext_dialog_highlight, '%s')
				else:
					instance.string3 = ''
					instance.string4 = ext_scr_format % (instance.ext_dialog_highlight, ls(32138))
				hide_busy_dialog()
				if not instance.progress_dialog.full_screen:
					instance.bg.create('POV', 'POV loading...')
				else: instance.progress_dialog.make(instance.meta)
			result = function(instance, *args, **kwargs)
			if not instance.background:
				if not instance.progress_dialog.full_screen:
					instance.bg.close()
				else: instance.progress_dialog.kill()
			return result
		return wrapper

	def __init__(self, source_instance):
		self.meta, self.background = source_instance.meta, source_instance.background
		self.bg = getattr(source_instance, 'bg', progressDialogBG)
		self.cancelled = getattr(source_instance, 'cancelled', lambda: False)
		self.quiet_busy = getattr(source_instance, 'quiet_busy', lambda: None)
		self.external_providers, self.debrid_torrents = source_instance.external_providers, source_instance.debrid_torrent_enabled
		self.internal_scrapers, self.prescrape_sources = source_instance.threads, source_instance.prescrape_sources
		self.progress_dialog, self.disabled_ignored = source_instance.progress_dialog, source_instance.disabled_ignored
		self.sleep_time, self.timeout = source_instance.sleep_time, source_instance.timeout
		self.internal_activated = len(self.internal_scrapers) > 0
		self.internal_prescraped = len(self.prescrape_sources) > 0
		self.processed_prescrape = False
		self.processed_internal_scrapers = []
		self.processed_internal_scrapers_append = self.processed_internal_scrapers.append
		self.hostDict, self.sources, self.final_sources = [], [], []
		self.int_dialog_highlight = get_setting('int_dialog_highlight', 'dodgerblue')
		self.ext_dialog_highlight = get_setting('ext_dialog_highlight', 'magenta')
		self.int_total = total_format % (self.int_dialog_highlight, '%s')
		self.ext_total = total_format % (self.ext_dialog_highlight, '%s')
		self.internal_resolutions = dict.fromkeys(resolutions.split(), 0)
		self.resolutions = dict.fromkeys(resolutions.split(), 0)

	@dialog_hook
	def results(self, info):
		tpe = TPE(max(1, len(self.external_providers), len(self.debrid_torrents)))
		# R10.43: stdlib pool workers don't inherit the preparation scope (threading.local), so bind it in each job;
		# otherwise Next Episode prep would write provider links/hashes to disk and log from the workers.
		scope = current()
		def scoped(fn):
			if scope is None: return fn
			def run(*args):
				with bind(scope):
					if scope.cancelled.is_set(): return []
					return fn(*args)
			return run
		try:
			threads = set()
			total_results = []
			for provider, module, *pack in self.external_providers:
				args = (provider, module, *pack) if pack else (provider, module)
				fut = tpe.submit(scoped(ExternalSource(self.meta, self.resolutions).results), info, args)
				fut.name = pack_display % (provider, *pack) if pack and pack[0] else provider
				threads.add(fut)
			self.monitor(threads, remaining_format, False)
			threads = [i for i in threads if i.done() and not i.exception()]
			for fut in as_completed(threads): total_results.extend(fut.result())
			self.sources.extend(self.process_duplicates(total_results))
			torrent_sources = [i for i in self.sources if 'torrent' in i['source']]
			result_hashes = list({i['hash'] for i in torrent_sources})
			DebridCheck.set_cached_hashes(result_hashes)
			threads = set()
			for item in self.debrid_torrents:
				check = DebridCheck(self.meta, item)
				if scope is not None: check.cache_write = lambda hashes: None  # prep keeps debrid hashes in memory only
				fut = tpe.submit(scoped(check.cache_check))
				fut.name = item
				threads.add(fut)
			self.monitor(threads, debrid_format, True)
			threads = [i for i in threads if i.done() and not i.exception()]
			for name, hashes in ((fut.name, fut.result()) for fut in threads):
				if name in ('realdebrid', 'alldebrid'): uncached = '%s %s' % ('Unchecked', name)
				else: uncached = '%s %s' % ('Uncached', name)
				self.final_sources.extend(
					{**i, 'cache_provider': name if i['hash'] in hashes else uncached, 'debrid': name}
					for i in torrent_sources
				)
		except: kodi_utils.notify_error()
		finally:
			if scope is None: tpe.shutdown(False)
			else: scope.retain_closer(lambda: tpe.shutdown(wait=True, cancel_futures=True))  # drain waits for pool workers
		return self.final_sources

	def monitor(self, threads, status_line='', debrid=False):
		len_threads = len(threads)
		end_time = time.monotonic() + self.timeout + 1
		while not monitor.abortRequested() and time.monotonic() <= end_time:
			alive_threads = [x.name for x in threads if not x.done()]
			if not debrid: alive_threads.extend(x.name for x in self.internal_scrapers if x.is_alive())
			if not alive_threads or self.cancelled(): break
			self.quiet_busy()
			if not self.background: self.progress_update(alive_threads, len_threads, status_line)
			sleep(self.sleep_time)

	def progress_update(self, alive_threads, len_threads, string1):
		try:
			ext_totals = [self.ext_total % v for v in self.resolutions.values()]
			len_alive_threads = len(alive_threads)
			progress = int((len_threads-len_alive_threads)/len_threads*100)
			if self.internal_activated or self.internal_prescraped:
				self.process_internal_results()
				int_totals = [self.int_total % v for v in self.internal_resolutions.values()]
				line1 = self.string3 % diag_format % tuple(int_totals)
				line2 = self.string4 % diag_format % tuple(ext_totals)
			else:
				line1 = self.string4
				line2 = diag_format % tuple(ext_totals)
			if len_alive_threads > 5: line3 = string1 % str(len_threads-len_alive_threads)
			else: line3 = string1 % ', '.join(alive_threads).upper()
			if self.progress_dialog.full_screen:
				self.progress_dialog.update(format_line % (line1, line2, line3), progress)
			else: self.bg.update(progress, line3)
		except: pass

	def process_duplicates(self, sources):
		uniqueURLs, uniqueHashes = set(), set()
		for provider in sources:
			try:
				url = provider['url'].lower()
				if url in uniqueURLs: continue
				uniqueURLs.add(url)
				if 'hash' in provider:
					_hash = provider['hash'].lower()
					if _hash in uniqueHashes: continue
					uniqueHashes.add(_hash)
					yield provider
				else: yield provider
			except: yield provider

	def process_internal_results(self):
		def _process_quality_count(sources):
			for k in self.internal_resolutions: self.internal_resolutions[k] += sources.get(k, 0)
		if self.internal_prescraped and not self.processed_prescrape:
			_process_quality_count(sources_quality_count(self.prescrape_sources))
			self.processed_prescrape = True
		for i in self.internal_scrapers:
			win_property = get_property('%s.internal_results' % i.name)
			if win_property in ('checked', '', None): continue
			try: internal_sources = json.loads(win_property)
			except: continue
			set_property('%s.internal_results' % i.name, 'checked')
			self.processed_internal_scrapers_append(i.name)
			_process_quality_count(internal_sources)
		return [i.name for i in self.internal_scrapers if i.name not in self.processed_internal_scrapers]

class ExternalSource:
	scrape_provider = 'external'
	timeout = 10
	hostDict = {}

	def __init__(self, meta, resolutions=None):
		self.sources = []
		self.meta = meta
		if isinstance(resolutions, dict): self.resolutions = resolutions
		else: self.resolutions = dict.fromkeys(resolutions.split(), 0)

	def results(self, info, args):
		try:
			self.mediatype, self.tmdb_id  = info['mediatype'], str(info['tmdb_id'])
			self.year, aliases = info['year'], info['aliases']
			self.season, self.episode = info['season'], info['episode']
			self.total_seasons = info['total_seasons']
			self.title, self.orig_title = safe_string(info['title']), info['title']
			self.single_expiry, self.season_expiry, self.show_expiry = info['expiry_times']
			if 'scrape_timeout' in info: self.timeout = info['scrape_timeout']
			if self.mediatype == 'episode':
				season_divider = (
					i['episode_count'] for i in self.meta['season_data']
					if int(i['season_number']) == int(self.meta['season'])
				)
				self.season_divider = int(next(season_divider, 1))
				self.show_divider = int(self.meta['total_aired_eps'])
				self.data = {
					'timeout': self.timeout, 'imdb': info['imdb_id'], 'tvdb': info['tvdb_id'],
					'title': safe_string(info['ep_name']), 'tvshowtitle': self.title, 'year': self.year,
					'season': str(self.season), 'episode': str(self.episode),
					'total_seasons': self.total_seasons, 'aliases': aliases
				}
				self.get_episode_source(*args)
			else:
				self.season_divider, self.show_divider, self.data = 1, 1, {
					'timeout': self.timeout, 'imdb': info['imdb_id'],
					'title': self.title, 'year': self.year, 'aliases': aliases
				}
				self.get_movie_source(*args)
		except: pass
		return self.sources

	def get_movie_source(self, provider, module):
		epc = ExternalProvidersCache()
		sources = epc.get(provider, self.mediatype, self.tmdb_id, self.title, self.year, '', '')
		if sources is None:
			sources = module().sources(self.data, self.hostDict)
			sources = self.process_sources(provider, sources)
			epc.set(provider, self.mediatype, self.tmdb_id, self.title, self.year, '', '', sources, self.single_expiry)
		if sources:
			self.sources.extend(sources)

	def get_episode_source(self, provider, module, pack):
		if pack in pack_check: s_check, e_check = '' if pack == show_display else self.season, ''
		else: s_check, e_check = self.season, self.episode
		epc = ExternalProvidersCache()
		sources = epc.get(provider, self.mediatype, self.tmdb_id, self.title, self.year, s_check, e_check)
		if sources is None:
			if pack == show_display:
				expiry_hours = self.show_expiry
				sources = module().sources_packs(self.data, self.hostDict, search_series=True, total_seasons=self.total_seasons)
			elif pack == season_display:
				expiry_hours = self.season_expiry
				sources = module().sources_packs(self.data, self.hostDict)
			else:
				expiry_hours = self.single_expiry
				sources = module().sources(self.data, self.hostDict)
			sources = self.process_sources(provider, sources)
			epc.set(provider, self.mediatype, self.tmdb_id, self.title, self.year, s_check, e_check, sources, expiry_hours)
		if sources:
			if pack == season_display: sources = [i for i in sources if 'episode_start' not in i or i['episode_start'] <= self.episode <= i['episode_end']]
			elif pack == show_display: sources = [i for i in sources if i['last_season'] >= self.season]
			self.sources.extend(sources)

	def process_sources(self, provider, sources):
		for i in sources:
			try:
				i_get = i.get
				if 'hash' in i: i['hash'] = str(i['hash']).lower()
				display_name = source_utils.clean_file_name(i_get('name')).replace('html', ' ')
				quality, extraInfo = get_file_info(name_info=i_get('name_info'))
				size, size_label, divider = 0, None, None
				try:
					size = i_get('size')
					if 'package' in i and not i_get('true_size', False):
						if i_get('package') == 'season': divider = self.season_divider
						else: divider = self.show_divider
						size = float(size) / divider
						size_label = '%.2f GB' % size
					else: size_label = '%.2f GB' % size
				except: pass
				i.update({
					'external': True, 'scrape_provider': self.scrape_provider,
					'provider': provider, 'display_name': display_name,
					'extraInfo': extraInfo, 'quality': quality,
					'size_label': size_label, 'size': round(size, 2)
				})
				if quality not in self.resolutions: self.resolutions['SD'] += 1
				else: self.resolutions[quality] += 1
				self.resolutions['total'] += 1
			except: pass
		return sources

