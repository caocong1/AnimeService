import json,time
from pathlib import Path
from unittest.mock import Mock
import pytest
from test_reliability import setup
from test_source_review import ready_review,finish_sample
from anime.download_recovery import recover_downloads,record_ui_failure
from anime.download_policy import fallback_receipt


def pending(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    db.set('qbit_fallback_enabled',True)
    record_ui_failure(db,'Computer Use helper already has an active request')
    q.check_storage=Mock();q.ensure_running=Mock()
    return db,q,e,r,c


def test_handoff_download_completion_and_play_before_scoring(setup):
    db,q,e,r,c=pending(setup);before=db.show(42)
    recover_downloads(e)
    t=db.one('SELECT * FROM tasks');h=t['hash']
    assert fallback_receipt(db,t) and t['provider']=='qbit' and t['purpose']=='source_sample'
    assert q.adds==1 and q.starts==[h] and not db.rows('SELECT * FROM episodes')
    assert Path(t['save_path']).is_relative_to(e.library)
    assert db.one('SELECT state FROM thunder_queue')['state']=='cancelled'
    assert e.thunder.claim() is None
    q.ensure_running.assert_not_called()
    f=q.fs[h][0];p=Path(t['save_path'])/f['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*f['size'])
    db.set('play_completed_premiere_enabled',True)
    e.reconcile();e.source_review.publish_available()
    assert not db.rows('SELECT * FROM episodes') # preallocation is not completion
    f['progress']=1;e.reconcile();e.source_review.publish_available()
    ep=db.one('SELECT * FROM episodes')
    assert ep['status']=='complete' and ep['path']==str(p)
    assert db.one('SELECT save_path,purpose FROM tasks')=={'save_path':t['save_path'],'purpose':'episode'}
    cfg=e.source_review.policy(42)
    assert cfg['mode']=='sampling' and not cfg['primary'] and cfg['playback_family']
    assert not db.rows('SELECT * FROM watches') and db.show(42)==before
    assert db.one('SELECT review FROM source_samples')['review']=='{}'
    recover_downloads(e);e.reconcile();e.source_review.publish_available()
    assert q.adds==1 and db.one('SELECT * FROM episodes')==ep
    candidate=setup[3]('[Group] Example S01E02 [1080p][CHS]',url='https://example.invalid/episode2')
    assert e.source_review.permits(candidate)
    assert not e.source_review.permits(setup[3]('[Other] Example S01E02 [1080p][CHS]',url='https://example.invalid/other2'))


@pytest.mark.parametrize('block',['disabled','unselected','dropped','paused','completed','cleanup','lease','committing','uncertain','foreign_qbit','foreign_thunder','thunder_offline','storage','manifest'])
def test_handoff_preserves_all_other_gates_and_ownership(setup,block):
    db,q,e,r,c=pending(setup);t=db.one('SELECT * FROM tasks');h=t['hash']
    if block=='disabled':db.set('qbit_fallback_enabled',False)
    if block=='unselected':db.execute('UPDATE shows SET selected=0')
    if block in ('dropped','paused','completed'):db.execute('UPDATE shows SET state=?',(block,))
    if block=='cleanup':db.execute('UPDATE shows SET cleanup_hold=1')
    if block=='lease':db.execute("UPDATE thunder_queue SET lease='secret-test',expires=?",(time.time()+60,))
    if block=='committing':db.execute('UPDATE thunder_queue SET committing=1')
    if block=='uncertain':db.execute("UPDATE tasks SET status='uncertain'")
    if block=='foreign_qbit':q.tasks[h]={'hash':h,'category':'Other','tags':'Other'}
    if block=='foreign_thunder':r.by_hash=lambda h:[{'TaskId':999}]
    if block=='thunder_offline':r.by_hash=Mock(side_effect=ConnectionError('offline'))
    if block=='storage':q.check_storage=Mock(side_effect=ValueError('wrong storage'))
    if block=='manifest':(e.cache/(h+'.torrent')).write_bytes(b'wrong')
    recover_downloads(e)
    assert q.adds==0 and not q.starts and not q.stops
    assert db.one('SELECT provider FROM tasks')['provider']=='thunder'
    assert not db.get('qbit_fallback_'+h)
    q.ensure_running.assert_not_called()


def test_qbit_sample_never_overwrites_existing_episode(setup):
    db,q,e,r,c=pending(setup);recover_downloads(e)
    db.execute("INSERT INTO episodes(show_id,episode,hash,path,status,progress,size) VALUES(42,1,'other','original','complete',1,100)")
    ep=db.one('SELECT * FROM episodes');e.reconcile()
    assert db.one('SELECT * FROM episodes')==ep


def test_fallback_accepted_timeout_reconciles_without_duplicate(setup):
    db,q,e,r,c=pending(setup);q.ambiguous=True
    recover_downloads(e)
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='uncertain'
    e.reconcile()
    assert q.adds==1 and len(q.starts)==1 and db.one('SELECT acknowledged FROM tasks')['acknowledged']==1


def test_missing_uncertain_qbit_task_is_never_resubmitted(setup):
    db,q,e,r,c=pending(setup);recover_downloads(e);h=db.one('SELECT hash FROM tasks')['hash']
    db.execute("UPDATE tasks SET status='uncertain',acknowledged=0 WHERE hash=?",(h,));q.tasks.pop(h)
    e.reconcile();e.reconcile()
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='uncertain'


def test_thunder_completed_sample_publishes_without_review_or_state_change(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    before=db.show(42);db.set('play_completed_premiere_enabled',True)
    e.source_review.publish_available()
    assert db.one('SELECT status FROM episodes')['status']=='complete'
    assert e.source_review.policy(42)['mode']=='sampling'
    assert not e.source_review.policy(42)['primary'] and db.show(42)==before
    assert not db.rows('SELECT * FROM watches') and q.adds==0


def test_qbit_text_response_is_not_called(tmp_path):
    from anime.qbit import Qbit
    q=Qbit(tmp_path/'unused');response=Mock(ok=True,status_code=200,text='Ok.')
    response.json.side_effect=ValueError();q.session.request=Mock(return_value=response)
    assert q.call('torrents/start','POST')=='Ok.'


def test_completed_qbit_incomplete_directory_publishes_alias(setup):
    db,q,e,r,c=pending(setup);recover_downloads(e)
    t=db.one('SELECT * FROM tasks');h=t['hash'];f=q.fs[h][0]
    source=e.incomplete.parent/f['name'];source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(b'x'*f['size'])
    q.tasks[h].update(download_path=str(e.incomplete.parent),content_path=str(source),amount_left=0,progress=1)
    f['progress']=1;db.set('play_completed_premiere_enabled',True)
    e.reconcile();e.source_review.publish_available()
    ep=db.one('SELECT * FROM episodes')
    assert ep['status']=='complete' and Path(ep['path']).samefile(source)
    assert source.exists() and db.get('qbit_file_alias_'+h+'_0')
    from anime.cleanup import Cleanup
    from anime.webplayer import WebPlayer
    WebPlayer(db);db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    plan=Cleanup(db,e).preview(42)
    assert {f['path'] for f in plan['files']}=={str(source.resolve()),str(Path(ep['path']).resolve())}
    assert plan['bytes']==f['size'] and source.exists()


def test_padding_omission_remaps_indices_without_ignoring_real_files(setup):
    from test_reliability import encode
    from anime.releases import bdecode,Rejected
    db,q,e,c=setup
    raw=encode({b'info':{b'name':b'Example',b'piece length':16384,b'pieces':b'x'*40,b'files':[
        {b'path':[b'.pad',b'100'],b'length':100,b'attr':b'p'},
        {b'path':[b'Example S01E01.mkv'],b'length':100}]}})
    h,files=bdecode(raw);(e.cache/(h+'.torrent')).write_bytes(raw)
    t={'hash':h,'selection':json.dumps({'files':files,'ids':[1],'selected':{'1':files[1]}})}
    actual=[dict(files[1],index=0,progress=1,priority=1)]
    e.verify_qbit_files(t,actual)
    with pytest.raises(Rejected):e.verify_qbit_files(t,[])
    with pytest.raises(Rejected):e.verify_qbit_files(t,[dict(actual[0],size=99)])


def test_analysis_errors_retry_with_backoff_and_eligibility(setup,monkeypatch):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    db.set('play_completed_premiere_enabled',True)
    db.execute("UPDATE source_samples SET state='analysis_error'")
    db.set('source_analysis_retry_'+str(c['id']),{'next_attempt':time.time()+60})
    thread=Mock();monkeypatch.setattr('anime.source_review.threading.Thread',thread)
    e.source_review.tick();thread.assert_not_called()
    db.set('source_analysis_retry_'+str(c['id']),{'next_attempt':0})
    e.source_review.tick();assert thread.call_count==1
    assert db.one('SELECT state FROM source_samples')['state']=='analyzing'


def test_successful_thunder_report_restores_thunder_first(setup):
    from test_thunder_bridge import accept
    db,q,e,r,c=pending(setup)
    t=db.one('SELECT * FROM tasks');job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease']);accept(r,t,8)
    f=json.loads(t['selection'])['selected']['1'];p=Path(t['staging_path'])/f['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*f['size'])
    e.thunder.report(t['hash'],job['lease'])
    assert not db.get('thunder_ui_fault')['active']
    recover_downloads(e);assert q.adds==0


def test_restart_retries_stale_preparation_without_duplicate_submission(setup):
    db,q,e,r,c=ready_review(setup)
    from anime.source_review import family
    db.execute("INSERT INTO source_samples(candidate_id,show_id,family,state,updated) VALUES(?,42,?,'preparing',?)",(c['id'],family(c['title'])['key'],time.time()-181))
    e.source_review.start(42)
    assert db.one('SELECT state FROM source_samples')['state']=='queued'
    assert len(db.rows('SELECT * FROM tasks'))==1 and q.adds==0
    e.source_review.start(42);assert len(db.rows('SELECT * FROM tasks'))==1


def test_verified_ui_fault_handoff_and_completed_playback_when_writer_disabled(setup):
    db,q,e,r,c=pending(setup);db.set('thunder_ui_enabled',False)
    recover_downloads(e);t=db.one('SELECT * FROM tasks');h=t['hash']
    assert q.adds==1 and t['provider']=='qbit'
    f=q.fs[h][0];p=Path(t['save_path'])/f['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*f['size']);f['progress']=1
    db.set('play_completed_premiere_enabled',True);e.reconcile();e.source_review.publish_available()
    assert db.one('SELECT status FROM episodes')['status']=='complete'
