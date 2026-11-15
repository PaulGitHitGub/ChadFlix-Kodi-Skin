"""Build a requested tab's rows from POV's warm metadata before starting workers."""

from threading import Thread
from time import monotonic

import xbmc

from modules.utils import TaskPool


def build_cached_page(build_item, rows):
    started = monotonic()
    owner = getattr(build_item, '__self__', None)
    action = getattr(owner, 'action', 'unknown')
    genre = getattr(owner, 'params', {}).get('genre_id', '')
    xbmc.log('Netflix Browse Provider: start action=%s genre=%s items=%d' % (action, genre, len(rows)), xbmc.LOGINFO)
    missing = []
    for position, row in enumerate(rows, 1):
        if build_item(position, row, memory_only=True) is False:
            missing.append((position, row))

    if missing:
        for worker in TaskPool().tasks(build_item, missing, Thread):
            worker.join()

    xbmc.log('Netflix Browse Provider: finish action=%s genre=%s items=%d memory_hits=%d misses=%d elapsed=%.3f' %
             (action, genre, len(rows), len(rows) - len(missing), len(missing), monotonic() - started), xbmc.LOGINFO)
