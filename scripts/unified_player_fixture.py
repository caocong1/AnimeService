"""Nonce-bound synthetic player fixture. Refuse unsafe setup before importing app."""
import hashlib
import json
import os
from pathlib import Path

qa_root=Path(os.environ.get('PLAYER_QA_ROOT','')).resolve()
qa_token=os.environ.get('PLAYER_QA_TOKEN','')
if (not qa_root.name.startswith('anime-player-qa-') or len(qa_token)<24
    or not (qa_root/'.fixture-token-sha256').is_file()
    or (qa_root/'.fixture-token-sha256').read_text().strip()!=hashlib.sha256(qa_token.encode()).hexdigest()):
    raise RuntimeError('Start the isolated player fixture with run_player_qa.py')
if os.environ.get('ANIMESERVICE_START_WORKER')!='0':raise RuntimeError('Fixture workers must be disabled')
fixture_db=qa_root/'qa.db'
if fixture_db.exists() or fixture_db.is_symlink():raise RuntimeError('Fixture requires a new database, never an existing or linked one')
os.environ['ANIMESERVICE_DB']=str(fixture_db)
os.environ['ANIMESERVICE_DEPLOYMENT']=str(qa_root/'deployment.json')

from anime.db import Store
from anime.engine import Engine
from anime.sources import Sources,Catalog
from anime.app import create_app
from fastapi.responses import JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

class NoNetwork:
    def __getattr__(self,name):
        def blocked(*args,**kwargs):raise RuntimeError('Fixture external access is disabled')
        return blocked

db=Store(fixture_db)
db.execute("INSERT OR REPLACE INTO shows(id,title,total,selected,state,watched,mapping) VALUES(42,'合成成品续播测试',3,1,'watching',1,?)",(json.dumps({'season':1}),))
db.execute('INSERT OR REPLACE INTO watches(show_id,episode,finished) VALUES(42,1,1)')
engine=Engine(db,NoNetwork(),Sources(db,NoNetwork()),qa_root/'library')
engine.catalog=Catalog(db,NoNetwork());engine.thunder.reader=NoNetwork()
app=create_app(db,engine,start_worker=False)
app.state.finished_viewing.root=qa_root
catalog=qa_root/'static/finished-media'
index=json.loads((catalog/'index.json').read_text(encoding='utf-8'))
movie=catalog/index['releases'][0]['id']/'episode.mp4'
db.execute("INSERT OR REPLACE INTO episodes(show_id,episode,path,size,hash,status) VALUES(42,2,?,?,?,'complete')",(str(movie),movie.stat().st_size,'a'*40))
app.router.routes.insert(0,Mount('/static/finished-media',StaticFiles(directory=catalog)))
vendor=os.environ.get('ANIMESERVICE_QA_VENDOR_ROOT')
if vendor:app.router.routes.insert(0,Mount('/static/vendor',StaticFiles(directory=Path(vendor))))

@app.middleware('http')
async def fixture_origin(request,call_next):
    if request.method not in ('GET','HEAD'):
        if request.headers.get('x-player-qa-token')!=qa_token:
            return JSONResponse({'error':'Fixture token is required'},status_code=403)
        if request.headers.get('origin')==str(request.base_url).rstrip('/'):
            request.scope['headers']=[(k,v) for k,v in request.scope['headers'] if k!=b'origin']+[(b'origin',b'http://127.0.0.1:4871')]
    return await call_next(request)

@app.get('/api/testing/fixture')
def fixture():
    return {'isolated':db.path.resolve()==fixture_db.resolve(),'synthetic_media':True,
            'worker_enabled':False,'token_sha256':hashlib.sha256(qa_token.encode()).hexdigest(),
            'show_id':42,'releases':index['releases']}
