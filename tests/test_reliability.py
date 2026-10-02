import hashlib,json,tempfile,time
from pathlib import Path
from unittest.mock import Mock
import pytest
from anime.db import Store
from anime.engine import Engine
from anime.releases import bdecode,validate_title,Rejected,episodes_of,select_files
from anime.sources import Sources

def encode(v):
    if isinstance(v,int):return b'i'+str(v).encode()+b'e'
    if isinstance(v,str):v=v.encode()
    if isinstance(v,bytes):return str(len(v)).encode()+b':'+v
    if isinstance(v,list):return b'l'+b''.join(encode(x) for x in v)+b'e'
    return b'd'+b''.join(encode(k)+encode(v[k]) for k in sorted(v))+b'e'
def torrent(episodes=(1,),season=1):
    return encode({b'announce':b'https://example.invalid/announce',b'info':{b'name':b'Example',b'piece length':16384,b'pieces':b'x'*20,b'files':[{b'length':100,b'path':[f'Example S{season:02}E{e:02}.mkv'.encode()]} for e in episodes]}})
class FakeQbit:
    def __init__(self):self.tasks={};self.fs={};self.adds=0;self.starts=[];self.stops=[];self.online=True;self.ambiguous=False
    def info(self,h=None):
        if not self.online:raise ConnectionError('offline')
        return [dict(t) for k,t in self.tasks.items() if h is None or k==h]
    def owned(self,t):return t['category']=='AnimeService' and t['tags']=='AnimeService-v1'
    def add(self,h,data,path):
        self.adds+=1;_,files=bdecode(data)
        self.tasks[h]={'hash':h,'category':'AnimeService','tags':'AnimeService-v1','save_path':path,'progress':0,'state':'stoppedDL'}
        self.fs[h]=[dict(f,progress=0,priority=1) for f in files]
        if self.ambiguous:self.ambiguous=False;raise TimeoutError('response lost after accepted')
    def files(self,h):return self.fs[h]
    def priorities(self,h,ids,priority):
        for f in self.fs[h]:
            if f['index'] in ids:f['priority']=int(priority)
    def stop(self,h):self.stops.append(h);self.tasks[h]['state']='stoppedDL'
    def start(self,h):self.starts.append(h);self.tasks[h]['state']='downloading'
class FakeSources:
    def __init__(self,data):self.data=data
    def torrent(self,c):return self.data

@pytest.fixture
def setup(tmp_path):
    db=Store(tmp_path/'data/test.db');db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    mapping={'aliases':['Example'],'season':1,'offset':0,'start':1,'end':12,'confirmed':True,'inventory_checked':True}
    db.execute("UPDATE shows SET mapping=?,selected=1,state='watching',authorized=1 WHERE id=42",(json.dumps(mapping),))
    qb=FakeQbit();eng=Engine(db,qb,FakeSources(torrent()),tmp_path/'library');eng.check_legacy_owner=lambda sid=None:None;eng.catalog=Mock()
    def candidate(title='[Group] Example S01E01 [1080p][CHS]',source=1,url='https://mikanani.me/a.torrent'):
        cid=db.execute('INSERT INTO candidates(show_id,source_id,title,url,discovered) VALUES(42,?,?,?,?)',(source,title,url,time.time()))
        return db.one('SELECT * FROM candidates WHERE id=?',(cid,))
    return db,qb,eng,candidate

