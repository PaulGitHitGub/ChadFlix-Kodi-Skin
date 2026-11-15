"""Preview a movie's playback choices without resolving or starting a stream.

The details page runs prefetch() once per movie (R9: a named alarm starts it a second after the page, as the single
launch in flight; it stops when its page goes away). It searches every enabled provider in the background, publishes the
quality tiers as soon as their availability is known (QualityState: checking -> partial -> ready), stops waiting for
straggler providers for the panel after a short cut-off, and keeps the finished search per movie so that a reopen and
Play reuse it. R10.25 (security audit F-07): the file on disk holds only the panel (no source, so no link); the sources
stay in Kodi's memory (a Home window property per movie, never written to disk) for RESULT_TTL, tied to the Kodi profile
and the signed-in source accounts. Files past RESULT_TTL are deleted at Kodi start, at every check and when read.
"""
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from queue import Empty, Queue
from tempfile import NamedTemporaryFile
from time import monotonic, time

import xbmc

from modules import kodi_utils
from modules.source_presentation import (available_sources, build_source_groups, format_source_size, play_facts,
                                         rank_playback_sources)

PREFIX = 'Netflix.Movie.Quality'
TIERS = ('highest', 'medium', 'lowest')
RESULT_TTL = 600        # seconds a finished search is reused for the same movie (reopen, Play, All Sources)
CACHE_ENTRIES = 24      # finished searches kept on disk per Kodi profile; the oldest go first
PREVIEW_CUTOFF = 4.0    # the panel stops waiting for straggler providers this long after the search starts,
PREVIEW_GRACE = 1.5     # or this long after the first provider returned results, whichever comes first
CACHE_WAVES = 4         # most debrid availability checks started while providers are still answering
STRAGGLER_CAP = 30.0    # longest the route holds its flight entry for threads that will not end (see finish)
POLL = .1
CACHE_DIR = 'special://profile/addon_data/plugin.video.pov/quality_preview/'
# R10.25 (F-07): the sources of a finished search (they can carry playable, already resolved links) are kept only here, in
# Kodi's memory: one Home window property per movie, plus an index for pruning. Never written to disk; gone when Kodi stops.
MEMORY = 'POV.QualityPreview.'
MEMORY_INDEX = MEMORY + 'Index'
MEMORY_ENTRIES = 6      # movies whose sources are held in memory at once (newest first), each for at most RESULT_TTL
LEGACY_MARK = '.no-links-v1'   # in CACHE_DIR: the files older versions wrote (with links) were removed from this folder
# Whose sources they are: the Kodi profile and the signed-in source accounts (ids only; hashed, never stored or logged).
ACCOUNT_IDS = ('rd.account_id', 'pm.account_id', 'ad.account_id', 'tb.account_id', 'oc.account_id', 'easynews_user', 'aio.username')
SETTINGS_FILE = 'special://profile/addon_data/plugin.video.pov/settings.xml'
EMPTY_VIEW = {'labels': {}, 'lines': {}, 'total': 0}
# R10-playback: the Highest tier's facts for the play loader, Netflix.Movie.QualityHighest{Res,SpecA,SpecB}
# (resolution, picture + codec + Blu-Ray/Web, sound + channels + size). Kept in the view, so a reopen fills them too.
FACT_FIELDS = ('Res', 'SpecA', 'SpecB')
# R9 single flight: ONE property names the one POV launch from the movie page that is in flight, for all movies:
#   "<kind>|<movie id>|<page token>|<due or start, epoch>|<valid until, epoch>"
#   pending  the skin set the alarm for this check     check  this route is running (renewed about once a second)
#   done     it has ended, its interpreter is leaving   play   the skin has just launched Play
# Nothing else starts while the value is valid; a crashed owner frees it by not renewing it. Skin side:
# extras/netflix_movie_details.py (flight, schedule_quality, settle_quality).
FLIGHT = 'Netflix.Flight'
SETUP_GRACE = 20.0      # first validity of "check": imports and metadata, before the search loop renews it
EXIT_ROOM = 0.8         # validity of "done": nothing is launched this close to the interpreter's exit
BEAT = 10.0             # the search loop renews "check" about once a second for this long (one slow step fits)
_MOVIE_ID = re.compile(r'\d{1,12}')
# Settings that change without changing what a search finds (sign-in refreshes, bookkeeping) are left out of the key.
_VOLATILE = re.compile(r'token|secret|expir|refresh|client|passw|user|account|auth|api_?key|\.key$|last|time|date|version|sync',
                       re.I)
