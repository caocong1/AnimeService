"""Owner review boundaries with synthetic state and no production clients."""
import hashlib,json,os,time
from pathlib import Path
from unittest.mock import Mock
import pytest
from test_reliability import setup
from test_subtitle_delivery import delivery,begin
from test_dandan_history import sample
from test_delivery_operator_tools import queue_cli
from anime.db import Store
from anime.browser_media import BrowserMedia
from anime.sources import reserve_request
from anime.net import RequestDeferred
from anime.webplayer import WebPlayer


@pytest.mark.parametrize('count',[0,1,1999,2000,2001,2010])
def test_retention_keeps_newest_two_thousand_even_when_expired(tmp_path,count):
    db=Store(tmp_path/'test.db');now=time.time()
    with db.connect() as c:
        c.executemany('INSERT INTO events(time,level,scope,message) VALUES(?,?,?,?)',[(now-40*86400,'info','old','synthetic')]*count)
    assert db.prune_events(now=now)==max(0,count-2000)
    assert db.one('SELECT count(*) n FROM events')['n']==min(count,2000)


@pytest.mark.parametrize('invalid',[0,-1,3651,False,True,'1',1.5,'bad',float('inf')])
def test_invalid_retention_setting_uses_thirty_days_without_coercion(tmp_path,invalid):
    db=Store(tmp_path/'test.db');now=time.time()
    with db.connect() as c:
        c.execute('INSERT INTO events(time,level,scope,message) VALUES(?,?,?,?)',(now-2*86400,'info','old','x'))
        c.executemany('INSERT INTO events(time,level,scope,message) VALUES(?,?,?,?)',[(now,'info','new','x')]*2000)
    assert db.prune_events(keep_days=invalid,now=now)==0
    assert db.prune_events(keep_days=1,now=now)==1


def test_source_scheduler_gives_oldest_unpolled_source_first_slot(setup):
    db,q,e,c=setup
    db.upsert_subject({'id':43,'name':'Synthetic second','total_episodes':12})
    db.execute("UPDATE shows SET mapping=?,selected=1,state='watching',authorized=1 WHERE id=43",(db.show(42)['mapping'],))
    db.execute("INSERT INTO episodes(show_id,episode,path,status) VALUES(42,1,'synthetic','complete')")
    old=db.execute("INSERT INTO sources(show_id,kind,url,last_attempt,next_attempt) VALUES(42,'tokyotosho','https://www.tokyotosho.info/rss.php?terms=old',1,100)")
    db.execute("INSERT INTO sources(show_id,kind,url,last_attempt,next_attempt) VALUES(43,'tokyotosho','https://www.tokyotosho.info/rss.php?terms=new',2,0)")
    e.maintenance=Mock();e.source_review=Mock();e.subtitle_studio=Mock();e.sources=Mock();e.sources.rss.return_value=False
    e.cycle()
    assert e.sources.rss.call_args_list[0].args[0]['id']==old and q.adds==0


def test_unchanged_history_still_merges_against_new_library_membership(tmp_path):
    db,h,media,data,t=sample(tmp_path)
    h.source_signature=lambda:[[10,20],None];h.extract=Mock(return_value=data)
    h.merge=Mock(side_effect=[{'ok':True,'total':0},{'ok':True,'total':1}])
    assert h.sync()['total']==0 and h.sync()['total']==1
    assert h.extract.call_count==1 and h.merge.call_count==2


def test_cache_hit_refreshes_lru_and_open_copy_does_not_fail_playback(tmp_path,monkeypatch):
    sub=Mock();sub.db.path=tmp_path/'db';sub.fingerprint.return_value='a'*64
    cache=BrowserMedia(sub);cache.root.mkdir(parents=True)
    target=cache.root/('a'*64+'.mp4');target.write_bytes(b'a'*10)
    target.with_suffix('.json').write_text(json.dumps({'fingerprint':'a'*64,'mode':'packet_copy','size':10}))
    os.utime(target,(1,1));assert cache.cached({})==target and target.stat().st_mtime>1
    keep=cache.root/('b'*64+'.mp4');keep.write_bytes(b'b'*10)
    original=Path.unlink
    def unlink(path,*a,**k):
        if path==target:raise PermissionError('synthetic open file')
        return original(path,*a,**k)
    monkeypatch.setattr(Path,'unlink',unlink);cache.trim(keep,limit=10)
    assert keep.exists() and target.exists()
    monkeypatch.setattr(os,'utime',Mock(side_effect=PermissionError('synthetic metadata lock')))
    assert cache.cached({})==target


def test_absence_evidence_is_bound_to_dispatch_and_real_file_hash(tmp_path):
    path=tmp_path/'evidence.json';record={'id':'synthetic','dispatch_token':'current','updated':0}
    data={'checked_at':'2026-10-18T00:00:00+00:00','checked_by':'operator','method':'complete dispatcher listing',
          'job_id':'synthetic','dispatch_token':'current','sha256':'forged','file':'forged'}
    path.write_text(json.dumps(data),encoding='utf-8');proof=queue_cli.absence_evidence(path,record)
    assert proof['sha256']==hashlib.sha256(path.read_bytes()).hexdigest() and proof['file']==path.name
    with pytest.raises(ValueError):queue_cli.absence_evidence(path,{**record,'dispatch_token':'next'})
    with pytest.raises(ValueError):queue_cli.absence_evidence(path,{**record,'updated':time.time()+86400})


def test_absence_receipt_cannot_release_an_acknowledged_owner(delivery):
    db,scheduler,ready,now=delivery
    identity,intent=begin(scheduler)
    scheduler.reconcile_dispatch(identity,intent['dispatch_token'],thread_id='actual-synthetic-owner')
    before=scheduler.get(identity)
    with pytest.raises(ValueError,match='preserved'):
        scheduler.reconcile_dispatch(identity,intent['dispatch_token'],definitely_not_created=True,absence_evidence='synthetic proof')
    assert scheduler.get(identity)==before


def test_alias_grouping_preserves_old_cooldown_and_merges_counts_once(tmp_path):
    db=Store(tmp_path/'test.db');deadline=time.time()+1800
    db.set('site_health_bt4g.org',{'next_attempt':deadline,'failures':4,'stats':{'polls':5,'failed_site':5}})
    for _ in range(2):
        with pytest.raises(RequestDeferred) as blocked:reserve_request(db,'https://bt4gprx.com/search/synthetic')
        assert blocked.value.until==deadline
    state=db.get('site_health_bt4g')
    assert state['failures']==4 and state['stats']['polls']==5


def test_concurrent_inventory_readers_share_one_hashed_version(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    db=Store(tmp_path/'test.db')
    (db.path.parent/'inventory.json').write_text(json.dumps([{'path':str(tmp_path/'a.mp4'),'size':1}]))
    player=WebPlayer(db);hasher=Mock(wraps=hashlib.sha256)
    monkeypatch.setattr('anime.webplayer.hashlib.sha256',hasher)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(lambda _:player.inventory_rows(),range(100)))
    assert all(row is rows[0] for row in rows) and hasher.call_count==1
