"""Temporary-database browser fixture; no production workers or writes."""
import os
import json
from pathlib import Path
from anime.db import Store, ROOT

fixture_db = Path(os.environ['ANIMESERVICE_DB']).resolve()
assert fixture_db.is_relative_to((ROOT/'data/unified-player-20261016').resolve())
assert fixture_db.name.startswith('tracking-test-')
db = Store(fixture_db)
db.execute("INSERT OR REPLACE INTO shows(id,title,total,selected,state,watched,mapping) VALUES(639938,'成品续播测试',3,1,'watching',1,?)", (json.dumps({'season':6}),))
db.execute('INSERT OR REPLACE INTO watches(show_id,episode,finished) VALUES(639938,1,1)')
from anime.app import app
db.execute('DELETE FROM web_progress')
db.execute('DELETE FROM finished_playback_receipts')
db.execute('DELETE FROM watches WHERE episode>1')

@app.middleware('http')
async def fixture_origin(request, call_next):
    if request.client.host == '127.0.0.1' and request.headers.get('origin') == 'http://127.0.0.1:4879':
        request.scope['headers'] = [(k,v) for k,v in request.scope['headers'] if k != b'origin'] + [(b'origin',b'http://127.0.0.1:4871')]
    return await call_next(request)

@app.get('/api/testing/fixture')
def fixture(): return {'isolated':True,'database':str(fixture_db)}

# A second playback source exercises the source selector using a read-only file.
movie = ROOT / 'static/finished-media/steelball-s06e02-v0/episode.mp4'
if not movie.exists():
    release = next(r for r in json.loads((ROOT/'static/finished-media/index.json').read_text(encoding='utf-8'))['releases']
                   if r['show_id'] == 639938 and r['episode'] == 2 and r['edition'] == 'v0')
    movie = ROOT / 'static/finished-media' / release['id'] / 'episode.mp4'
db.execute("INSERT OR REPLACE INTO episodes(show_id,episode,path,size,hash,status) VALUES(639938,2,?,?,?,'complete')",
           (str(movie), movie.stat().st_size, 'a'*40))