_UNSAFE_NAME = re.compile(r'[^\w .()+-]')


def log(text):
    xbmc.log('POV-QUALITY ' + text, xbmc.LOGINFO)


def _ms(seconds):
    return '-' if seconds is None else '%d' % (seconds * 1000)


# Finished searches, one file per movie under the current Kodi profile.

def cache_dir():
    return Path(kodi_utils.translate_path(CACHE_DIR))


def _settings_stamp():
    # POV's settings file changes when a provider, filter or account setting changes; only its time is read.
    try:
        return os.stat(kodi_utils.translate_path(SETTINGS_FILE)).st_mtime_ns
    except (OSError, TypeError, ValueError):
        return None


def _settings_key():
    """A short hash over POV's settings that can change a search (everything except _VOLATILE ids), or None when
    POV's settings table is not available. Only the hash is kept; no value is logged or stored."""
    try:
        kodi_utils.get_setting('scrapers_timeout')            # makes POV load its settings table
        table = kodi_utils.manager._cache
        if not isinstance(table, dict) or not table:
            return None
        items = sorted((str(key), str(value)) for key, value in table.items() if not _VOLATILE.search(str(key)))
        return hashlib.sha256(repr(items).encode('utf-8', 'replace')).hexdigest()[:20]
    except Exception:
        return None


def _settings_match(record):
    """The record was made under the settings in force now. The settings key decides when both sides have one
    (so a sign-in refresh that only rewrites the file does not throw the result away); otherwise the file time."""
    key = _settings_key()
    if key is not None and record.get('key') is not None:
        return record['key'] == key
    return record.get('settings') == _settings_stamp()


def _flight():
    """(kind, movie, token) of the launch in flight, or None when there is none or it went stale."""
    parts = kodi_utils.get_property(FLIGHT).split('|')
    try:
        return (parts[0], parts[1], parts[2]) if time() < float(parts[4]) else None
    except (IndexError, ValueError):
        return None


def _fly(kind, movie_id, token, seconds):
    kodi_utils.set_property(FLIGHT, '%s|%s|%s|%.2f|%.2f' % (kind, movie_id, token, time(), time() + seconds))


def _renew(movie_id, token, seconds, kind='check'):
    """Renew, or end ("done"), this route's own entry; never touch one that has passed to someone else."""
    if _flight() in (('check', movie_id, token), None):
        _fly(kind, movie_id, token, seconds)


def _interrupt(threads):
    """Tell threads of this run to stop: SystemExit at their next Python instruction, the way Kodi stops a script.
    A thread inside one blocking call (a network read) ends when that call returns. Returns False when the
    Python API cannot be reached on this platform; the threads then simply run out."""
    try:
        import ctypes
        for library in (None, 'libkodi.so'):        # Android: Python lives inside libkodi.so
            try:
                call = ctypes.PyDLL(library).PyThreadState_SetAsyncExc
                break
            except (OSError, AttributeError):
                call = None
        call.argtypes, call.restype = (ctypes.c_ulong, ctypes.py_object), ctypes.c_int
        for thread in threads:
            call(thread.ident, SystemExit)
        return True
    except Exception:
        return False


def _cache_file(movie_id):
    return cache_dir() / ('movie-%s.json' % movie_id) if _MOVIE_ID.fullmatch(movie_id or '') else None


