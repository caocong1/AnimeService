import contextlib,hashlib,json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.board import episode_board
from anime.db import Store
from anime.engine import Engine
from anime.show_list import ShowListSnapshot


def setup(tmp_path):
    db=Store(tmp_path/'test.db')
    engine=Engine(db,qbit=Mock(),sources=Mock(),library=tmp_path/'library')
    client=TestClient(create_app(db,engine,start_worker=False),base_url='http://127.0.0.1:4871')
    return db,engine,client


@pytest.mark.parametrize('state,authorized,hold',[
    ('watching',1,0),('trial',1,0),('wish',1,0),('paused',1,0),
    ('dropped',1,0),('completed',1,0),('watching',0,0),('watching',1,1)])
def test_batch_board_matches_detail_and_keeps_state(tmp_path,state,authorized,hold):
    db,engine,client=setup(tmp_path)
    mapping={'aliases':['Example'],'confirmed':True,'inventory_checked':True,
             'airing_offset':12,'offset':20,'start':1,'end':8,'required_title':'Part II'}
    db.upsert_subject({'id':1,'name':'Example','total_episodes':8})
    db.execute('UPDATE shows SET selected=1,state=?,authorized=?,auto_download=1,cleanup_hold=?,mapping=? WHERE id=1',
               (state,authorized,hold,json.dumps(mapping)))
    paths=[]
    for n in range(1,5):
        p=tmp_path/f'{n}.mp4';p.write_bytes(b'video');paths.append(p)
        db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(1,?,?,5,'complete')",(n,str(p)))
    paths[2].unlink();paths[3].write_bytes(b'wrong size')
    db.execute("INSERT INTO episodes(show_id,episode,status,progress) VALUES(1,5,'downloading',.42)")
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(1,1,1,'manual')")
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(1,3,1,'dandan')")
    key=hashlib.sha256(str(paths[1]).lower().encode()).hexdigest()[:32]
    db.execute('INSERT INTO web_progress(media_id,position,duration) VALUES(?,100,1400)',(key,))
    for ep,date in [(18,'2000-01-01'),(20,'2999-01-01')]:
        db.execute('INSERT INTO airings VALUES(1,?,?,?)',(ep,date,''))
    for title in ['[Group] Example Part II - 27 [1080p]',
                  '[Group] Example Part I - 28 [1080p]', '[Group] Other - 28 [1080p]']:
        db.execute('INSERT INTO candidates(show_id,title,url) VALUES(1,?,?)',(title,title))
    before=db.show(1);watches=db.rows('SELECT * FROM watches')
    expected=episode_board(db,engine,before)
    row=client.get('/api/shows?scope=history').json()[0]
    assert {'eps':row['eps'],'next':row['next']}==expected
    assert [e['s'] for e in row['eps'][:5]]==['watched','resume','watched','missing','downloading']
    assert row['downloaded']==4 and row['downloading']==1 and row['unwatched']==2
    assert row['eps'][5]['s']==('missing' if db.eligible(1) else 'aired')
    assert row['eps'][6]['s']==('missing' if db.eligible(1) else 'future')
    assert row['eps'][7]=={'n':8,'s':'future','date':'2999-01-01'}
    assert db.show(1)==before and db.rows('SELECT * FROM watches')==watches
    assert not engine.qb.mock_calls and not engine.sources.mock_calls


