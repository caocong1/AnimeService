import json,time
from pathlib import Path
import pytest
from test_reliability import setup
from anime.thunder_bridge import ThunderBridge

class FakeThunder:
    def __init__(self):self.data={}
    def by_hash(self,h):return [self.data[h]] if h in self.data else []
    def verify(self,id,h,path,files,ids):
        t=self.data[h]
        if t['TaskId']!=id or t['SavePath']!=path:raise ValueError('path/id mismatch')
        return t

def ready(setup):
    db,q,e,c=setup;db.set('downloader_preference','thunder_first');db.set('thunder_ui_enabled',True)
    e.incomplete=db.path.parent/'incomplete';r=FakeThunder();e.thunder=ThunderBridge(e,r)
    e.ingest(c());t=db.one('SELECT * FROM tasks');assert t and t['provider']=='thunder' and q.adds==0
    return db,q,e,r,t

def accept(r,t,status=5):
    sel=json.loads(t['selection'])
    r.data[t['hash']]={'TaskId':81,'hash':t['hash'],'SavePath':t['staging_path'],'CreationTime':int(time.time()*1000),
        'Status':status,'FailureErrorCode':0,'verified_complete':status==8,
        'files':[{**f,'selected':f['index'] in sel['ids'],'received':f['size'] if status==8 else 0} for f in sel['files']]}

def test_queue_persistence_and_drop_before_click(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();assert job['action']=='submit'
    assert e.thunder.claim() is None
    e.state(42,'dropped')
    with pytest.raises(ValueError):e.thunder.preflight(t['hash'],job['lease'])
    e.thunder=ThunderBridge(e,r);e.reconcile()
    assert e.thunder.claim() is None and q.adds==0

def test_crash_after_click_recovers_by_hash_and_pause_after_drop(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease'])
    e.state(42,'dropped');accept(r,t)
    e.thunder=ThunderBridge(e,r);e.reconcile()
    current=db.one('SELECT * FROM tasks');assert current['external_id']==81
    job=e.thunder.claim();assert job['action']=='pause'
    assert job['external_id']==81
    assert q.adds==0

def test_completed_file_published_as_same_hardlink_once(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease']);accept(r,t,8)
    sel=json.loads(t['selection']);f=sel['files'][0];source=Path(t['staging_path'])/f['name'];source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(b'x'*f['size'])
    e.reconcile();e.reconcile();ep=db.one('SELECT * FROM episodes')
    assert ep['status']=='complete' and Path(ep['path']).samefile(source)
    assert e.thunder.claim() is None and not db.rows('SELECT * FROM watches')

def test_foreign_hash_never_adopted(setup):
    db,q,e,r,t=ready(setup);accept(r,t)
    with pytest.raises(ValueError):e.thunder.inspect(t)
    assert not db.one('SELECT acknowledged FROM tasks')['acknowledged']

def test_cleanup_removes_both_owned_links_after_confirmation(setup):
    from anime.cleanup import Cleanup
    from anime.webplayer import WebPlayer
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease']);accept(r,t,8)
    WebPlayer(db)
    f=json.loads(t['selection'])['files'][0];source=Path(t['staging_path'])/f['name'];source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(b'x'*f['size'])
    e.reconcile();e.state(42,'dropped');cleaner=Cleanup(db,e);plan=cleaner.preview(42)
    assert len(plan['files'])==2 and plan['bytes']==f['size'] and source.exists()
    cleaner.confirm(42,plan['fingerprint'])
    assert not source.exists() and db.show(42)['cleanup_hold'] and db.one('SELECT * FROM tasks')['status']=='cleaned'

def test_legacy_rss_processing_disabled_keeps_rules_but_allows_handoff(setup,tmp_path,monkeypatch):
    from anime.engine import Engine
    db,q,e,c=setup;root=tmp_path/'appdata';rss=root/'qBittorrent/rss';rss.mkdir(parents=True)
    (rss/'download_rules.json').write_text(json.dumps({'anime':{'enabled':True}}))
    (rss.parent/'qBittorrent.ini').write_text('[RSS]\nAutoDownloader\\EnableProcessing=false\n')
    monkeypatch.setenv('APPDATA',str(root))
    Engine.check_legacy_owner(e)

def test_stage_and_airing_numbers_are_not_confused(setup):
    from anime.releases import validate_title,Rejected,select_files
    db,q,e,c=setup;m=db.mapping(42);m.update(offset=12,airing_offset=12,required_title='第二赛段')
    db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    db.execute("INSERT INTO airings VALUES(42,13,'2026-01-01','')")
    assert e.gaps(42)['missing']==[1]
    with pytest.raises(Rejected):validate_title('第一赛段 - 13 [1080p CHS]',m,e.settings())
    files=[{'index':0,'name':'Example S01E01.mkv','size':100}]
    with pytest.raises(Rejected):select_files(files,{'aliases':['Example'],'season':1,'offset':12},[1],[1])
