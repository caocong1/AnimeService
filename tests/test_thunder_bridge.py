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


def test_ui_timeout_isolates_uncertain_submit_and_leases_other_work(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease'])
    e.thunder.report(t['hash'],job['lease'],'UI submit timeout; no acknowledgement')
    uncertain=db.one('SELECT * FROM thunder_queue WHERE hash=?',(t['hash'],))
    assert uncertain['state']=='review' and uncertain['committing']==1 and not uncertain['lease']
    assert db.one('SELECT status FROM tasks WHERE hash=?',(t['hash'],))['status']=='review'
    e.sources.data=torrent((2,));e.ingest(setup[3]('[Group] Example S01E02 [1080p][CHS]',source=2))
    next_job=e.thunder.claim();assert next_job['hash']!=t['hash'] and not next_job['uncertain'] and q.adds==0


def test_ui_timeout_with_real_ack_is_reconciled_before_review(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease']);accept(r,t)
    e.thunder.report(t['hash'],job['lease'],'UI response timed out')
    assert db.one('SELECT status,acknowledged FROM tasks')=={'status':'downloading','acknowledged':1}
    assert db.one('SELECT state FROM thunder_queue')['state']=='done' and e.thunder.claim() is None


def uncertain_review(setup):
    db,q,e,r,t=ready(setup);job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease'])
    e.thunder.report(t['hash'],job['lease'],'UI submit timeout; no acknowledgement')
    return db,q,e,r,t


def test_uncertain_review_without_match_stays_isolated_after_restart(setup):
    db,q,e,r,t=uncertain_review(setup)
    before=db.one('SELECT * FROM thunder_queue');task=db.one('SELECT * FROM tasks');show=db.show(42)
    e.thunder=ThunderBridge(e,r);e.reconcile();e.reconcile()
    assert db.get('thunder_review_check_'+t['hash'])['result']=='no_match'
    assert db.one('SELECT * FROM thunder_queue')==before and db.one('SELECT * FROM tasks')==task
    assert e.thunder.claim() is None and q.adds==0 and not q.starts
    assert db.show(42)==show and not db.rows('SELECT * FROM watches')


def test_uncertain_review_late_receipt_is_verified_automatically(setup):
    db,q,e,r,t=uncertain_review(setup);accept(r,t)
    e.thunder=ThunderBridge(e,r);e.reconcile()
    assert db.one('SELECT status,acknowledged,external_id,error FROM tasks')=={
        'status':'downloading','acknowledged':1,'external_id':81,'error':''}
    assert db.one('SELECT state,committing,lease FROM thunder_queue')=={'state':'done','committing':1,'lease':''}
    assert db.get('thunder_review_check_'+t['hash'])['result']=='verified'
    assert e.thunder.claim() is None and q.adds==0 and not q.starts


def test_uncertain_late_receipt_after_drop_only_queues_pause(setup):
    db,q,e,r,t=uncertain_review(setup);db.execute("UPDATE shows SET state='dropped',authorized=0 WHERE id=42")
    before=db.show(42);accept(r,t);e.reconcile()
    assert db.one('SELECT acknowledged,external_id,error FROM tasks')=={'acknowledged':1,'external_id':81,'error':''}
    assert db.one('SELECT action,state FROM thunder_queue')=={'action':'pause','state':'pending'}
    assert e.thunder.claim()['action']=='pause' and db.show(42)==before
    assert q.adds==0 and not q.starts and not db.rows('SELECT * FROM watches')


@pytest.mark.parametrize('failure',['path','old_task','read_error','duplicates'])
def test_uncertain_review_never_adopts_unverified_or_unreadable_receipt(setup,monkeypatch,failure):
    db,q,e,r,t=uncertain_review(setup);accept(r,t)
    if failure=='path':r.data[t['hash']]['SavePath']='outside-project'
    if failure=='old_task':r.data[t['hash']]['CreationTime']=int((t['created']-60)*1000)
    if failure=='duplicates':monkeypatch.setattr(r,'by_hash',lambda h:[r.data[h],r.data[h]])
    if failure=='read_error':
        def unreadable(h):raise OSError('private exception text')
        monkeypatch.setattr(r,'by_hash',unreadable)
    before=db.one('SELECT * FROM tasks');queue=db.one('SELECT * FROM thunder_queue')
    e.reconcile()
    assert db.one('SELECT * FROM tasks')==before and db.one('SELECT * FROM thunder_queue')==queue
    check=db.get('thunder_review_check_'+t['hash'])
    assert check['result']==('unavailable' if failure=='read_error' else 'conflict')
    assert 'private' not in check['error'] and q.adds==0 and not q.starts


def test_claim_show_filter_leaves_other_work_untouched(setup):
    db,q,e,r,t=ready(setup)
    before=db.one('SELECT * FROM thunder_queue')
    assert e.thunder.claim(99) is None
    assert db.one('SELECT * FROM thunder_queue')==before
    assert e.thunder.claim(42)['hash']==t['hash'] and q.adds==0


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


def readonly_sample(setup,tmp_path):
    from test_source_review import ready_review
    db,q,e,r,c=ready_review(setup)
    e.source_review.start(42)
    t=db.one('SELECT * FROM tasks');accept(r,t,8)
    outside=tmp_path/'outside-sample';outside.mkdir()
    r.data[t['hash']]['SavePath']=str(outside)
    f=json.loads(t['selection'])['files'][0];p=outside/f['name'];p.parent.mkdir(parents=True);p.write_bytes(b'x'*f['size'])
    e.reconcile()
    return db,q,e,r,t,p


def test_readonly_sample_resolves_without_episode_or_downloader_adoption(setup,tmp_path,monkeypatch):
    db,q,e,r,t,p=readonly_sample(setup,tmp_path)
    before=db.show(42);outside=dict(r.data[t['hash']])
    e.thunder.link_reviewed_completed_file(t['hash'],81)
    s=db.one('SELECT * FROM source_samples')
    assert s['path']==str(p.resolve()) and s['state']=='analyzing'
    assert json.loads(s['evidence'])['read_only']['mode']=='read_only'
    assert db.one('SELECT status,external_id,acknowledged FROM tasks')=={'status':'linked','external_id':None,'acknowledged':0}
    assert db.one('SELECT state,error,committing FROM thunder_queue')=={'state':'done','error':'','committing':0}
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches') and db.show(42)==before
    calls=[]
    monkeypatch.setattr(e.source_review,'analyze',lambda cid:calls.append(cid))
    e.source_review.tick()
    for worker in __import__('threading').enumerate():
        if worker.name=='source-analysis':worker.join(timeout=2)
    assert calls==[s['candidate_id']]
    e.reconcile();e.dispatch(db.one('SELECT * FROM tasks'))
    assert e.thunder.claim() is None and q.adds==0 and not q.starts and r.data[t['hash']]==outside
    # If this reviewed source wins later, publish only the same read-only path.
    e.source_review.promote(db.one('SELECT * FROM source_samples'))
    ep=db.one('SELECT * FROM episodes')
    assert ep['path']==str(p.resolve()) and ep['hash'] is None and ep['first_completed'] is None
    from anime.cleanup import Cleanup
    e.state(42,'dropped')
    assert Cleanup(db,e).preview(42) is None and p.exists()


@pytest.mark.parametrize('blocker',['unwanted','uncertain','changed_size','existing_path','not_first','wrong_title'])
def test_readonly_sample_preserves_blocked_and_uncertain_records(setup,tmp_path,blocker):
    db,q,e,r,t,p=readonly_sample(setup,tmp_path)
    if blocker=='unwanted':db.execute('UPDATE shows SET selected=0 WHERE id=42')
    if blocker=='uncertain':db.execute('UPDATE thunder_queue SET committing=1')
    if blocker=='changed_size':db.execute('UPDATE source_samples SET size=size+1')
    if blocker=='existing_path':db.execute("UPDATE source_samples SET path='existing'")
    if blocker=='not_first':
        selection=json.loads(t['selection']);selection['selected']={'2':selection['selected']['1']}
        db.execute('UPDATE tasks SET selection=?',(json.dumps(selection),))
    if blocker=='wrong_title':db.execute("UPDATE tasks SET title='Different S01E01' ")
    before={table:db.rows('SELECT * FROM '+table) for table in ('tasks','thunder_queue','source_samples','episodes','watches','shows')}
    with pytest.raises(ValueError):e.thunder.link_reviewed_completed_file(t['hash'],81)
    assert before=={table:db.rows('SELECT * FROM '+table) for table in before}
    assert p.exists() and q.adds==0

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