def load_result(movie_id):
    path = _cache_file(movie_id)
    if path is None:
        return None
    try:
        record = json.loads(path.read_bytes())
        if (record.get('v') == 1 and record.get('movie') == movie_id and 0 <= time() - record['created'] < RESULT_TTL
                and _settings_match(record)
                and isinstance(record.get('results'), list) and isinstance(record.get('view'), dict)):
            # R10.25: the file holds the panel only; the sources come from memory (empty when Kodi restarted since,
            # or when the profile or a source account changed)
            return {**record, 'results': _memory_results(movie_id, record)}
        if not 0 <= time() - record['created'] < RESULT_TTL:
            path.unlink()               # R10.25: a stale read deletes the file; it is of no use any more
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        pass
    return None


def _owner():
    """A short one-way hash of the Kodi profile and the signed-in source accounts, or None when they cannot be read."""
    try:
        parts = [xbmc.getInfoLabel('System.ProfileName')] + [str(kodi_utils.get_setting(key, '')) for key in ACCOUNT_IDS]
        return hashlib.sha256(repr(parts).encode('utf-8', 'replace')).hexdigest()[:20]
    except Exception:
        return None


def _memory_key(movie_id):
    return MEMORY + 'Results.' + movie_id


def _memory_index():
    try:
        return [(str(movie), float(created), str(owner)) for movie, created, owner in json.loads(kodi_utils.get_property(MEMORY_INDEX) or '[]')
                if _MOVIE_ID.fullmatch(str(movie))]
    except (TypeError, ValueError):
        return []


def _memory_prune(index, keep_owner=True):
    """Keep the newest MEMORY_ENTRIES movies younger than RESULT_TTL that belong to the current profile and accounts;
    release every other movie's property. keep_owner=False releases all of them."""
    now, owner, kept = time(), _owner(), {}
    for movie, created, whose in sorted(index, key=lambda entry: entry[1], reverse=True):
        if (keep_owner and owner is not None and whose == owner and movie not in kept and len(kept) < MEMORY_ENTRIES
                and 0 <= now - created < RESULT_TTL):
            kept[movie] = created
    for movie in {movie for movie, _created, _whose in index} - set(kept):
        kodi_utils.clear_property(_memory_key(movie))
    kodi_utils.set_property(MEMORY_INDEX, json.dumps(sorted(([movie, created, owner] for movie, created in kept.items()),
                                                            key=lambda entry: -entry[1])))


def _memory_put(movie_id, record):
    owner = _owner()
    if not record.get('results') or owner is None:
        return
    kodi_utils.set_property(_memory_key(movie_id), json.dumps({'created': record['created'], 'key': record.get('key'), 'owner': owner,
                                                               'results': record['results']}, separators=(',', ':')))
    _memory_prune(_memory_index() + [(movie_id, record['created'], owner)])


def _memory_results(movie_id, record):
    """The sources of this very record (same search: same created time and settings key) for the current profile and
    accounts, or []."""
    try:
        entry = json.loads(kodi_utils.get_property(_memory_key(movie_id)) or 'null')
        owner = _owner()
        if (isinstance(entry, dict) and entry.get('created') == record['created'] and entry.get('key') == record.get('key')
                and owner is not None and entry.get('owner') == owner and isinstance(entry.get('results'), list)):
            return entry['results']
    except (KeyError, TypeError, ValueError):
        pass
    return []


def forget():
    """R10.25: release every movie's sources held in memory (a sign-in or sign-out in My Services calls this)."""
    _memory_prune(_memory_index(), keep_owner=False)


def _remove_legacy(folder):
    """Once per profile folder: delete every finished search an older version wrote (it held the sources and their links)."""
    mark = folder / LEGACY_MARK
    if mark.exists():
        return
    for path in folder.iterdir():
        if path.name.startswith(('movie-', '.tmp-')):
            try:
                path.unlink()
            except OSError:
                pass
    mark.touch()