def test_real_completion_requires_local_file(setup):
    db,q,e,c=setup;e.ingest(c());assert q.adds==1
    h=next(iter(q.tasks));q.fs[h][0]['progress']=1;e.reconcile()
    assert db.one('SELECT status FROM episodes')['status']=='file_missing'
    p=Path(q.tasks[h]['save_path'])/q.fs[h][0]['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    e.reconcile();assert db.one('SELECT status FROM episodes')['status']=='complete'


def test_thunder_preference_never_silently_falls_back(setup):
    db,q,e,c=setup
    db.set('downloader_preference','thunder_first')
    e.ingest(c())
    assert q.adds==0 and not q.starts
    assert db.one('SELECT status FROM candidates')['status']=='review'
    assert not db.rows('SELECT * FROM tasks')
    # Also block a previously prepared/uncertain qB intent after restart.
    db.set('downloader_preference','qbit');q.ambiguous=True;e.ingest(c(source=2))
    assert q.adds==1 and not q.starts
    db.set('downloader_preference','thunder_first')
    q.ensure_running=Mock()
    e.cycle()
    q.ensure_running.assert_not_called()
    assert q.adds==1 and not q.starts


def test_thunder_blocks_quarter_toggle_and_future_tick(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    import datetime
    db,q,e,c=setup;db.set('downloader_preference','thunder_first')
    db.set('auto_quarter_start','2026-10');db.set('auto_quarter_enabled',False)
    app=create_app(db,e,False)
    with TestClient(app,base_url='http://127.0.0.1:4871') as client:
        token=client.get('/api/bootstrap').json()['token']
        response=client.post('/api/season',json={'enabled':True},headers={'X-Anime-Token':token})
        assert response.status_code==400 and not db.get('auto_quarter_enabled')
        assert client.get('/api/season').json()['downloader']['ready'] is False
        db.set('auto_quarter_enabled',True);e.catalog.sync_quarter=Mock()
        e.auto_tick(datetime.date(2026,10,2))
        e.catalog.sync_quarter.assert_called_once()
        assert q.adds==0

def test_timeout_after_accept_and_process_restart(setup):
    db,q,e,c=setup;q.ambiguous=True;e.ingest(c())
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='uncertain'
    restarted=Engine(Store(db.path),q,e.sources,e.library);restarted.check_legacy_owner=e.check_legacy_owner
    restarted.reconcile();assert q.adds==1 and len(q.starts)==1

def test_dual_source_and_alternate_hash_episode_dedup(setup):
    db,q,e,c=setup;e.ingest(c());e.ingest(c(source=2));assert q.adds==1
    e.sources.data=torrent()+b'bad';e.ingest(c(source=3));assert q.adds==1
    assert len(db.rows('SELECT * FROM episodes'))==1

def test_batch_only_missing(setup):
    db,q,e,c=setup;db.execute("INSERT INTO episodes(show_id,episode,status,path) VALUES(42,1,'complete','old.mkv')")
    e.sources.data=torrent((1,2,3));e.ingest(c('[Group] Example S01E01-E03 [1080p][CHS]'))
    h=next(iter(q.tasks));assert [f['index'] for f in q.fs[h] if f['priority']]==[1,2]
    assert [x['episode'] for x in db.rows('SELECT * FROM episodes WHERE hash=? ORDER BY episode',(h,))]==[2,3]

def test_wrong_season_and_episode_refused(setup):
    db,q,e,c=setup;e.ingest(c('[Group] Example S02E01 [1080p][CHS]'));assert not q.tasks
    e.sources.data=torrent((2,));e.ingest(c(source=2));assert not q.tasks
    assert all(x['status']=='rejected' for x in db.rows('SELECT * FROM candidates'))

def test_drop_during_fetch_blocks_submission(setup):
    db,q,e,c=setup
    def download(_):e.state(42,'dropped');return torrent()
    e.sources.torrent=download;e.ingest(c());assert not q.tasks
    db.upsert_subject({'id':42,'name':'Example changed','total_episodes':12},'2026-10')
    e.ingest(c(source=2));assert not q.tasks and db.show(42)['state']=='dropped'

def test_drop_after_uncertain_never_resumed_by_restart(setup):
    db,q,e,c=setup;q.ambiguous=True;e.ingest(c());e.state(42,'dropped')
    e.reconcile();assert not q.starts and q.stops
    assert db.one('SELECT status FROM tasks')['status']=='held'
    e.state(42,'watching');e.reconcile();assert len(q.starts)==1 and q.adds==1

def test_trial_has_no_episode_cap_and_explicit_resume(setup):
    db,q,e,c=setup;e.state(42,'trial');e.sources.data=torrent((1,2,3,4,5))
    candidate=c('[Group] Example S01E01-E05 [1080p][CHS]')
    assert e.gaps(42)['missing']==[1,2,3,4,5]
    assert db.allowed(42,5)
    e.ingest(candidate)
    h=next(iter(q.tasks));assert [f['index'] for f in q.fs[h] if f['priority']]==[0,1,2,3,4]
    e.state(42,'dropped');e.reconcile()
    e.sources.data=torrent((6,));e.ingest(c('[Group] Example S01E06 [1080p][CHS]',source=2))
    assert len(q.starts)==1 and q.stops and q.adds==1 and not db.allowed(42,6)
    e.state(42,'trial');e.reconcile()
    assert q.adds==1 and len(q.starts)==2 and db.show(42)['state']=='trial'


def test_explicit_range_expands_same_batch(setup):
    db,q,e,c=setup;e.state(42,'trial')
    mapping=db.mapping(42);mapping['end']=3
    db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(mapping),))
    e.sources.data=torrent((1,2,3,4,5));title='[Group] Example S01E01-E05 [1080p][CHS]'
    e.ingest(c(title));h=next(iter(q.tasks))
    assert [f['index'] for f in q.fs[h] if f['priority']]==[0,1,2]
    mapping['end']=5;db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(mapping),))
    e.ingest(c(title,source=2))
    assert q.adds==1 and [f['index'] for f in q.fs[h] if f['priority']]==[0,1,2,3,4]


