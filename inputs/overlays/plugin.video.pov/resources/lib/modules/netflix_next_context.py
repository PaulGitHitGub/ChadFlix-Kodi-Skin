"""Preparation-local lifetime: ephemeral caches, isolated workers and drain.

No globals are monkeypatched. Non-preparation callers keep their usual behavior.
The Thread wrapper propagates context to owned provider children.
"""
from contextlib import contextmanager
from threading import Event, Lock, Thread as _Thread, current_thread, local

_state = local()


def current():
    return getattr(_state, 'scope', None)


def ephemeral():
    return current() is not None


@contextmanager
def bind(scope):
    previous = current()
    _state.scope = scope
    try:
        yield scope
    finally:
        _state.scope = previous


class PreparationScope:
    def __init__(self, cancelled=None):
        self.cancelled = cancelled if cancelled is not None else Event()
        self.lock = Lock()
        self.failed = Event()
        self.closed = False
        self.threads = []
        self.closers = []
        self.local = local()

    def drain(self):
        # Workers can create nested workers; repeat until the registered set is stable.
        seen_threads = set()
        while True:
            with self.lock:
                threads = [t for t in self.threads if t not in seen_threads and t is not current_thread()]
                if not threads:
                    self.closed = True
                    break
            for thread in threads:
                # A failed start registers no running thread.
                if thread.ident is not None: thread.join()
                seen_threads.add(thread)
        with self.lock:
            closers, self.closers = self.closers, []
        for close in closers:
            try: close()
            except Exception: pass

    def retain_closer(self, close):
        with self.lock: self.closers.append(close)


class Thread(_Thread):
    def __init__(self, *args, **kwargs):
        self._next_scope = current()
        super().__init__(*args, **kwargs)

    def start(self):
        scope = self._next_scope
        if scope:
            # Publish only after start succeeds, under the same lock as drain's
            # snapshot. A concurrent drain must not skip a not-yet-started child.
            with scope.lock:
                if scope.closed: raise RuntimeError('Preparation scope is closed')
                super().start()
                scope.threads.append(self)
            return None
        return super().start()

    def run(self):
        scope = self._next_scope
        with bind(scope):
            try:
                if scope is None or not scope.cancelled.is_set(): return super().run()
            except Exception:
                if scope is None: raise
                # threading.excepthook would otherwise persist exception payloads
                # to Kodi's log even when its ordinary logger is suppressed.
                scope.failed.set()

