import hashlib,time
import pytest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store

@pytest.fixture(autouse=True)
def deployment_settings(monkeypatch):
    monkeypatch.setattr('anime.app.load_deployment', lambda: {
        'lan_network': '192.168.1.0/24', 'lan_authority': '192.168.1.100:4871',
        'public_authorities': ['anime.example.test', 'anime.example.test:443', 'anime.example.test:38443']})


def app_at(tmp_path):
    db=Store(tmp_path/'data/test.db');engine=Mock()
    app=create_app(db,engine,start_worker=False)
    return app,db


def test_ui_revalidates_on_normal_and_conditional_reload(tmp_path):
    app,db=app_at(tmp_path)
    client=TestClient(app,base_url='http://localhost:4871')
    for path in ('/manage','/','/watch','/static/core.js?v=2','/static/manage.js?v=2','/static/app.css'):
        response=client.get(path)
        cache='no-store' if path=='/manage' else 'no-cache'
        assert response.status_code==200 and response.headers['cache-control']==cache
        conditional=client.get(path,headers={'If-None-Match':response.headers['etag']})
        assert conditional.status_code==(304 if path.startswith('/static/') else 200)
        assert conditional.headers['cache-control']==cache
    html=client.get('/manage').text
    assert '/static/core.js?v=2' in html and '/static/manage.js?v=2' in html
    assert client.get('/api/bootstrap').headers['cache-control']=='no-store'


def test_manage_has_one_canonical_entry_and_keeps_error_history(tmp_path):
    app,db=app_at(tmp_path)
    client=TestClient(app,base_url='http://localhost:4871')
    old=client.get('/manage?v=2',follow_redirects=False)
    assert old.status_code==303 and old.headers['location']=='/manage'
    assert old.headers['cache-control']=='no-store'
    db.execute("INSERT INTO tasks(hash,show_id,status,error) VALUES('abc',42,'review','ownership conflict')")
    db.event('task:abc','old failure','error');db.event('service','live failure','error')
    events=client.get('/api/status').json()['events']
    past=next(e for e in events if e['scope']=='task:abc')
    assert past['level']=='warning' and past['original_level']=='error' and past['historical']
    assert next(e for e in events if e['scope']=='service')['level']=='error'
    assert db.one("SELECT level FROM events WHERE scope='task:abc'")['level']=='error'
    db.execute("UPDATE tasks SET status='downloading',error='still failing' WHERE hash='abc'")
    assert next(e for e in client.get('/api/status').json()['events'] if e['scope']=='task:abc')['level']=='error'


@pytest.mark.parametrize('base,origin', [
    ('http://127.0.0.1:4871','http://127.0.0.1:4871'),
    ('http://localhost:4871','http://localhost:4871'),
    ('http://127.0.0.1:4871','http://127.0.0.1'),
    ('http://localhost:4871','http://localhost'),
])
def test_local_browser_can_drop_show_without_touching_downloaders(tmp_path,base,origin):
    from anime.engine import Engine
    db=Store(tmp_path/'data/test.db')
    downloader=Mock();sources=Mock()
    engine=Engine(db,downloader,sources,tmp_path/'library')
    db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    db.execute("UPDATE shows SET selected=1,state='watching',authorized=1 WHERE id=42")
    app=create_app(db,engine,start_worker=False)
    client=TestClient(app,base_url=base)
    token=client.get('/api/bootstrap').json()['token']
    response=client.post('/api/shows/42/state',json={'state':'dropped'},headers={
        'Origin':origin,'Sec-Fetch-Site':'same-origin','X-Anime-Token':token})
    assert response.status_code==200
    assert db.show(42)['state']=='dropped' and not db.eligible(42)
    assert not downloader.mock_calls and not sources.mock_calls


@pytest.mark.parametrize('origin', ['https://evil.invalid','http://127.0.0.1:9999','null'])
def test_untrusted_state_origin_never_reaches_engine(tmp_path,origin):
    app,db=app_at(tmp_path)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    token=client.get('/api/bootstrap').json()['token']
    response=client.post('/api/shows/42/state',json={'state':'dropped'},headers={
        'Origin':origin,'Sec-Fetch-Site':'cross-site','X-Anime-Token':token})
    assert response.status_code==403
    app.state.engine.state.assert_not_called()


@pytest.mark.parametrize('site', ['', 'cross-site', 'same-site', 'none'])
def test_portless_origin_requires_same_origin_fetch_metadata(tmp_path,site):
    app,db=app_at(tmp_path)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    token=client.get('/api/bootstrap').json()['token']
    response=client.post('/api/shows/42/state',json={'state':'dropped'},headers={
        'Origin':'http://127.0.0.1','Sec-Fetch-Site':site,'X-Anime-Token':token})
    assert response.status_code==403
    app.state.engine.state.assert_not_called()


def test_portless_origin_still_requires_token_and_matching_loopback_host(tmp_path):
    app,db=app_at(tmp_path)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    headers={'Origin':'http://127.0.0.1','Sec-Fetch-Site':'same-origin'}
    assert client.post('/api/shows/42/state',json={'state':'dropped'},headers=headers).status_code==403
    token=client.get('/api/bootstrap').json()['token']
    headers.update({'Origin':'http://localhost','X-Anime-Token':token})
    assert client.post('/api/shows/42/state',json={'state':'dropped'},headers=headers).status_code==403
    remote=TestClient(app,base_url='https://anime.example.test:38443')
    # Login itself is unauthenticated, so this exercises the remote Origin guard.
    assert remote.post('/api/access/login',json={'code':'invalid'},headers={
        'Origin':'https://anime.example.test','Sec-Fetch-Site':'same-origin'}).status_code==403
    app.state.engine.state.assert_not_called()