@pytest.mark.parametrize('count',[1,90,405])
def test_list_connections_and_queries_are_batched(tmp_path,monkeypatch,count):
    db,engine,client=setup(tmp_path)
    with db.connect() as c:
        c.executemany("INSERT INTO shows(id,title,selected,total) VALUES(?,?,1,12)",[(n,f'Example {n}') for n in range(1,count+1)])
        c.executemany("INSERT INTO airings VALUES(?,13,'2000-01-01','')",[(n,) for n in range(1,count+1)])
        c.executemany('INSERT INTO kv VALUES(?,?)',[(f'airing_sort_offset_{n}','12') for n in range(1,count+1)])
    opened=[];queries=[];original=db.connect
    @contextlib.contextmanager
    def counted():
        opened.append(1)
        with original() as c:
            c.set_trace_callback(queries.append)
            yield c
    monkeypatch.setattr(db,'connect',counted)
    response=client.get('/api/shows?scope=history')
    assert response.status_code==200 and len(response.json())==count
    assert len(opened)==1
    selects=[q for q in queries if q.startswith('SELECT')]
    assert len(selects)<=1+6*((count+399)//400)
    assert all(s['eps'][0]['s']=='aired' for s in response.json())
    assert not engine.qb.mock_calls and not engine.sources.mock_calls


def test_new_requests_see_progress_watch_file_and_membership_changes(tmp_path):
    db,engine,client=setup(tmp_path)
    db.upsert_subject({'id':1,'name':'Example','total_episodes':1})
    db.execute('UPDATE shows SET selected=1 WHERE id=1')
    p=tmp_path/'video.mp4';p.write_bytes(b'video')
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status,first_completed) VALUES(1,1,?,5,'complete',100)",(str(p),))
    row=client.get('/api/shows?scope=history').json()[0]
    assert row['eps'][0]['s']=='ready' and row['download_updated']==100
    db.execute("INSERT INTO web_progress(media_id,position,duration) VALUES(?,100,1000)",(row['next']['media'],))
    db.execute("INSERT INTO quarter_members VALUES(1,'2026-10','continuing','verified')")
    row=client.get('/api/shows?scope=history').json()[0]
    assert row['eps'][0]['s']=='resume' and row['quarter_kind']=='continuing'
    db.execute("INSERT INTO watches(show_id,episode,finished) VALUES(1,1,1)")
    assert client.get('/api/shows?scope=history').json()[0]['eps'][0]['s']=='watched'
    db.execute('UPDATE watches SET finished=0 WHERE show_id=1')
    p.unlink()
    assert client.get('/api/shows?scope=history').json()[0]['eps'][0]['s']=='missing'


def test_snapshot_stays_consistent_across_later_writes(tmp_path):
    db,engine,_=setup(tmp_path)
    db.upsert_subject({'id':1,'name':'Example','total_episodes':1})
    db.execute('UPDATE shows SET selected=1 WHERE id=1')
    snapshot=ShowListSnapshot(db,'selected=1',[],'',True)
    db.execute("UPDATE shows SET state='dropped' WHERE id=1")
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(1,1,1)')
    assert snapshot.shows[1]['state']=='wish' and snapshot.watched[1]==set()
    assert episode_board(db,engine,snapshot.shows[1],snapshot=snapshot)['eps']==[{'n':1,'s':'future'}]


def test_release_number_cannot_override_future_cour_schedule(tmp_path):
    db,engine,client=setup(tmp_path)
    db.upsert_subject({'id':1,'name':'Example Part 2','total_episodes':11})
    mapping={'aliases':['Example'],'confirmed':True,'inventory_checked':True,
             'start':1,'end':13,'airing_offset':13,
             'numbering':[{'min':1,'max':13,'offset':0},{'min':14,'max':26,'offset':13}]}
    db.execute('UPDATE shows SET selected=1,authorized=1,auto_download=1,mapping=? WHERE id=1',
               (json.dumps(mapping),))
    for ep,date in [(14,'2000-01-01'),(15,'2999-01-01'),(16,'2999-01-08')]:
        db.execute('INSERT INTO airings VALUES(1,?,?,?)',(ep,date,''))
    db.execute("INSERT INTO candidates(show_id,title,url) VALUES(1,'[Group] Example Part 2 - 13 [1080p]','test')")
    assert engine.gaps(1)['missing']==[1]
    detail=client.get('/api/shows/1').json()
    listing=client.get('/api/shows?scope=home').json()[0]
    assert detail['board']['eps']==listing['eps']
    assert detail['board']['eps'][0]['s']=='missing'
    assert detail['board']['eps'][1]=={'n':2,'s':'future','date':'2999-01-01'}
    assert all(e['s']=='future' for e in listing['eps'][1:])
    assert len(listing['eps'])==11
    assert listing['airing_schedule_version']==1
    assert not engine.qb.mock_calls and not engine.sources.mock_calls