def test_offline_and_recovery_rss_persistent(tmp_path):
    db=Store(tmp_path/'s.db');db.upsert_subject({'id':42,'name':'Example'})
    sid=db.execute('INSERT INTO sources(show_id,kind,url) VALUES(42,?,?)',('mikan','https://mikanani.me/RSS/Bangumi?bangumiId=1'))
    web=Mock();web.fetch.side_effect=ConnectionError('private token should never be saved');sources=Sources(db,web)
    source=db.one('SELECT * FROM sources');assert not sources.rss(source)
    failed=db.one('SELECT * FROM sources');assert failed['failures']==1 and failed['next_attempt']>time.time()
    assert 'private' not in failed['error']
    response=Mock();response.content=b'<rss version="2.0"><channel><title>test</title><item><title>Example S01E01</title><link>https://mikanani.me/a.torrent</link></item></channel></rss>'
    web.fetch.side_effect=None;web.fetch.return_value=response
    reopened=Store(db.path);sources=Sources(reopened,web);assert sources.rss(reopened.one('SELECT * FROM sources'))
    assert sources.rss(reopened.one('SELECT * FROM sources'))
    assert len(reopened.rows('SELECT * FROM candidates'))==1 and reopened.one('SELECT * FROM sources')['failures']==0

def test_missing_episode_gap_and_future_airing(setup):
    db,q,e,c=setup;c('[Group] Example S01E03 [1080p][CHS]')
    db.execute("INSERT INTO airings VALUES(42,4,'2099-01-01','future')")
    assert e.gaps(42)['missing']==[1,2,3]

def test_foreign_task_never_mutated(setup):
    db,q,e,c=setup;data=torrent();h,_=bdecode(data);q.add(h,data,'elsewhere');q.tasks[h]['category']='other'
    e.ingest(c());assert not q.starts and not q.stops and q.adds==1

def test_parser_specials_and_paths():
    for title in ['Example S01E01 OVA','Example - 12.5','Example [1080p]']:
        with pytest.raises(Rejected):episodes_of(title)
    with pytest.raises(Rejected):bdecode(encode({b'info':{b'name':b'../evil',b'length':4,b'pieces':b'x'*20,b'piece length':4}}))
    with pytest.raises(Rejected):select_files([{'index':0,'name':'Wrong show S01E01.mkv','size':100}],{'aliases':['Example'],'season':1},[1],[1])

def test_restart_forces_archive_backfill_and_drop_excludes_sources(setup):
    db,q,e,c=setup
    db.execute('INSERT INTO sources(show_id,kind,url,next_attempt,archive_success) VALUES(42,?,?,?,?)',('mikan','https://mikanani.me/rss',time.time()+900,time.time()))
    source=Mock();source.rss.return_value=True;e.sources=source;e.catalog=Mock()
    e.cycle(force=True);assert source.rss.call_count==1 and source.archive.call_count==1
    e.state(42,'dropped');e.cycle(force=True);assert source.rss.call_count==1

