from contextlib import closing
from datetime import datetime, timezone

from modules.kodi_utils import database, database_connect, favorites_db, watched_db
from modules.settings import ignore_articles
from modules.utils import sort_for_article
# from modules.kodi_utils import logger

def call_local(path, values=None, executemany=False, db_path=None):
	if values is None: values = ()
	if db_path is None: db_path = favorites_db
	with closing(database_connect(db_path, isolation_level='IMMEDIATE')) as dbcon, dbcon:
		dbcon.row_factory = database.Row
		dbcur = dbcon.executemany(path, values) if executemany else dbcon.execute(path, values)
		if path.strip().upper().startswith('SELECT'):
			return [dict(row) for row in dbcur.fetchall()]
		return None


def local_favorites(watched_info, mediatype, page_no):
	path = 'SELECT tmdb_id, title FROM favorites WHERE db_type = ?'
	data = call_local(path, (mediatype,))
	if page_no == 'all': return data
	data = [{'media_id': str(i['tmdb_id']), 'title': i['title']} for i in data]
	original_list = sort_for_article(data, 'title', ignore_articles())
	return original_list, 1

def local_droplist(watched_info, mediatype, page_no):
	path = 'SELECT tmdb_id, title FROM dropped WHERE db_type = ?'
	data = call_local(path, (mediatype,))
	if page_no == 'all': return data
	data = [{'media_id': str(i['tmdb_id']), 'title': i['title']} for i in data]
	original_list = sort_for_article(data, 'title', ignore_articles())
	return original_list, 1

def add_to_sync(list_id, mediatype, tmdb_id, title):
	path = 'INSERT INTO %s VALUES (?, ?, ?)' % list_id
	try: call_local(path, (mediatype, str(tmdb_id), title))
	except: return False
	return True

def remove_from_sync(list_id, mediatype, tmdb_id, title):
	path = 'DELETE FROM %s WHERE db_type = ? and tmdb_id = ?' % list_id
	try: call_local(path, (mediatype, str(tmdb_id)))
	except: return False
	return True

def local_get_hidden_items(list_type):
	data = call_local('SELECT tmdb_id FROM dropped')
	return [int(i['tmdb_id']) for i in data]


CW_HIDDEN_SCHEMA = (
	'CREATE TABLE IF NOT EXISTS netflix_cw_hidden ('
	'db_type TEXT, media_id TEXT, hidden_at TEXT, PRIMARY KEY (db_type, media_id))'
)

def hide_continue(db_type, media_id):
	"""Hide a title in Continue Watching until POV records newer playback."""
	if db_type not in ('movie', 'tvshow') or not isinstance(media_id, (str, int)):
		return False
	media_id = str(media_id)
	if not media_id.isascii() or not media_id.isdecimal():
		return False
	media_id = media_id.lstrip('0')
	if not media_id:
		return False
	try:
		with closing(database_connect(watched_db)) as dbcon, dbcon:
			dbcon.execute('BEGIN IMMEDIATE')
			dbcon.execute(CW_HIDDEN_SCHEMA)
			hidden_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
			dbcon.execute('INSERT OR REPLACE INTO netflix_cw_hidden VALUES (?, ?, ?)', (db_type, media_id, hidden_at))
	except database.Error:
		return False
	return True

def read_cw_hidden():
	"""Read all Continue Watching hides once from the local store in any indicator mode."""
	try:
		with closing(database_connect(watched_db)) as dbcon:
			return {(db_type, media_id): hidden_at for db_type, media_id, hidden_at in
			        dbcon.execute('SELECT db_type, media_id, hidden_at FROM netflix_cw_hidden')}
	except database.Error:
		return {}