def test_remote_access_pairing_csrf_and_revocation(tmp_path):
    app,db=app_at(tmp_path)
    local=TestClient(app,base_url='http://127.0.0.1:4871')
    remote=TestClient(app,base_url='https://anime.example.test')
    assert remote.get('/api/bootstrap').status_code==401
    assert remote.get('/',follow_redirects=False).status_code==303
    assert remote.get('/login').status_code==200
    token=local.get('/api/bootstrap').json()['token'];headers={'X-Anime-Token':token}
    assert local.post('/api/access/pair').status_code==403
    code=local.post('/api/access/pair',headers=headers,json={}).json()['code']
    assert remote.post('/api/access/login',json={'code':code}).status_code==403
    res=remote.post('/api/access/login',headers={'Origin':'https://anime.example.test'},json={'code':code})
    assert res.status_code==200
    assert all(flag in res.headers['set-cookie'].lower() for flag in ['httponly','secure','samesite=strict'])
    assert remote.get('/api/bootstrap').status_code==200
    assert remote.get('/api/bootstrap').json()['local'] is False
    assert remote.post('/api/access/login',headers={'Origin':'https://anime.example.test'},json={'code':code}).status_code==401
    assert remote.post('/api/access/pair',headers={**headers,'Origin':'https://anime.example.test'},json={}).status_code==403
    assert remote.post('/api/access/logout',headers={**headers,'Origin':'https://evil.invalid'},json={}).status_code==403
    assert remote.post('/api/thunder/claim',headers={**headers,'Origin':'https://anime.example.test'},json={}).status_code==403
    assert remote.post('/api/player/open',headers={**headers,'Origin':'https://anime.example.test'},json={}).status_code==403
    assert local.post('/api/access/revoke',headers=headers,json={}).status_code==200
    assert remote.get('/api/bootstrap').status_code==401
    assert local.get('/api/bootstrap',headers={'Host':'evil.invalid'}).status_code==403

def test_home_only_verified_unwatched_files_without_mutations(tmp_path):
    app,db=app_at(tmp_path);client=TestClient(app,base_url='http://127.0.0.1:4871')
    db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=42")
    p=tmp_path/'ep.mp4';p.write_bytes(b'video')
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(42,1,?,5,'complete')",(str(p),))
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(42,2,?,5,'complete')",(str(tmp_path/'missing.mp4'),))
    before=db.show(42)
    d=client.get('/api/home').json();assert d['unwatched']==1 and d['next'][0]['episode']==1
    assert 'path' not in d['next'][0] and 'size' not in d['next'][0]
    assert db.show(42)==before and not db.rows('SELECT * FROM watches')
    db.execute("UPDATE shows SET state='paused' WHERE id=42")
    assert client.get('/api/home').json()['next']==[]


def test_lan_without_pairing_keeps_csrf_and_local_restrictions(tmp_path):
    app,db=app_at(tmp_path)
    origin='http://192.168.1.100:4871'
    lan=TestClient(app,base_url=origin,client=('192.168.1.20',50000))
    assert lan.get('/',follow_redirects=False).status_code==200
    assert lan.get('/login',follow_redirects=False).headers['location']=='/'
    bootstrap=lan.get('/api/bootstrap')
    assert bootstrap.status_code==200 and bootstrap.json()['local'] is False
    token=bootstrap.json()['token']
    headers={'Origin':origin,'X-Anime-Token':token}
    for path in ['/api/access/pair','/api/access/revoke','/api/thunder/claim','/api/player/open']:
        assert lan.post(path,headers=headers,json={}).status_code==403
    for bad_origin in [None,'http://192.168.1.100','https://evil.invalid']:
        h={'X-Anime-Token':token}
        if bad_origin:h['Origin']=bad_origin
        assert lan.post('/api/access/logout',headers=h,json={}).status_code==403
    assert lan.post('/api/access/logout',headers={'Origin':origin},json={}).status_code==403
    assert lan.post('/api/access/logout',headers=headers,json={}).status_code==200
    assert lan.get('/api/bootstrap').status_code==200
    remote=TestClient(app,base_url='https://anime.example.test:38443',client=('192.168.1.20',50000))
    assert remote.get('/api/bootstrap').status_code==401
    assert not app.state.engine.mock_calls


@pytest.mark.parametrize('peer,base',[
    ('203.0.113.20','http://192.168.1.100:4871'),
    ('192.168.2.20','http://192.168.1.100:4871'),
    ('192.168.1.20','http://192.168.1.166:4871'),
    ('192.168.1.20','http://127.0.0.1:4871'),
    ('192.168.1.20','http://anime.example.test:38443'),
])
def test_lan_rejects_untrusted_peers_and_hosts(tmp_path,peer,base):
    app,db=app_at(tmp_path)
    client=TestClient(app,base_url=base,client=(peer,50000))
    assert client.get('/login',headers={'X-Forwarded-For':'127.0.0.1','X-Forwarded-Proto':'https'}).status_code==403