def test_batch_root_range_does_not_override_episode_filename():
    files=[{'index':0,'name':'Example [01-03]/Example [02].mkv','size':100}]
    selected,ids=select_files(files,{'aliases':['Example'],'season':1},[2],[1,2,3])
    assert ids==[0] and 2 in selected

def test_api_host_csrf_watch_and_catalog_state(setup):
    from anime.app import create_app
    from fastapi.testclient import TestClient
    db,q,e,c=setup;app=create_app(db,e,False)
    with TestClient(app,base_url='http://127.0.0.1:4871') as client:
        assert client.get('/api/status',headers={'host':'evil.example'}).status_code==403
        assert client.post('/api/shows/42/state',json={'state':'dropped'}).status_code==403
        token=client.get('/api/bootstrap').json()['token'];headers={'x-anime-token':token}
        assert client.post('/api/shows/42/state',json={'state':'dropped'},headers=headers).status_code==200
        assert client.post('/api/shows/42/watch',json={'episode':2,'finished':True},headers=headers).status_code==200
        assert db.show(42)['watched']==1 and not db.one('SELECT 1 FROM watches WHERE episode=1')
        assert client.post('/api/settings',json={'resolution':'720'},headers={**headers,'origin':'https://evil.example'}).status_code==403

def test_watching_last_episode_completes_show_once(setup):
    from anime.app import create_app
    from fastapi.testclient import TestClient
    db,q,e,c=setup;app=create_app(db,e,False)
    with TestClient(app,base_url='http://127.0.0.1:4871') as client:
        headers={'x-anime-token':client.get('/api/bootstrap').json()['token']}
        watch=lambda n,f=True:client.post('/api/shows/42/watch',json={'episode':n,'finished':f},headers=headers).json()['state']
        assert all(watch(n) is None for n in range(1,12)) and db.show(42)['state']=='watching'
        assert watch(12)=='completed' and db.show(42)['state']=='completed'
        assert watch(12,False)=='watching' and db.show(42)['state']=='watching'
        assert watch(12)=='completed'
        e.state(42,'watching')  # a manual choice after finishing sticks
        assert watch(3) is None and db.show(42)['state']=='watching'
        e.state(42,'dropped')
        assert watch(12,False) is None and watch(12) is None and db.show(42)['state']=='dropped'

def test_removed_confirmed_task_not_readded_on_drop_restore(setup):
    db,q,e,c=setup;e.ingest(c());h=next(iter(q.tasks));q.tasks.pop(h)
    e.state(42,'dropped');e.state(42,'watching');e.reconcile()
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='review'

def test_source_hash_mismatch_rejected(setup):
    db,q,e,c=setup;e.ingest(c(url='https://mikanani.me/'+('a'*40)+'.torrent'))
    assert q.adds==0 and 'infohash' in db.one('SELECT reason FROM candidates')['reason']

def test_source_owner_conflict_rolls_back_mapping(setup):
    from anime.app import create_app
    from fastapi.testclient import TestClient
    db,q,e,c=setup;db.upsert_subject({'id':43,'name':'Another'})
    db.execute('INSERT INTO sources(show_id,kind,url) VALUES(43,?,?)',('dmhy','https://share.dmhy.org/topics/rss/rss.xml?keyword=Example'))
    old=db.show(42)['mapping'];m=json.loads(old);m['dmhy_keyword']='Example'
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        token=client.get('/api/bootstrap').json()['token']
        r=client.post('/api/shows/42/mapping',json=m,headers={'x-anime-token':token})
        assert r.status_code==400
    assert db.show(42)['mapping']==old


def test_web_range_and_open_never_finish(setup,tmp_path):
    from anime.app import create_app
    from fastapi.testclient import TestClient
    db,q,e,c=setup;p=tmp_path/'clip.mp4';p.write_bytes(b'0123456789')
    (db.path.parent/'inventory.json').write_text(json.dumps([{'path':str(p),'size':10}]),encoding='utf-8')
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        h={'x-anime-token':client.get('/api/bootstrap').json()['token']}
        media=client.get('/api/web/media').json()[0];key=media['id']
        assert 'path' not in media
        r=client.get(f'/api/web/media/{key}/stream',headers={'Range':'bytes=2-5'})
        assert r.status_code==206 and r.content==b'2345'
        assert client.get('/api/web/media/arbitrary/stream').status_code==404
        assert client.post(f'/api/web/media/{key}/progress',json={'position':2,'duration':10,'playing':False},headers=h).status_code==200
        assert db.one('SELECT last_played FROM web_progress')['last_played'] is None
        assert not db.rows('SELECT * FROM watches')
        p.write_bytes(b'changed')
        assert client.get(f'/api/web/media/{key}/stream').status_code==400


