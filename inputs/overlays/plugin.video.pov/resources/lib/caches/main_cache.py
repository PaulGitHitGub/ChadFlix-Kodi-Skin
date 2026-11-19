from datetime import datetime, timedelta
from sqlite3 import DatabaseError
from caches import BaseCache, maincache_db
# from modules.kodi_utils import logger

BASE_GET = 'SELECT data FROM maincache WHERE id = ? AND expires > ?'
BASE_SET = 'INSERT OR REPLACE INTO maincache VALUES (?, ?, ?)'
BASE_DELETE = 'DELETE FROM maincache WHERE id = ?'
LIKE_SELECT_ADD = 'id LIKE ?'

class MainCache(BaseCache):
	db_file = maincache_db

	def get(self, string):
		# Read the indexed SQLite cache directly. Kodi Home properties survive
		# add-on runs and otherwise retain one serialized list per arbitrary key.
		current_time = self._get_timestamp(datetime.now())
		try:
			with self: data = self.dbcur.execute(BASE_GET, (string, current_time)).fetchone()
		except DatabaseError:
			return None
		return self.jsloads(data[0]) if data else None

	def set(self, string, data, expiration):
		from modules.netflix_next_context import ephemeral
		if ephemeral(): return  # preparation owns its payload in memory; never SQLite/global cache
		expires = self._get_timestamp(datetime.now() + expiration)
		with self: self.dbcur.execute(BASE_SET, (string, expires, self.jsdumps(data)))

	def delete(self, string):
		with self: self.dbcur.execute(BASE_DELETE, (string,))

	def delete_all(self):
		from modules.meta_lists import media_lists
		items = ' OR '.join(LIKE_SELECT_ADD for _ in media_lists)
		with self:
			self.dbcur.execute('DELETE FROM maincache WHERE ' + items, media_lists)
			self.dbcur.execute('VACUUM')

def cache_object(function, string, url, expiration=24, json=False):
	maincache = MainCache()
	cache = maincache.get(string)
	if cache: return cache
	if not isinstance(url, list): url = (url,)
	if json: result = function(*url).json()
	else: result = function(*url)
	if isinstance(expiration, (int, float)): expiration = timedelta(hours=expiration)
	try: maincache.set(string, result, expiration)
	except DatabaseError: pass  # a provider result is still usable without a cache write
	return result

