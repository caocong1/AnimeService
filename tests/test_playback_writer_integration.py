"""Passive legacy fences and real server identity lookup, synthetic media only."""
import datetime
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from anime.app import create_app
from anime.db import Store
from anime.dandan_history import DandanHistory
from anime.playback_writers import PlaybackMediaResolver, PlaybackWriters, WriterConflict, guard_legacy_progress


@pytest.fixture
def media(tmp_path):
    db=Store(tmp_path/'test.db')
    db.execute("INSERT INTO shows(id,title,total,selected,state,mapping) VALUES(42,'Test',2,1,'watching',?)",(json.dumps({'season':2}),))
    plain=tmp_path/'plain.mp4';plain.write_bytes(b'video')
    key=hashlib.sha256(str(plain).lower().encode()).hexdigest()[:32]
    (tmp_path/'inventory.json').write_text(json.dumps([{'path':str(plain),'size':5}]),encoding='utf-8')
    app=create_app(db,Mock(),start_worker=False)
    app.state.finished_viewing.root=tmp_path
    catalog=tmp_path/'static/finished-media'; rows=[]
    for n in range(2):
        release=f'test-v{n}';folder=catalog/release;folder.mkdir(parents=True)
        (folder/'episode.mp4').write_bytes(b'synthetic release')
        rows.append({'id':release,'show_id':42,'season':2,'episode':2,'status':'finished','video_sha256':str(n)*64})
    (catalog/'index.json').write_text(json.dumps({'releases':rows}),encoding='utf-8')
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    client.headers['X-Anime-Token']=client.get('/api/bootstrap').json()['token']
    return db,app,client,PlaybackMediaResolver(app.state.webplayer),key,plain,catalog


def claim(key, suffix='a'):
    return {'media_key':key,'session':'session-123456789-'+suffix,'writer_token':'writer-token-123456789012345678901234-'+suffix,
            'request_id':'request-123456789-'+suffix,'device_id':'device-'+suffix,'expected_generation':0,'mode':'open'}


def packet(sequence=1):
    return {'position':123,'duration':1420,'playing':True,'session':'legacy-session-123456789','sequence':sequence}


def test_real_resolver_aliases_share_existing_progress_keys_and_versions_are_independent(media):
    db,app,client,resolve,key,plain,catalog=media
    assert resolve(key)==key
    a=resolve('finished:test-v0');b=resolve('finished:test-v1')
    assert a!=b and resolve(a)==a and resolve(b)==b
    assert a==app.state.finished_viewing.item('test-v0')['media_id']
    assert not db.rows("SELECT name FROM sqlite_master WHERE name IN ('playback_writers','playback_claim_receipts')")
    assert not db.rows('SELECT * FROM web_progress')
    assert not any(p.startswith('/api/playback-sessions') for p in app.openapi()['paths'])


@pytest.mark.parametrize('identity',['../../plain.mp4','plain.mp4','finished:missing','0'*32,'finished:'+'f'*32])
def test_resolver_rejects_client_paths_and_unknown_aliases(media,identity):
    resolve=media[3]
    with pytest.raises((ValueError,HTTPException)):
        resolve(identity)


def test_resolver_rechecks_file_size_and_current_release_mapping(media):
    db,app,client,resolve,key,plain,catalog=media
    plain.write_bytes(b'replaced and larger')
    with pytest.raises(ValueError):resolve(key)
    old=resolve('finished:test-v0')
    index=json.loads((catalog/'index.json').read_text())
    index['releases'][0]['video_sha256']='a'*64
    (catalog/'index.json').write_text(json.dumps(index),encoding='utf-8')
    assert resolve('finished:test-v0')!=old
    with pytest.raises(ValueError):resolve(old)
    db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps({'season':9}),))
    with pytest.raises(ValueError):resolve('finished:test-v0')


def test_resolver_rejects_ambiguous_or_concurrently_changed_release_manifest(media,monkeypatch):
    db,app,client,resolve,key,plain,catalog=media
    index=json.loads((catalog/'index.json').read_text())
    duplicate={**index['releases'][0],'video_sha256':'a'*64}
    index['releases'].append(duplicate)
    (catalog/'index.json').write_text(json.dumps(index),encoding='utf-8')
    with pytest.raises(ValueError,match='ambiguous'):resolve('finished:test-v0')
    index['releases'].pop()
    (catalog/'index.json').write_text(json.dumps(index),encoding='utf-8')
    original=app.state.webplayer.item
    def moved(requested):
        index['releases'][0]['video_sha256']='b'*64
        (catalog/'index.json').write_text(json.dumps(index),encoding='utf-8')
        return original(requested)
    monkeypatch.setattr(app.state.webplayer,'item',moved)
    with pytest.raises(ValueError,match='changed during lookup'):resolve('finished:test-v0')


def test_default_off_legacy_routes_keep_working_without_creating_v2_tables(media):
    db,app,client,resolve,key,*_=media
    assert client.post(f'/api/web/media/{key}/progress',json={'position':10,'duration':100}).status_code==200
    assert client.post('/api/finished-viewing/test-v0/progress',json=packet()).status_code==200
    assert not db.rows("SELECT name FROM sqlite_master WHERE name='playback_writers'")


