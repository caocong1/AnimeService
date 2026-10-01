import json,time
from pathlib import Path
import pytest
from test_reliability import setup,torrent
from anime.thunder_bridge import ThunderBridge
from anime.thunder import ThunderReviewRequired

class FakeThunder:
    def __init__(self):self.data={}
    def by_hash(self,h):return [self.data[h]] if h in self.data else []
    def verify(self,id,h,path,files,ids):
        t=self.data[h]
        if t['TaskId']!=id or t['SavePath']!=path:raise ThunderReviewRequired('迅雷未采用项目指定目录')
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


def test_foreign_task_quarantined_once_without_network_error_or_adoption(setup):
    db,q,e,r,t=ready(setup);accept(r,t)
    before=db.show(42)
    e.reconcile();e.reconcile()
    current=db.one('SELECT * FROM tasks')
    assert current['status']=='review' and '提交凭据' in current['error']
    assert '请求失败' not in current['error']
    assert not current['acknowledged'] and current['external_id'] is None
    queue=db.one('SELECT * FROM thunder_queue')
    assert queue['state']=='review' and not queue['lease'] and not queue['committing']
    events=db.rows("SELECT * FROM events WHERE scope=?",('task:'+t['hash'],))
    assert len(events)==1 and events[0]['level']=='warning'
    assert db.one('SELECT status FROM episodes')['status']=='pending'
    assert not db.rows('SELECT * FROM watches') and db.show(42)==before
    e.thunder=ThunderBridge(e,r)
    assert e.thunder.claim() is None and q.adds==0 and not q.starts


def test_claim_skips_foreign_conflict_and_leases_next_valid_task(setup):
    db,q,e,r,t=ready(setup);accept(r,t)
    e.sources.data=torrent((2,));e.ingest(setup[3]('[Group] Example S01E02 [1080p][CHS]',source=2))
    second=db.one('SELECT * FROM tasks WHERE hash<>?',(t['hash'],))
    job=e.thunder.claim()
    assert job['hash']==second['hash'] and job['action']=='submit'
    assert db.one('SELECT state FROM thunder_queue WHERE hash=?',(t['hash'],))['state']=='review'
    assert db.one('SELECT external_id FROM tasks WHERE hash=?',(t['hash'],))['external_id'] is None
    assert e.thunder.claim() is None and q.adds==0 and not q.starts


def test_owned_task_directory_conflict_preserves_uncertain_commit(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease'])
    accept(r,t);r.data[t['hash']]['SavePath']='some-other-directory'
    e.reconcile()
    current=db.one('SELECT * FROM tasks');queue=db.one('SELECT * FROM thunder_queue')
    assert current['status']=='review' and '指定目录' in current['error']
    assert current['external_id'] is None and not current['acknowledged']
    assert queue['committing']==1 and queue['state']=='review' and not queue['lease']
    assert e.thunder.claim() is None and q.adds==0


def test_temporary_thunder_read_failure_remains_retryable(setup,monkeypatch):
    db,q,e,r,t=ready(setup)
    def unavailable(h):raise OSError('untrusted exception text')
    monkeypatch.setattr(r,'by_hash',unavailable)
    e.reconcile()
    current=db.one('SELECT * FROM tasks')
    assert current['status']=='intent' and 'OSError' in current['error']
    assert 'untrusted' not in current['error']
    assert db.one('SELECT state FROM thunder_queue')['state']=='pending'


def test_explicit_readonly_link_resolves_review_without_owning_or_cleaning_file(setup,tmp_path):
    from anime.cleanup import Cleanup
    from anime.webplayer import WebPlayer
    db,q,e,r,t=ready(setup);accept(r,t,8)
    outside=tmp_path/'outside-project';outside.mkdir()
    r.data[t['hash']]['SavePath']=str(outside)
    f=json.loads(t['selection'])['files'][0];p=outside/f['name'];p.parent.mkdir(parents=True);p.write_bytes(b'x'*f['size'])
    e.reconcile();before=db.show(42)
    result=e.thunder.link_reviewed_completed_file(t['hash'],81)
    assert result['mode']=='read_only' and Path(result['path'])==p.resolve()
    episode=db.one('SELECT * FROM episodes');task=db.one('SELECT * FROM tasks')
    assert episode['status']=='complete' and episode['hash'] is None and episode['first_completed'] is None
    assert task['status']=='linked' and not task['external_id'] and not task['acknowledged']
    assert db.one('SELECT state FROM thunder_queue')['state']=='done'
    assert db.show(42)==before and not db.rows('SELECT * FROM watches')
    WebPlayer(db);e.reconcile();e.dispatch(task)
    assert e.thunder.claim() is None and q.adds==0 and not q.starts
    e.state(42,'dropped');assert db.one('SELECT status FROM tasks')['status']=='linked'
    assert Cleanup(db,e).preview(42) is None and p.read_bytes()==b'x'*f['size']


def test_readonly_link_requires_explicit_complete_unambiguous_review(setup,tmp_path):
    db,q,e,r,t=ready(setup);accept(r,t)
    e.reconcile()
    with pytest.raises(ValueError,match='完成'):e.thunder.link_reviewed_completed_file(t['hash'],81)
    db.execute('UPDATE thunder_queue SET committing=1')
    with pytest.raises(ValueError,match='不确定提交'):e.thunder.link_reviewed_completed_file(t['hash'],81)
    db.execute('UPDATE thunder_queue SET committing=0');db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    with pytest.raises(ValueError,match='已清理'):e.thunder.link_reviewed_completed_file(t['hash'],81)
    assert db.one('SELECT hash FROM episodes')['hash']==t['hash'] and not db.rows('SELECT * FROM watches')

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