def test_danmu_normalization_dedup_and_invalid_times():
    from anime.webplayer import normalize_comments
    d=normalize_comments({'comments':[{'p':'1.2,1,16777215,x','m':'hello'},{'p':'1.2,1,16777215,x','m':'hello'},
        {'p':'NaN,1,1,x','m':'bad'},{'p':'-2,1,1,x','m':'bad'},{'p':'2,5,255,x','m':'top'}]})
    assert len(d)==2 and d[1]['mode']==1 and d[1]['color']=='#0000ff'


def test_danmu_normalization_keeps_only_valid_sender():
    from anime.webplayer import normalize_comments
    d=normalize_comments({'comments':[{'p':'1,1,1,x','m':'a','sender':'9f3a1c2b'},{'p':'2,1,1,x','m':'b','sender':''},
        {'p':'3,1,1,x','m':'c','sender':{'x':1}},{'p':'4,1,1,x','m':'d','sender':'x'*65},{'p':'5,1,1,x','m':'e'}]})
    assert [x.get('user') for x in d]==['9f3a1c2b',None,None,None,None]


def test_season_checks_early_october_without_marking_watching(setup):
    import datetime
    from anime.season import install_season
    from fastapi import FastAPI
    db,q,e,c=setup;db.execute("UPDATE shows SET state='wish',selected=0,authorized=0 WHERE id=42")
    db.set('auto_quarter_enabled',True);db.set('auto_quarter_start','2026-10')
    e.catalog=Mock();listing=Mock(side_effect=lambda **kw:[db.show(42)])
    prepare=Mock(return_value={'mapping':db.mapping(42),'warning':'','preview':['valid'],'inventory_count':0})
    tick=install_season(FastAPI(),db,e,listing,prepare,Mock())
    tick(datetime.date(2026,9,1));assert not prepare.called
    tick(datetime.date(2026,9,27));assert prepare.called
    tick(datetime.date(2026,10,1));assert db.eligible(42) and db.show(42)['state']=='wish' and not db.show(42)['selected']
    e.state(42,'dropped');db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    tick(datetime.date(2026,10,2));assert prepare.call_count==1 and not db.eligible(42)


def test_season_refuses_old_inventory_and_drop_during_prepare(setup):
    import datetime
    from anime.season import install_season
    from fastapi import FastAPI
    db,q,e,c=setup;db.execute("UPDATE shows SET state='wish',authorized=0 WHERE id=42")
    db.set('auto_quarter_enabled',True);db.set('auto_quarter_start','2026-10');e.catalog=Mock()
    prepare=Mock(return_value={'mapping':db.mapping(42),'warning':'','preview':['valid'],'inventory_count':1})
    tick=install_season(FastAPI(),db,e,lambda **kw:[db.show(42)],prepare,Mock())
    tick(datetime.date(2026,10,1));assert not db.eligible(42)
    db.set('auto_retry_42',0)
    def raced(_):e.state(42,'dropped');return {'mapping':db.mapping(42),'warning':'','preview':['valid'],'inventory_count':0}
    prepare.side_effect=raced;tick(datetime.date(2026,10,1));assert not db.eligible(42)