@pytest.mark.parametrize('route',['ordinary','finished','finished_alias'])
def test_durable_fence_blocks_each_legacy_route_before_receipt_write(media,route):
    db,app,client,resolve,key,*_=media
    requested=key if route=='ordinary' else 'finished:test-v0'
    writers=PlaybackWriters(db,resolve,enabled=lambda:True,clock=lambda:1000)
    writers.claim(claim(requested))
    # Expired/feature-off still fenced. The production enable callback is absent.
    writers.enabled=lambda:False
    path=(f'/api/web/media/{key}/progress' if route=='ordinary' else
          '/api/web/media/finished:test-v0/progress' if route=='finished_alias' else '/api/finished-viewing/test-v0/progress')
    r=client.post(path,json=packet())
    assert r.status_code==409 and r.json()['reason']=='session_required'
    assert not db.rows('SELECT * FROM web_progress') and not db.rows('SELECT * FROM finished_playback_receipts')
    assert not db.rows('SELECT * FROM watches')
    assert client.post('/api/finished-viewing/test-v1/progress',json=packet()).status_code==200


def test_trusted_enable_hook_blocks_unclaimed_legacy_without_creating_tables(media):
    db,app,client,resolve,key,*_=media
    db.playback_writer_enabled=lambda:True
    assert client.post(f'/api/web/media/{key}/progress',json=packet()).status_code==409
    assert client.post('/api/finished-viewing/test-v0/progress',json=packet()).status_code==409
    assert not db.rows("SELECT name FROM sqlite_master WHERE name='playback_writers'")


def test_history_fence_skips_progress_but_keeps_independent_watched_and_manual_priority(media):
    db,app,client,resolve,key,plain,_=media
    writers=PlaybackWriters(db,resolve,enabled=lambda:True);writers.claim(claim(key))
    db.execute('INSERT INTO web_progress(media_id,position,duration,updated) VALUES(?,?,?,?)',(key,10,100,0))
    h=DandanHistory(db);now=time.time()-100;stamp=datetime.datetime.fromtimestamp(now,datetime.timezone.utc).isoformat()
    entries={key:{'id':key,'show_id':42,'episode':2,'path':str(plain),'size':5,'name':plain.name,'folder':'Test'}}
    data={'recent':[{'path':str(plain),'played':stamp,'seconds':50}],
          'files':[{'path':str(plain),'size':5,'duration':100,'episode_id':'9002'}],
          'seen':[{'episode_id':'9002','watched':stamp}]}
    result=h.merge(data,entries)
    assert result['progress_fenced']==1 and result['resume_files']==0 and result['synced_watched']==1
    assert db.one('SELECT position FROM web_progress')['position']==10
    assert db.one('SELECT * FROM watches')['finished']==1
    db.execute("UPDATE watches SET finished=0,method='manual'")
    h.merge(data,entries)
    assert db.one('SELECT * FROM watches')['finished']==0 and db.show(42)['watched']==0


@pytest.mark.parametrize('route',['ordinary','finished'])
def test_rejected_legacy_packet_preserves_existing_receipt_and_saved_position(media,route):
    db,app,client,resolve,key,*_=media
    requested=key if route=='ordinary' else 'finished:test-v0'
    path=f'/api/web/media/{key}/progress' if route=='ordinary' else '/api/finished-viewing/test-v0/progress'
    assert client.post(path,json=packet()).status_code==200
    writers=PlaybackWriters(db,resolve,enabled=lambda:True);writers.claim(claim(requested))
    response=client.post(path,json={**packet(2),'position':456})
    assert response.status_code==409
    assert db.one('SELECT sequence FROM finished_playback_receipts')['sequence']==1
    assert db.one('SELECT position FROM web_progress')['position']==123


@pytest.mark.parametrize('route',['ordinary','finished'])
def test_progress_insert_failure_rolls_back_the_session_receipt(media,route):
    db,app,client,resolve,key,*_=media
    db.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON web_progress BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    path=f'/api/web/media/{key}/progress' if route=='ordinary' else '/api/finished-viewing/test-v0/progress'
    safe_client=TestClient(app,base_url='http://127.0.0.1:4871',raise_server_exceptions=False)
    safe_client.headers.update(client.headers)
    assert safe_client.post(path,json=packet()).status_code==503
    assert not db.rows('SELECT * FROM finished_playback_receipts')
    assert not db.rows('SELECT * FROM web_progress')


def test_guard_requires_transaction_and_serializes_against_a_real_concurrent_claim(media):
    db,app,client,resolve,key,*_=media
    writers=PlaybackWriters(db,resolve,enabled=lambda:True)
    started=Event()
    def concurrent_claim():
        started.set();return writers.claim(claim(key))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.connect() as c:
            with pytest.raises(ValueError,match='explicit transaction'):guard_legacy_progress(c,key)
            c.execute('BEGIN IMMEDIATE');guard_legacy_progress(c,key)
            task=pool.submit(concurrent_claim);assert started.wait(2)
            assert not task.done()
            c.execute('INSERT INTO web_progress(media_id,position,duration) VALUES(?,?,?)',(key,10,100))
        assert task.result(timeout=5)['ok']
    assert client.post(f'/api/web/media/{key}/progress',json=packet()).status_code==409
    assert db.one('SELECT position FROM web_progress WHERE media_id=?',(key,))['position']==10