def housekeeping():
    """R10.25: at Kodi start (POV's service) and at the start of every check. Expired files go now, not only after the next
    successful store; older versions' files (with links) go once; memory entries past RESULT_TTL, or of another profile or
    account, are released."""
    folder = cache_dir()
    try:
        if folder.is_dir():
            _remove_legacy(folder)
            _prune(folder)
    except OSError:
        pass
    _memory_prune(_memory_index())


def _prune(folder):
    now, files = time(), []
    for path in folder.iterdir():
        try:
            if path.name.startswith('movie-') or (path.name.startswith('.tmp-') and now - path.stat().st_mtime > 60):
                files.append((path.stat().st_mtime, path))
        except OSError:
            pass
    files.sort(reverse=True)
    kept = 0
    for mtime, path in files:
        if path.name.startswith('movie-') and kept < CACHE_ENTRIES and 0 <= now - mtime < RESULT_TTL:
            kept += 1
            continue
        try:
            path.unlink()
        except OSError:
            pass


def store_result(movie_id, record):
    path = _cache_file(movie_id)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _remove_legacy(path.parent)
        newer = load_result(movie_id)
        if newer and newer['created'] > record['created']:
            return False
        with NamedTemporaryFile(mode='w', dir=path.parent, prefix='.tmp-', suffix='.json', delete=False) as stream:
            try:
                # R10.25 (F-07): the panel only. "results" stays an (empty) list: the skin's cached_quality() checks its type.
                json.dump({**record, 'results': []}, stream, separators=(',', ':'))
                stream.flush()
                os.replace(stream.name, path)
            finally:
                Path(stream.name).unlink(missing_ok=True)
        _memory_put(movie_id, record)
        _prune(path.parent)
        return True
    except (OSError, TypeError, ValueError):
        return False


def playback_preview(token, movie_id, timeout, monitor):
    # Play reuses the finished search of this movie when there is one. R9: it never waits here any more: the skin
    # launches Play only when no check is in flight (single flight), and All Sources passes no token at all.
    # R10.25: only when the sources are still in memory; otherwise Play searches (an empty list would mean "no sources").
    record = load_result(movie_id)
    return (record['results'], record['scrape_time']) if record and record['results'] else None


def describe(source):
    group = next(iter(build_source_groups([source])), {})
    size = format_source_size(source)
    video = ' + '.join(group.get('video_badges', ())).replace('Dolby Vision + ', 'DV + ')
    video = ' · '.join(filter(None, (video, group.get('video_detail')))) or 'N/A'
    audio = next(iter(group.get('audio_badges', ())), 'N/A')
    return ' · '.join((source.get('quality') or 'N/A', size if size != 'Size unknown' else 'N/A')), video, audio


def summary(source):
    """One spec line for the details-page menu: picture, size, audio; unknown parts are left out."""
    group = next(iter(build_source_groups([source])), {})
    badges = group.get('video_badges', ())
    picture = next((badge for badge in ('Dolby Vision', 'HDR10+', 'HDR') if badge in badges), '')
    picture = ' '.join(filter(None, (str(source.get('quality') or '').strip(), picture)))
    size = format_source_size(source)
    audio = next(iter(group.get('audio_badges', ())), '')
    return ' · '.join(filter(None, (picture, '' if size == 'Size unknown' else size, audio))) or 'Details unknown'


def build_view(source, processed):
    """Tiers over sources whose availability is known; returns (view, available sources)."""
    from modules.sources import prefer_available_quality, quality_ranks
    results = available_sources(processed)
    ranked = rank_playback_sources(results, quality_ranks)
    view = {'labels': {}, 'lines': {}, 'total': 0}
    if ranked:
        for key, index in source.movie_quality_indices(ranked).items():
            if index is not None:
                view['labels'][key] = list(describe(ranked[index]))
                view['lines'][key] = summary(ranked[index])
                if key == 'highest':
                    # R10-playback: what the play loader shows for plain Play (it plays this source first).
                    view['facts'] = list(play_facts(ranked[index]))
        view['total'] = len(build_source_groups(prefer_available_quality(results, 'movie')))
    return view, results