def test_cleanup_preview_confirmation_and_tombstone(setup):
    from anime.cleanup import Cleanup
    from anime.webplayer import WebPlayer
    db,q,e,c=setup;WebPlayer(db);e.ingest(c());h=next(iter(q.tasks))
    p=Path(q.tasks[h]['save_path'])/q.fs[h][0]['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    q.fs[h][0]['progress']=1;e.reconcile();e.state(42,'dropped')
    cleaner=Cleanup(db,e);plan=cleaner.preview(42);assert p.exists() and plan['bytes']==100
    with pytest.raises(ValueError):cleaner.confirm(42,'old-fingerprint')
    assert p.exists()
    def remove(endpoint,method,data):assert data['deleteFiles']=='false';q.tasks.pop(data['hashes'])
    q.call=remove
    cleaner.confirm(42,plan['fingerprint']);assert not p.exists() and db.show(42)['cleanup_hold']==1
    e.state(42,'watching');e.reconcile();e.ingest(c(source=2))
    assert q.adds==1 and not db.eligible(42) and db.one('SELECT status FROM episodes')['status']=='cleaned'


def test_cleanup_180_days_real_playback_protects_and_legacy_excluded(setup):
    from anime.cleanup import Cleanup
    from anime.webplayer import WebPlayer
    db,q,e,c=setup;WebPlayer(db);e.ingest(c());h=next(iter(q.tasks))
    p=Path(q.tasks[h]['save_path'])/q.fs[h][0]['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    q.fs[h][0]['progress']=1;e.reconcile();cleaner=Cleanup(db,e);now=time.time()
    assert cleaner.preview(42,now) is None
    db.execute('UPDATE episodes SET first_completed=?',(now-181*86400,));assert cleaner.preview(42,now)
    key=hashlib.sha256(str(p).lower().encode()).hexdigest()[:32]
    db.execute('INSERT INTO web_progress(media_id,last_played) VALUES(?,?)',(key,now));assert cleaner.preview(42,now) is None
    db.execute('UPDATE episodes SET hash=NULL');e.state(42,'dropped');assert cleaner.preview(42,now) is None


def test_cleanup_foreign_task_and_changed_file_refused(setup):
    from anime.cleanup import Cleanup
    from anime.webplayer import WebPlayer
    db,q,e,c=setup;WebPlayer(db);e.ingest(c());h=next(iter(q.tasks))
    p=Path(q.tasks[h]['save_path'])/q.fs[h][0]['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    q.fs[h][0]['progress']=1;e.reconcile();e.state(42,'dropped');cleaner=Cleanup(db,e);plan=cleaner.preview(42)
    q.tasks[h]['category']='foreign'
    with pytest.raises(ValueError):cleaner.confirm(42,plan['fingerprint'])
    assert p.exists() and not db.show(42)['cleanup_hold']
    p.write_bytes(b'y'*99)
    with pytest.raises(ValueError):cleaner.preview(42)


def test_danmu_selection_pins_public_url_not_transient_id(setup,monkeypatch):
    from anime.app import create_app
    from anime.webplayer import WebPlayer
    from fastapi.testclient import TestClient
    db,q,e,c=setup;calls=[]
    def upstream(self,path,params=None):
        calls.append((path,params))
        if path.startswith('bangumi/'):
            return {'bangumi':{'episodes':[{'episodeId':1,'url':'https://www.bilibili.com/bangumi/play/ep123'},
                {'episodeId':2,'url':'http://127.0.0.1/private'}]}}
        return {'comments':[{'p':'1,1,16777215,x','m':'test'}]}
    monkeypatch.setattr(WebPlayer,'danmu',upstream)
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        h={'x-anime-token':client.get('/api/bootstrap').json()['token']}
        eps=client.get('/api/web/danmu/show/1').json()['bangumi']['episodes']
        assert 'source_key' not in eps[1]
        assert client.post('/api/web/danmu/comments',json={'episodes':[1]},headers=h).status_code==400
        r=client.post('/api/web/danmu/comments',json={'episodes':[eps[0]['source_key']]},headers=h)
        assert r.status_code==200 and len(r.json()['comments'])==1
        assert calls[-1]==('comment',{'url':'https://www.bilibili.com/bangumi/play/ep123','format':'json'})


def test_bahamut_numeric_episode_pins_site_url(setup,monkeypatch):
    from anime.app import create_app
    from anime.webplayer import WebPlayer
    from fastapi.testclient import TestClient
    db,q,e,c=setup;calls=[]
    def upstream(self,path,params=None):
        calls.append((path,params))
        if path.startswith('bangumi/'):
            return {'bangumi':{'episodes':[
                {'url':'47221','episodeTitle':'【bahamut】 第29集'},
                {'url':'47221','episodeTitle':'Unknown provider'},
                {'url':'https://ani.gamer.com.tw/other?sn=47221','episodeTitle':'【bahamut】 第29集'}]}}
        return {'comments':[{'p':'1,1,16777215,x','m':'test'}]}
    monkeypatch.setattr(WebPlayer,'danmu',upstream)
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        h={'x-anime-token':client.get('/api/bootstrap').json()['token']}
        eps=client.get('/api/web/danmu/show/1').json()['bangumi']['episodes']
        assert all('source_key' not in row for row in eps[1:])
        r=client.post('/api/web/danmu/comments',json={'episodes':[eps[0]['source_key']]},headers=h)
        assert r.status_code==200 and len(r.json()['comments'])==1
        assert calls[-1]==('comment',{'url':'https://ani.gamer.com.tw/animeVideo.php?sn=47221','format':'json'})


def test_manual_refresh_during_cycle_survives_restart(setup):
    db,q,e,c=setup
    db.execute("INSERT INTO sources(show_id,kind,url,next_attempt,archive_success) VALUES(42,'mikan','https://mikanani.me/rss',?,?)",(time.time()+9999,time.time()))
    e.sources.rss=Mock(return_value=True);e.sources.archive=Mock();e.catalog=Mock()
    first=e.request_check()
    e.sources.rss.side_effect=lambda source: (e.request_check() and True)
    e.cycle()
    assert db.get('check_completed')==first and db.get('check_requested')==first+1
    assert e.sources.archive.called
    restarted=Engine(db,q,e.sources,e.library);restarted.catalog=Mock();restarted.check_legacy_owner=lambda *args:None
    e.sources.rss.side_effect=None;e.sources.rss.reset_mock()
    restarted.cycle()
    assert e.sources.rss.called and db.get('check_requested')==db.get('check_completed')


def test_continuing_yearlong_source_checks_after_quarter_boundary(setup):
    db,q,e,c=setup
    db.execute("UPDATE shows SET quarter='2026-07',total=0 WHERE id=42")
    m=db.mapping(42);m['end']=999;db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    assert db.allowed(42,13) and db.allowed(42,52)
    db.execute("INSERT INTO sources(show_id,kind,url) VALUES(42,'mikan','https://mikanani.me/rss')")
    e.sources.rss=Mock(return_value=True);e.sources.archive=Mock();e.catalog=Mock();e.cycle(force=True)
    assert e.sources.rss.called
    e.state(42,'dropped');e.sources.rss.reset_mock();e.request_check();e.cycle()
    assert not e.sources.rss.called and not db.allowed(42,53)


def test_preview_enabled_while_thunder_blocked_and_early_membership(setup):
    from anime.app import create_app
    from fastapi.testclient import TestClient
    db,q,e,c=setup
    db.execute("UPDATE shows SET state='wish',selected=0,authorized=0,quarter='2026-07' WHERE id=42")
    db.execute("INSERT INTO quarter_members VALUES(42,'2026-10','early','reviewed')")
    db.set('downloader_preference','thunder_first');db.set('auto_discovery_enabled',True);db.set('auto_quarter_start','2026-10')
    e.catalog=Mock()
    from anime.season import install_season
    from fastapi import FastAPI
    prepare=Mock(return_value={'mapping':db.mapping(42),'warning':'','preview':['episode 1'],'inventory_count':0})
    saved=Mock();tick=install_season(FastAPI(),db,e,lambda **kw:[db.show(42)],prepare,saved)
    import datetime
    tick(datetime.date(2026,9,27))
    assert db.get('auto_preview_42')['titles']==['episode 1'] and not saved.called and q.adds==0 and not db.eligible(42)
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        rows=client.get('/api/shows?quarter=2026-10&kind=all').json()
        # Fixture has no Japanese metadata: test history-independent membership with a Japanese title.
        db.execute("UPDATE shows SET original='アニメ',metadata=? WHERE id=42",(json.dumps({'platform':'TV'}),))
        rows=client.get('/api/shows?quarter=2026-10').json()
        assert rows[0]['id']==42 and rows[0]['quarter_kind']=='early'
