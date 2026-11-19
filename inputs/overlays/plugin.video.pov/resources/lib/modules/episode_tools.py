import json
from random import choice
from threading import Thread
from uuid import uuid4
from indexers.metadata import all_episodes_meta, season_episodes_meta, tvshow_meta
from modules import kodi_utils, settings
from modules.sources import Sources
from modules.utils import adjust_premiered_date, get_datetime, get_next_episode_pointer
from windows import open_window
# from modules.kodi_utils import logger

ls, build_url = kodi_utils.local_string, kodi_utils.build_url
nores_str = '[B]%s[/B] %s' % (ls(32484), ls(32573))

def SmartPlay(params):
	tmdb_id = params.get('tmdb_id')
	if not tmdb_id: return kodi_utils.notify_error()
	from caches.watched_cache import get_next_episodes
	meta_user_info, current_date = settings.metadata_user_info(), get_datetime()
	watched_info = get_next_episodes(settings.watched_indicators())
	watched_info = next((i for i in watched_info if i['media_ids']['tmdb'] == str(tmdb_id)), None)
	if watched_info: season, episode = int(watched_info['season']), int(watched_info['episode'])
	else: season, episode = 1, 0
	meta = tvshow_meta('tmdb_id', tmdb_id, meta_user_info, current_date)
	meta.update({'season': season, 'episode': episode})
	nextep_meta, nextep_params = nextep_playback_info(meta)
	if nextep_params == 'error': return kodi_utils.notify_error()
	if nextep_params == 'no_next_episode': return kodi_utils.notification(nores_str)
	Sources.factory(nextep_params)

def get_random_episode(tmdb_id, continual=False):
	meta_user_info, adjust_hours, current_date = settings.metadata_user_info(), settings.date_offset(), get_datetime()
	tmdb_key = str(tmdb_id)
	meta = tvshow_meta('tmdb_id', tmdb_id, meta_user_info, current_date)
	try:
		all_episodes = all_episodes_meta(meta, meta_user_info, Thread)
		episodes_data = []
		for ep in all_episodes:
			if ep.get('season') == 0 or not ep.get('premiered'): continue
			episode_date, premiered = adjust_premiered_date(ep['premiered'], adjust_hours)
			if episode_date <= current_date: episodes_data.append(ep)
	except: episodes_data = []
	if not episodes_data: return None
	episode_history = {}
	episode_list = []
	if continual:
		try:
			episode_history = json.loads(kodi_utils.get_property('pov_random_episode_history'))
			if tmdb_key in episode_history: episode_list = episode_history[tmdb_key]
		except: pass
		episodes_data = [i for i in episodes_data if i not in episode_list] or episodes_data
	chosen_episode = choice(episodes_data)
	if continual:
		episode_list.append(chosen_episode)
		episode_history[tmdb_key] = episode_list
		kodi_utils.set_property('pov_random_episode_history', json.dumps(episode_history))
	title, season, episode = meta['title'], int(chosen_episode['season']), int(chosen_episode['episode'])
	query = '%s S%.2dE%.2d' % (title, season, episode)
	display_name = '%s - %dx%.2d' % (title, season, episode)
	ep_name, plot = chosen_episode['title'], chosen_episode['plot']
	try: premiered = adjust_premiered_date(chosen_episode['premiered'], adjust_hours)[1]
	except: premiered = chosen_episode['premiered']
	meta.update({
		'mediatype': 'episode', 'rootname': display_name, 'season': season, 'episode': episode,
		'premiered': premiered, 'ep_name': ep_name, 'plot': plot
	})
	if continual: meta['random_continual'] = 'true'
	else: meta['random'] = 'true'
	url_params = {
		'mode': 'play_media', 'mediatype': 'episode', 'autoplay': 'true', 'season': season, 'episode': episode,
		'tmdb_id': meta['tmdb_id'], 'tvshowtitle': meta['rootname'], 'query': query, 'meta': json.dumps(meta)
	}
	return meta, url_params

def nextep_playback_info(meta, *, next_runtime=False):
	adjust_hours, current_date = settings.date_offset(), get_datetime()
	meta_get = meta.get
	try:
		sn, en = int(meta_get('season')), int(meta_get('episode'))
		season, episode, new_season = get_next_episode_pointer(meta, sn, en)
		if new_season and season > meta_get('total_seasons'): return meta, 'no_next_episode'
		ep_data = season_episodes_meta(season, meta, settings.metadata_user_info())
		try: ep_data = next((i for i in ep_data if i['episode'] == episode))
		except: return meta, 'no_next_episode'
		episode_date, premiered = adjust_premiered_date(ep_data['premiered'], adjust_hours)
		if current_date < episode_date: return meta, 'no_next_episode'
		custom_title = meta_get('custom_title')
		title = custom_title or meta_get('title')
		display_name = '%s - %dx%.2d' % (title, int(season), int(episode))
		meta.update({
			'mediatype': 'episode', 'rootname': display_name, 'season': season, 'episode': episode,
			'premiered': premiered, 'ep_name': ep_data['title'], 'plot': ep_data['plot']
		})
		if next_runtime: meta['duration'] = ep_data.get('duration') or 0
		url_params = {
			'mode': 'play_media', 'mediatype': 'episode', 'season': season, 'episode': episode,
			'tmdb_id': meta_get('tmdb_id'), 'tvshowtitle': meta_get('rootname')
		}
		if custom_title: url_params['custom_title'] = custom_title
	except: url_params = 'error'
	return meta, url_params