def publish(view, final, current):
    """partial: tiers found so far, empty lines for the rest (the panel keeps its bar there). final: every tier."""
    if not current():
        return False
    set_property, lines, total = kodi_utils.set_property, view['lines'], view['total']
    count = '%d %s' % (total, 'source' if total == 1 else 'sources')
    if not final:
        for tier in TIERS:
            set_property(PREFIX + tier.title() + 'Line', lines.get(tier, ''))
        set_property(PREFIX + 'AllLine', count)
        set_property(PREFIX + 'State', 'partial')
        return True
    for tier in TIERS:
        for field, value in zip(('Specs', 'Video', 'Audio'), view['labels'].get(tier) or ('Unavailable', '', '')):
            set_property(PREFIX + tier.title() + field, value)
        set_property(PREFIX + tier.title() + 'Line', lines.get(tier, 'Not available'))
    for field, value in zip(FACT_FIELDS, view.get('facts') or ('', '', '')):
        set_property(PREFIX + 'Highest' + field, value)
    set_property(PREFIX + 'AllSpecs', count)
    set_property(PREFIX + 'AllLine', count if total else 'No sources found')
    set_property(PREFIX + 'State', 'ready')
    return True


def progressive(source):
    return all(hasattr(source, name) for name in ('scraper_processor', 'results_processor', 'providers'))


