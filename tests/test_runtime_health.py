"""Cheap liveness, isolated scheduler steps and a clean stop: the running service tells the truth about itself."""
import threading,time
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from test_reliability import setup
from anime.app import create_app
from anime.db import Store
from anime.run import watch_stop


@pytest.fixture(autouse=True)
def deployment_settings(monkeypatch):
    monkeypatch.setattr('anime.app.load_deployment',lambda:{'lan_network':'192.168.1.0/24','lan_authority':'192.168.1.100:4871'})


def test_health_is_cheap_and_reports_liveness_and_version(tmp_path):
    db=Store(tmp_path/'data/test.db');app=create_app(db,Mock(),start_worker=False)
    client=TestClient(app,base_url='http://localhost:4871')
    h=client.get('/api/health').json()
    assert h['service']=='AnimeService' and h['ok'] is False and h['heartbeat']=={'phase':None,'age':None}
    assert h['code_version'] and h['pid']>0 and h['uptime']>=0 and h['worker_thread'] is False
    assert not {'tasks','sources','events','watch_counts'}&set(h)
    db.set('heartbeat',{'time':time.time()-5,'phase':'idle'});db.set('qbit_health',{'ok':True});db.set('download_maintenance_at',time.time()-7)
    h=client.get('/api/health').json()
    assert h['ok'] is True and h['heartbeat']['phase']=='idle' and 4<=h['heartbeat']['age']<=6 and h['qbit'] is True and 6<=h['maintenance_age']<=8
    db.set('heartbeat',{'time':time.time()-3600,'phase':'source'})
    assert client.get('/api/health').json()['ok'] is False
    db.set('cycle_errors',{'subtitle_studio.discover':{'error':'x','count':3}})
    assert client.get('/api/health').json()['cycle_errors']['subtitle_studio.discover']['count']==3


def test_cycle_isolates_a_failing_step_and_still_reaches_idle(setup):
    db,q,e,c=setup
    e.subtitle_studio=Mock();e.subtitle_studio.discover.side_effect=RuntimeError('core missing')
    e.sources=Mock();e.catalog=Mock();db.set('check_requested',1)
    for _ in range(3):e.cycle(force=True)
    assert db.get('heartbeat')['phase']=='idle' and db.get('check_completed')==1
    err=db.get('cycle_errors')['subtitle_studio.discover']
    assert err['count']==3 and err['error'].startswith('RuntimeError：') and 'core missing' not in err['error']
    assert e.subtitle_studio.tick.call_count>=3  # later steps still ran
    assert len(db.rows("SELECT * FROM events WHERE scope='cycle:subtitle_studio.discover'"))==1
    e.subtitle_studio.discover.side_effect=None;e.cycle(force=True)
    assert 'subtitle_studio.discover' not in db.get('cycle_errors')


def test_studio_worker_records_a_failing_delivery_refresh_instead_of_dying(setup):
    db,q,e,c=setup;studio=e.subtitle_studio
    db.set('subtitle_studio_enabled',True);db.set('subtitle_delivery_policy',{'enabled':True})
    studio.media=lambda:[];studio.delivery=Mock();studio.delivery.refresh.side_effect=RuntimeError('core missing')
    studio.tick()
    for t in threading.enumerate():
        if t.name=='subtitle-studio-archive':t.join(5)
    health=db.get('subtitle_studio_health')
    assert health['ok'] is False and health['error'].startswith('RuntimeError：') and studio.lock.acquire(False)
    studio.lock.release()
    assert db.rows("SELECT * FROM events WHERE scope='subtitle-delivery' AND level='error'")
    studio.delivery.refresh.side_effect=None;studio.tick()
    for t in threading.enumerate():
        if t.name=='subtitle-studio-archive':t.join(5)
    assert db.get('subtitle_studio_health')['ok'] is True


def test_stop_request_file_stops_the_server_cleanly(tmp_path):
    server=Mock();server.should_exit=False;flag=tmp_path/'stop.request'
    watch_stop(server,flag=flag,interval=0.05)
    time.sleep(0.2);assert server.should_exit is False
    flag.write_text('');deadline=time.time()+2
    while not server.should_exit and time.time()<deadline:time.sleep(0.05)
    assert server.should_exit is True


def test_health_problems_follow_the_status_rules_and_events_are_pruned(setup):
    db,q,e,c=setup
    from anime.runtime import problems
    now=time.time()
    assert problems(db,now)=={'reviews':0,'uncertain':0,'error':None,'blocker':''}
    e.ingest(c());h=next(iter(q.tasks))
    db.event('task:'+h,'historical error','error');db.execute("UPDATE tasks SET status='complete' WHERE hash=?",(h,))
    assert problems(db,now)['error'] is None  # a completed task's old error is history, not a problem
    db.execute("UPDATE tasks SET status='downloading',error='still failing' WHERE hash=?",(h,))
    assert problems(db,now)['error']['scope']=='task:'+h
    db.execute("UPDATE tasks SET status='review' WHERE hash=?",(h,))
    assert problems(db,now)['reviews']==1 and problems(db,now)['error'] is None
    db.event('api','request failed','error');db.event('source:9','rss broken','error')
    assert problems(db,now)['error']['scope']=='source:9'
    assert problems(db,now+7200)['error'] is None  # older than an hour
    db.set('downloader_preference','thunder_first');assert problems(db,now)['blocker']
    for i in range(10):db.execute('INSERT INTO events(time,level,scope,message) VALUES(?,?,?,?)',(now-40*86400,'info','old','x'))
    assert db.prune_events(now=now)==10 and db.rows("SELECT * FROM events WHERE scope='old'")==[]
    db.set('events_keep_days',1);db.execute('INSERT INTO events(time,level,scope,message) VALUES(?,?,?,?)',(now-2*86400,'info','older','y'))
    assert db.prune_events(now=now)==1 and db.rows("SELECT * FROM events WHERE scope='source:9'")  # recent rows stay