def execute_skip_intro(player, meta):
	# R10.3 (owner, 13 Nov): only the pop-up. It opens by itself once per pass through the intro, when the video plays
	# with no OSD and no other dialog up (so it waits for the play-bar / scrubber to close), and one OK skips. Leaving the
	# intro (a seek out, or the end) re-arms it. The token keeps one owner per playback (a new episode ends this loop).
	intro_start, intro_end = player.intro
	if not 0 <= intro_start < intro_end: return
	owner = 'Netflix.SkipIntro.Owner'
	token = uuid4().hex
	try: playing_file = player.getPlayingFile()
	except RuntimeError: return
	if not playing_file: return

	def same_playback():
		return (kodi_utils.get_property(owner) == token and player.isPlayingVideo()
		        and player.getPlayingFile() == playing_file)

	offered = False
	kodi_utils.set_property(owner, token)
	try:
		while same_playback():
			current_time = player.getTime()
			inside = intro_start <= current_time < intro_end
			if not inside:
				offered = False
			elif not offered and intro_end - current_time >= 2 and not kodi_utils.get_visibility(
			        'Player.Paused | Window.IsActive(videoosd) | System.HasActiveModalDialog'):
				# (review L2) never with under 2 s of intro left: the pop-up would sit over the episode for 20 s
				offered = True
				skip = _continue_action(True, meta, 'skip_intro') is True
				if skip and same_playback() and intro_start <= player.getTime() < intro_end:
					player.seekTime(intro_end)
			if player.monitor.waitForAbort(0.25 if inside else 0.75 if current_time < intro_start else 1.5):
				break
	except RuntimeError:
		# Kodi can close the player between an identity check and a time read.
		pass
	finally:
		if kodi_utils.get_property(owner) == token:
			kodi_utils.clear_property(owner)

def execute_scrape_nextep(player, meta):
	nextep_meta, nextep_params = nextep_playback_info(meta)
	if nextep_params == 'error': return kodi_utils.notify_error()
	if nextep_params == 'no_next_episode': return
	if not Sources.background_prep(nextep_params): return kodi_utils.notification(nores_str)
	Sources.nextep_callback(nextep_params)
	action = _continue_action(True, nextep_meta)
	if action == 'cancel':
		Sources.nextep_params.clear()
		return kodi_utils.notification(32736)
	if action == 'play': player.stop()

def execute_nextep(player, meta, nextep_settings):
	if 'random_continual' in meta: nextep_meta, nextep_params = get_random_episode(meta['tmdb_id'], True)
	else: nextep_meta, nextep_params = nextep_playback_info(meta)
	if nextep_params == 'error': return kodi_utils.notify_error()
	if nextep_params == 'no_next_episode': return
	if not Sources.background_prep(nextep_params): return kodi_utils.notification(nores_str)
	Sources.nextep_callback(nextep_params)
	action = _control_playback(player, nextep_settings, nextep_meta)
	if action == 'cancel':
		Sources.nextep_params.clear()
		kodi_utils.clear_property('pov_total_autoplays')
		return kodi_utils.notification(32736)
	if action == 'close':
		if nextep_settings['run_popup']: return
		text = '[B]%s[/B] %s S%02dE%02d' % (ls(32484), nextep_meta['title'], nextep_meta['season'], nextep_meta['episode'])
		kodi_utils.notification(text, 6500, nextep_meta['poster'])
	if action == 'play': player.stop()

def _continue_action(run_popup, nextep_meta, function='next_ep'):
	if not run_popup: return 'close'
	return open_window(('windows.episodes', 'NextEpisode'), 'episodes.xml', meta=nextep_meta, function=function)

def _confirm_threshold(nextep_settings, nextep_meta):
	nextep_threshold = nextep_settings['threshold']
	if nextep_threshold == 0: return True
	try: current_number = int(kodi_utils.get_property('pov_total_autoplays'))
	except: current_number = 1
	if current_number < nextep_threshold:
		current_number += 1
		kodi_utils.set_property('pov_total_autoplays', str(current_number))
		return True
	if _continue_action(True, nextep_meta, 'confirm'):
		current_number = 1
		kodi_utils.set_property('pov_total_autoplays', str(current_number))
		return True
	return False

def _control_playback(player, nextep_settings, nextep_meta):
	confirm_threshold = False
	final_action = 'cancel'
	run_popup = nextep_settings['run_popup']
	display_nextep_popup = nextep_settings['window_time']
	nextep_threshold_check = nextep_settings['threshold_check']
	while player.isPlayingVideo():
		try:
			total_time = player.getTotalTime()
			curr_time = player.getTime()
			remaining_time = round(total_time - curr_time)
			if remaining_time <= nextep_threshold_check:
				if not confirm_threshold:
					confirm_threshold = _confirm_threshold(nextep_settings, nextep_meta)
					if not confirm_threshold:
						final_action = 'cancel'
						break
			if remaining_time <= display_nextep_popup:
				final_action = _continue_action(run_popup, nextep_meta)
				break
			kodi_utils.sleep(200)
		except: pass
	return final_action