class Collector:
    """One preview search: a thread per provider, debrid availability checked in waves on what has arrived.

    Mirrors Sources.collect_results + ExternalManager.results for a background autoplay movie search (same
    providers, same deadlines, same de-duplication and cache labels), but reports each provider as it finishes."""

    def __init__(self, source, movie_id, current, begun):
        self.source, self.movie_id, self.current, self.begun = source, movie_id, current, begun
        self.started = monotonic()                    # search start, after metadata
        self.created, self.settings, self.key = time(), _settings_stamp(), _settings_key()
        self.beat_at, self.left = 0.0, False
        self.hard = self.started + source.timeout + 1  # provider deadline, as ExternalManager.monitor
        self.events = Queue()
        self.threads, self.running, self.debrids = [], {}, []
        self.internal, self.checked, self.unchecked, self.late, self.available = [], [], [], [], []
        self.seen_urls, self.seen_hashes = set(), set()
        self.wave, self.waves, self.check_seconds = None, 0, 0.0
        self.first_result = self.first_tier = self.panel_at = self.complete_at = self.cut_at = None
        self.cut_reason, self.dirty, self.aborted, self.panel_final = 'none', False, False, False
        self.view, self.shown, self.final_view = None, None, None
        self.count = {'providers': 0, 'given_up': 0, 'after_cut': 0, 'raw': 0}

    def jobs(self):
        from modules.sources import DialogProgress, ExternalSource
        from modules.debrid import DebridCheck
        from caches.debrid_cache import DebridCache
        self.debrid_check, self.debrid_cache = DebridCheck, DebridCache
        source, info = self.source, self.source.meta['search_info']
        source.prescrape = False
        source.progress_dialog = DialogProgress(False)
        source.scraper_processor.activate_internal(False)
        jobs = [(name, 'internal', lambda module=module: module().results(info)) for _kind, module, name in source.providers]
        source.scraper_processor.activate_external()
        self.debrids = list(getattr(source, 'debrid_torrent_enabled', None) or ())
        if source.active_external:
            counts = dict.fromkeys(('4K', '1080p', '720p', 'SD', 'total'), 0)
            for provider, module, *pack in source.external_providers:
                args = (provider, module, *pack) if pack else (provider, module)
                name = '%s (%s)' % (provider, pack[0]) if pack and pack[0] else provider
                jobs.append((name, 'external', lambda args=args: ExternalSource(source.meta, counts).results(info, args)))
        return jobs

    def run(self, token):
        jobs = self.jobs()
        self.count['providers'] = len(jobs)
        for index, (name, kind, job) in enumerate(jobs):
            self.running[index] = (_UNSAFE_NAME.sub('', str(name))[:40] or 'provider', kind, monotonic())
            self._start(self._provider, (index, job), 'POVQuality-%d' % index)
        monitor = xbmc.Monitor()
        while True:
            if monitor.abortRequested():
                self.aborted = True
                return
            try:
                self._handle(self.events.get(timeout=POLL))
                while True:
                    self._handle(self.events.get_nowait())
            except Empty:
                pass
            now = monotonic()
            if not self.current():
                # R9: the page that asked for this check is gone (closed, replaced, or Play went ahead alone):
                # stop here, within POLL. Nothing more is checked, published or kept; finish() stops the
                # provider threads already out, and the flight entry stays ours until they have ended.
                self.left = True
                return
            if now - self.beat_at >= 1.0:
                self.beat_at = now
                _renew(self.movie_id, token, BEAT)
            if self._step(monotonic()):
                return

    def _start(self, target, args, name):
        thread = threading.Thread(target=target, args=args, name=name)
        self.threads.append(thread)
        thread.start()

    def _provider(self, index, job):
        begun, items, status = monotonic(), [], 'error'
        try:
            items = list(job() or ())
            status = 'ok' if items else 'empty'
        except Exception:
            items = []
        finally:  # SystemExit still propagates after this
            self.events.put(('provider', index, items, status, monotonic() - begun))

    def _check(self, wave_id, name, hashes):
        cached = None
        try:
            check = self.debrid_check(self.source.meta, name)
            check.hash_list = list(hashes)                 # instance copies: no shared class state
            check.cached_hashes = self.debrid_cache().get_many(check.hash_list) or []
            cached = set(check.cache_check())
        except Exception:
            cached = None
        finally:
            self.events.put(('cache', wave_id, name, cached))

    def _handle(self, event):
        if event[0] == 'cache':
            _kind, wave_id, name, cached = event
            if self.wave and self.wave['id'] == wave_id:
                self.wave['waiting'].discard(name)
                if cached is not None:
                    self.wave['found'][name] = cached
            return
        _kind, index, items, status, seconds = event
        if index not in self.running:
            return  # given up at the deadline; ExternalManager ignores these too
        name, kind, _begun = self.running.pop(index)
        after_cut = self.cut_at is not None
        self.count['after_cut'] += after_cut
        self.count['raw'] += len(items)
        log('provider name=%s kind=%s ms=%s results=%d status=%s%s'
            % (name, kind, _ms(seconds), len(items), status, ' after_cut=1' if after_cut else ''))
        if items and self.first_result is None:
            self.first_result = monotonic()
        if after_cut and not self.panel_final:
            self.late.append((kind, items))
        else:
            self._take(kind, items)

    def _take(self, kind, items):
        if kind == 'internal':
            self.internal.extend(items)
            self.dirty = True
            return
        for item in items:  # ExternalManager.process_duplicates, kept across providers
            try:
                url = item['url'].lower()
                if url in self.seen_urls:
                    continue
                self.seen_urls.add(url)
                if 'hash' in item:
                    hash_ = item['hash'].lower()
                    if hash_ in self.seen_hashes:
                        continue
                    self.seen_hashes.add(hash_)
            except (AttributeError, KeyError, TypeError):
                pass
            if isinstance(item, dict) and 'torrent' in str(item.get('source', '')):
                self.unchecked.append(item)

    def _start_wave(self, now):
        batch, self.unchecked = self.unchecked, []
        self.waves += 1
        self.wave = {'id': self.waves, 'items': batch, 'waiting': set(self.debrids), 'found': {}, 'begun': now,
                     'deadline': now + self.source.timeout + 1}
        hashes = list({item.get('hash') for item in batch})
        for name in self.debrids:
            self._start(self._check, (self.waves, name, hashes), 'POVQualityCheck-%d' % self.waves)

    def _finish_wave(self, now):
        wave, self.wave = self.wave, None
        self.check_seconds += now - wave['begun']
        for name, cached in wave['found'].items():
            uncached = '%s %s' % ('Unchecked' if name in ('realdebrid', 'alldebrid') else 'Uncached', name)
            self.checked.extend({**item, 'cache_provider': name if item.get('hash') in cached else uncached,
                                 'debrid': name} for item in wave['items'])
        log('cache_check wave=%d torrents=%d services=%d answered=%d ms=%s'
            % (wave['id'], len(wave['items']), len(self.debrids), len(wave['found']), _ms(now - wave['begun'])))
        self.dirty = True

    def _compute(self):
        processed = self.source.results_processor.process(self.internal + self.checked)
        self.view, self.available = build_view(self.source, processed)
        self.dirty = False
        return self.view

    def _step(self, now):
        if self.running and now > self.hard:
            for name, kind, begun in self.running.values():
                log('provider name=%s kind=%s ms=%s results=0 status=timeout' % (name, kind, _ms(now - begun)))
            self.count['given_up'] = len(self.running)
            self.running.clear()
        if self.cut_at is None and not self.panel_final and self.running and self.first_result is not None:
            stop = min(self.first_result + PREVIEW_GRACE, max(self.started + PREVIEW_CUTOFF, self.first_result))
            if now >= stop:
                self.cut_at = now
                self.cut_reason = 'grace' if stop == self.first_result + PREVIEW_GRACE else 'cutoff'
        if self.wave and (not self.wave['waiting'] or now >= self.wave['deadline']):
            self._finish_wave(now)
        collecting = bool(self.running)
        if self.wave is None and self.unchecked and (
                self.waves < CACHE_WAVES or not collecting or (self.cut_at is not None and not self.panel_final)):
            self._start_wave(now)
        settled = self.wave is None and not self.unchecked
        if not self.panel_final:
            if settled and (not collecting or self.cut_at is not None):
                self._panel(now)
            elif self.dirty:
                view = self._compute()
                if view['lines'] and view != self.shown and publish(view, False, self.current):
                    self.shown = view
                    if self.first_tier is None:
                        self.first_tier = now
            return False
        if settled and not collecting and not self.late:
            self._complete(now)
            return True
        return False

    def _panel(self, now):
        view = self._compute()
        if publish(view, True, self.current):
            self.final_view = view
            if self.first_tier is None and view['lines']:
                self.first_tier = now
        self.panel_final, self.panel_at = True, now
        late, self.late = self.late, []
        for kind, items in late:
            self._take(kind, items)

    def _complete(self, now):
        view = self._compute()
        if self.final_view is not None and view != self.final_view and publish(view, True, self.current):
            self.final_view = view
        self.complete_at = now
        self.source.meta['scrape_time'] = now - self.started
        # R9: an empty result is not kept (as before quality-speed), so a failed search is retried on the next open.
        # A search-relevant setting changed while this ran: the result fits neither the old nor the new settings.
        unchanged = self.key == _settings_key()
        self.saved = bool(self.available) and unchanged and store_result(self.movie_id, {'v': 1, 'key': self.key, 'movie': self.movie_id, 'created': self.created,
                                                  'settings': self.settings,
                                                  'scrape_time': now - self.started, 'results': self.available,
                                                  'view': view})

    def finish(self, token):
        """Hold the flight entry until every thread this run started has ended, then report. Whatever is still out
        is of no use (given up at its deadline, or the page left): it is told to stop and joined in POLL steps.
        After STRAGGLER_CAP the route gives up on a thread that will not end and says so (threads_alive)."""
        monitor, cap, interrupted = xbmc.Monitor(), monotonic() + STRAGGLER_CAP, '-'
        nudge = monotonic() + (0 if self.left else POLL)   # a search that completed: its threads are just ending
        alive = [thread for thread in self.threads if thread.is_alive()]
        while alive and monotonic() < cap and not monitor.abortRequested():
            _renew(self.movie_id, token, BEAT)
            if monotonic() >= nudge:                # again every half second: scrapers swallow exceptions
                nudge, interrupted = monotonic() + .5, int(_interrupt(alive))
            alive[0].join(POLL)
            alive = [thread for thread in alive if thread.is_alive()]
        alive = len(alive)
        begun = self.begun
        log('summary movie=%s cache=miss providers=%d given_up=%d after_cut=%d raw=%d available=%d setup_ms=%s '
            'first_result_ms=%s first_tier_ms=%s panel_ms=%s complete_ms=%s cache_check_ms=%s waves=%d cut=%s '
            'saved=%d stale=%d threads_alive=%d interrupted=%s tiers=%d'
            % (self.movie_id, self.count['providers'], self.count['given_up'], self.count['after_cut'],
               self.count['raw'], len(self.available), _ms(self.started - begun),
               _ms(self.first_result and self.first_result - begun), _ms(self.first_tier and self.first_tier - begun),
               _ms(self.panel_at and self.panel_at - begun), _ms(self.complete_at and self.complete_at - begun),
               _ms(self.check_seconds), self.waves, self.cut_reason, bool(getattr(self, 'saved', False)),
               self.left, alive, interrupted, len((self.view or EMPTY_VIEW)['lines'])))


