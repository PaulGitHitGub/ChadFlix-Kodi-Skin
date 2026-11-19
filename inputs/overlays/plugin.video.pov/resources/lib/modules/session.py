_pp = getattr(__import__('sys'), '_pov_probe', None)
if _pp: _pp('session_start')
# Lazy HTTP layer: urllib3/requests (~440 ms cold on the Shield) are imported on the first REAL use of
# `session` / `http` (or of TimeoutSession / requests / urllib3 / retry as module attributes), not at import.
# `session` and `http` are thin proxies; `HTTPAdapter(...)` / `Retry(...)` build lightweight specs that are
# turned into the real objects (once, cached) when the real session is created or `session.mount` runs late.
from threading import RLock

_lock = RLock()
_loaded = None
_PENDING = []  # (prefix, spec_or_adapter) queued by session.mount() before the real session exists

def _load():
	global _loaded
	if _loaded is None:
		with _lock:
			if _loaded is None:
				import urllib3
				if _pp: _pp('session_urllib3_imported')
				import requests
				from requests.adapters import HTTPAdapter as _HTTPAdapter, Retry as _Retry
				if _pp: _pp('session_requests_imported')

				class TimeoutSession(requests.Session):
					CUSTOM_ERRORS = requests.exceptions.ConnectionError, requests.exceptions.Timeout
					def __init__(self, timeout=None):
						requests.Session.__init__(self)
						self.timeout = timeout or (3.05, 6.05)

					def get_adapter(self, url):
						from modules.netflix_next_context import current
						scope = current()
						if scope is None: return requests.Session.get_adapter(self, url)
						adapter = getattr(scope.local, 'adapter', None)
						if adapter is None:
							adapter = _loaded['PreparationAdapter'](max_retries=_Retry(total=0, connect=0, read=0, status=0, other=0))
							scope.local.adapter = adapter
							scope.retain_closer(adapter.close)
						return adapter

					def request(self, *args, **kwargs):
						kwargs.setdefault('timeout', self.timeout)
						return requests.Session.request(self, *args, **kwargs)

				class BoundedRetry(_Retry):
					"""R10.14: a READ TIMEOUT is never retried (a stalled server or a dead network costs one timeout, not one per try);
					every other error is counted as the Retry arguments say (e.g. read=1: one retry of a dropped keep-alive); Retry-After
					is capped at 1 s."""
					def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
						if isinstance(error, urllib3.exceptions.ReadTimeoutError): self = self.new(read=0)
						return _Retry.increment(self, method=method, url=url, response=response, error=error, _pool=_pool,
												_stacktrace=_stacktrace)

					def get_retry_after(self, response):
						"""R10.14: a server's Retry-After is honoured for at most 1 s (a 429 / 503 then gets its one retry after a short
						wait instead of giving up at once, and a large value such as 600 cannot stall the call)."""
						try: value = _Retry.get_retry_after(self, response)
						except Exception: return None
						return None if value is None else min(value, 1.0)

				class BoundedAdapter(_HTTPAdapter):
					"""R10.14: caps the timeout of every request sent through this adapter, whatever the caller passes: connect always,
					read for GET / HEAD (writes keep the caller's read timeout)."""
					def __init__(self, *args, connect_cap=3.05, read_cap=6.05, **kwargs):
						self.connect_cap, self.read_cap = connect_cap, read_cap
						_HTTPAdapter.__init__(self, *args, **kwargs)

					def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
						if timeout is None or isinstance(timeout, (int, float, tuple)):
							connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
							connect = self.connect_cap if connect is None else min(connect, self.connect_cap)
							if request.method in ('GET', 'HEAD'): read = self.read_cap if read is None else min(read, self.read_cap)
							timeout = (connect, read)
						return _HTTPAdapter.send(self, request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

				class ServiceRetry(_Retry):
					"""R10.25 (security audit F-11): the retry policy of the account services (debrid, Easynews, MDbList) and of the shared
					pool. Callers give it a finite budget (total / connect / read / status); POV's Retry(total=None, status=1) had none for a
					failed connection or a dropped read. A server's Retry-After is honoured up to RETRY_AFTER_CAP seconds. Methods that are
					not idempotent (POST) are never retried after a read error or a status, as in urllib3: no duplicated writes."""
					RETRY_AFTER_CAP = 10.0
					def get_retry_after(self, response):
						try: value = _Retry.get_retry_after(self, response)
						except Exception: return None
						return None if value is None else min(value, self.RETRY_AFTER_CAP)

				class PreparationAdapter(_HTTPAdapter):
					def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
						from modules.netflix_next_context import current
						scope = current()
						# R10.43: cleanup DELETEs (e.g. Real-Debrid removing a torrent prep just added) still go out after Stop.
						if scope is None or (scope.cancelled.is_set() and request.method != 'DELETE'): raise RuntimeError('Preparation cancelled')
						return _HTTPAdapter.send(self, request, stream=stream, timeout=(3.05, 6.05), verify=verify, cert=cert, proxies=proxies)

				_loaded = {'PreparationAdapter': PreparationAdapter, 'urllib3': urllib3, 'requests': requests, 'TimeoutSession': TimeoutSession,
							'HTTPAdapter': _HTTPAdapter, 'Retry': _Retry, 'BoundedAdapter': BoundedAdapter, 'BoundedRetry': BoundedRetry, 'ServiceRetry': ServiceRetry}
	return _loaded

def _real(obj):
	return obj._build() if isinstance(obj, _Spec) else obj

class _Spec(object):
	"""Deferred constructor call: same signature as the real class, built once on first need."""
	_name = None
	def __init__(self, *args, **kwargs):
		self._args, self._kwargs, self._built = args, kwargs, None

	def _build(self):
		if self._built is None:
			with _lock:
				if self._built is None:
					args = [_real(i) for i in self._args]
					kwargs = {k: _real(v) for k, v in self._kwargs.items()}
					self._built = _load()[self._name](*args, **kwargs)
		return self._built

	def __getattr__(self, name):
		if name.startswith('__') or name in ('_args', '_kwargs', '_built'): raise AttributeError(name)
		return getattr(self._build(), name)

class HTTPAdapter(_Spec): _name = 'HTTPAdapter'
class Retry(_Spec): _name = 'Retry'
class BoundedAdapter(_Spec): _name = 'BoundedAdapter'  # R10.14 (see _load)
class BoundedRetry(_Spec): _name = 'BoundedRetry'  # R10.14 (see _load)
class ServiceRetry(_Spec): _name = 'ServiceRetry'  # R10.25 (see _load)

class _Lazy(object):
	def __init__(self):
		self._obj, self._olock = None, RLock()

	def _get(self):
		obj = self._obj
		if obj is None:
			with self._olock:
				obj = self._obj
				if obj is None:
					obj = self._obj = self._create()
		return obj

	def __getattr__(self, name):
		if name.startswith('__') or name in ('_obj', '_olock'): raise AttributeError(name)
		return getattr(self._get(), name)

	def __setattr__(self, name, value):
		if name in ('_obj', '_olock'): object.__setattr__(self, name, value)
		else: setattr(self._get(), name, value)

class _LazySession(_Lazy):
	def _create(self):
		s = _load()['TimeoutSession']()
		for prefix, adapter in _PENDING: s.mount(prefix, _real(adapter))
		del _PENDING[:]
		return s

	def mount(self, prefix, adapter):
		with self._olock:
			if self._obj is None: _PENDING.append((prefix, adapter))
			else: self._obj.mount(prefix, _real(adapter))

class _LazyHttp(_Lazy):
	def _get(self):
		from modules.netflix_next_context import current
		scope = current()
		if scope is None: return super()._get()
		if scope.cancelled.is_set(): raise RuntimeError('Preparation cancelled')
		pool = getattr(scope.local, 'http', None)
		if pool is None:
			urllib3 = _load()['urllib3']
			class PreparationPool(urllib3.PoolManager):
				def urlopen(self, method, url, redirect=True, **kw):
					if scope.cancelled.is_set(): raise RuntimeError('Preparation cancelled')
					kw['retries'] = urllib3.util.Retry(total=0, connect=0, read=0, status=0, other=0)
					kw['timeout'] = urllib3.util.Timeout(connect=3.05, read=6.05)
					return super().urlopen(method, url, redirect=redirect, **kw)
			pool = PreparationPool(maxsize=4)
			pool.headers.update(urllib3.util.make_headers(keep_alive=True, accept_encoding=True))
			scope.local.http = pool
			scope.retain_closer(pool.clear)
		return pool

	def _create(self):
		urllib3 = _load()['urllib3']
		# R10.25 (F-11): finite (was total=None: no limit for a failed connection). At most 3 retries per call.
		retry = _load()['ServiceRetry'](total=3, connect=2, read=1, status=1, status_forcelist=(429, 502, 503, 504), backoff_factor=1)
		http = urllib3.PoolManager(maxsize=10, retries=retry, timeout=urllib3.util.Timeout(connect=3.05, read=6.05))
		http.headers.update(urllib3.util.make_headers(keep_alive=True, accept_encoding=True))
		return http

def __getattr__(name):  # PEP 562: legacy module attributes (TimeoutSession, requests, urllib3, retry) load on demand
	if name in ('TimeoutSession', 'requests', 'urllib3'): return _load()[name]
	if name == 'retry':
		return _load()['ServiceRetry'](total=3, connect=2, read=1, status=1, status_forcelist=(429, 502, 503, 504), backoff_factor=1)
	raise AttributeError('module %r has no attribute %r' % (__name__, name))

http = _LazyHttp()
session = _LazySession()
if _pp: _pp('session_lazy_defined')
