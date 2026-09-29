import datetime
import pytest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from anime.app import create_app,catalog_quarters
from anime.db import Store


@pytest.fixture(autouse=True)
def deployment_settings(monkeypatch):
    monkeypatch.setattr('anime.app.load_deployment',lambda:{'public_authorities':['anime.example.test']})


def client_at(tmp_path):
    db=Store(tmp_path/'data/test.db');engine=Mock();engine.gaps.return_value={'missing':[3],'unverified_files':[],'active':True}
    app=create_app(db,engine,start_worker=False)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    return client,db,{'X-Anime-Token':client.get('/api/bootstrap').json()['token'],'Origin':'http://127.0.0.1:4871'}


@pytest.mark.parametrize('day,expected',[
    ('2026-09-29',['2026-07','2026-10']),('2026-12-15',['2026-10','2027-01']),('2027-02-01',['2026-10','2027-01'])])
def test_catalog_quarters_follow_the_date(day,expected):
    assert catalog_quarters(datetime.date.fromisoformat(day))==expected


def test_episode_board_states_without_mutation(tmp_path):
    client,db,_=client_at(tmp_path)
    db.upsert_subject({'id':42,'name':'Example','total_episodes':5})
    db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=42")
    for ep in (1,2):
        p=tmp_path/f'{ep}.mp4';p.write_bytes(b'video')
        db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(42,?,?,5,'complete')",(ep,str(p)))
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status,progress) VALUES(42,4,'',0,'downloading',.5)")
    db.execute("INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)")
    db.execute("INSERT INTO airings VALUES(42,5,'2999-01-01','')")
    before=db.show(42)
    row=client.get('/api/shows?scope=home').json()[0]
    assert [e['s'] for e in row['eps']]==['watched','ready','missing','downloading','future']
    assert row['next']['n']==2 and row['next']['media'] and row['eps'][3]['progress']==50
    assert row['eps'][4]['date']=='2999-01-01'
    detail=client.get('/api/shows/42').json()
    assert detail['board']['next']['n']==2 and detail['episodes'][1]['media_id']==row['next']['media']
    assert db.show(42)==before and len(db.rows('SELECT * FROM watches'))==1


def test_unselect_only_undoes_a_fresh_wish(tmp_path):
    client,db,h=client_at(tmp_path)
    db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    db.execute("UPDATE shows SET selected=1,state='wish' WHERE id=42")
    assert client.post('/api/shows/42/unselect',headers=h,json={}).status_code==200
    assert db.show(42)['selected']==0
    db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=42")
    assert client.post('/api/shows/42/unselect',headers=h,json={}).status_code==400
    db.execute("UPDATE shows SET state='wish',authorized=1 WHERE id=42")
    assert client.post('/api/shows/42/unselect',headers=h,json={}).status_code==400
    assert db.show(42)['selected']==1


def test_pages_and_old_addresses(tmp_path):
    client,db,_=client_at(tmp_path)
    for path in ['/','/season','/library','/show/42','/manage','/watch','/login']:
        r=client.get(path);assert r.status_code==200 and 'text/html' in r.headers['content-type'],path
    r=client.get('/history-sync?show=7',follow_redirects=False)
    assert r.status_code==308 and r.headers['location']=='/library?tab=history&show=7'
    assert client.get('/transition',follow_redirects=False).headers['location']=='/manage#legacy'


def test_login_assets_are_public_and_app_assets_are_not(tmp_path):
    client,db,_=client_at(tmp_path)
    remote=TestClient(client.app,base_url='https://anime.example.test')
    for path in ['/login','/static/tokens.css','/static/app.css','/static/theme.js','/static/login.js']:
        assert remote.get(path,follow_redirects=False).status_code==200,path
    for path in ['/static/core.js','/static/home.js','/season']:
        assert remote.get(path,follow_redirects=False).status_code==303,path


def test_history_pending_lists_only_linked_unwatched(tmp_path):
    client,db,_=client_at(tmp_path)
    db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    for mid,sid,ep in [('a',42,1),('b',42,2),('c',None,None)]:
        db.execute('INSERT INTO dandan_history(media_id,show_id,episode,name,title,last_played) VALUES(?,?,?,?,?,1)',(mid,sid,ep,mid,'Example'))
    db.execute("INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)")
    d=client.get('/api/history/dandan?pending=1').json()
    assert d['total']==1 and [r['media_id'] for r in d['items']]==['b']
    assert client.get('/api/history/dandan').json()['total']==3


def test_home_board_cost_does_not_grow_with_candidates(tmp_path,monkeypatch):
    import contextlib,json
    from anime.engine import Engine
    db=Store(tmp_path/'data/test.db');engine=Engine(db,Mock(),Mock(),tmp_path/'library')
    db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    db.execute("UPDATE shows SET selected=1,state='watching',mapping=? WHERE id=42",(json.dumps({'aliases':['Example'],'confirmed':True,'inventory_checked':True}),))
    for i in range(300):
        db.execute("INSERT INTO candidates(show_id,source_id,title,url) VALUES(42,1,?,?)",(f'[Group] Example - {i%12+1:02d} [1080p]',f'u{i}'))
    client=TestClient(create_app(db,engine,start_worker=False),base_url='http://127.0.0.1:4871')
    opened=[0];original=Store.connect
    @contextlib.contextmanager
    def counted(self):
        opened[0]+=1
        with original(self) as c:yield c
    monkeypatch.setattr(Store,'connect',counted)
    assert client.get('/api/shows?scope=home').status_code==200
    # One show: a fixed number of queries, never one per candidate or per episode.
    assert opened[0]<40,opened[0]