def prefetch(params):
    movie_id, token = params.get('tmdb_id', ''), params.get('preview', '')

    def current():
        return (token and kodi_utils.get_property(PREFIX + 'Token') == token
                and kodi_utils.get_property('Netflix.Movie.ID') == movie_id)

    # Only the page that scheduled this call may run it, and only as the one launch in flight.
    mine = _flight() in (('pending', movie_id, token), None)
    if not current() or not mine:
        if mine:
            _fly('done', movie_id, token, EXIT_ROOM)    # the page closed as the alarm went off: this one just leaves
        return
    _fly('check', movie_id, token, SETUP_GRACE)
    housekeeping()
    begun = monotonic()
    view, collector, stopping = None, None, False
    try:
        record = load_result(movie_id)
        if record:
            view = record['view']
            if record.get('settings') != _settings_stamp():
                # Valid by the settings key, but the file time moved (a sign-in refresh): renew the stamp so the
                # skin, which only compares the file time, can fill the menu itself again next time.
                store_result(movie_id, {**record, 'settings': _settings_stamp()})
            log('summary movie=%s cache=hit age_s=%d panel_ms=%s' % (movie_id, time() - record['created'],
                                                                    _ms(monotonic() - begun)))
            return
        from modules.sources import Sources
        if not current():
            return  # the page went away while POV was loading
        source = Sources()
        source.config_loader.apply(source, {'tmdb_id': movie_id, 'autoplay': 'true',
                                            'background': 'true', 'netflix_quality_preview': 'true'})
        source.meta = source.meta_builder.build(source)
        if not current():
            return  # the page went away during set-up: nothing has been searched yet
        if progressive(source):
            collector = Collector(source, movie_id, current, begun)
            collector.run(token)
        else:  # unexpected Sources API: one full search, published at the end, not stored
            _renew(movie_id, token, 2 * getattr(source, 'timeout', 10) + 10)
            view = build_view(source, source.get_sources())[0]
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        pass
    except (SystemExit, KeyboardInterrupt):  # Kodi is stopping this script: leave at once, re-raised
        stopping = True
        raise
    finally:
        if not stopping and not (collector is not None and collector.aborted):
            if collector is None or not (collector.panel_final or collector.left):
                publish((collector and collector.view) or view or EMPTY_VIEW, True, current)
            if collector is not None:
                collector.finish(token)
            # Every way out ends the flight the same way: "done" for EXIT_ROOM, while this interpreter leaves.
            _renew(movie_id, token, EXIT_ROOM, 'done')
