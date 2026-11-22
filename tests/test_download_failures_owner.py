"""Refusal cache invalidation and polling bounds; synthetic downloader only."""
import json
import time
from pathlib import Path
from unittest.mock import Mock
import pytest
from test_reliability import setup,encode,FakeSources
from test_download_failures import intent_task,task_events
from anime.download_policy import Refusal,THUNDER_PENDING


def test_pending_manual_check_is_consumed_once_even_if_cycle_has_not_finished(setup):
    db,q,e,c=setup;h=intent_task(db,q,e,c);db.set('downloader_preference','thunder_first')
    e.reconcile();e.request_check()
    for _ in range(6):e.reconcile()
    assert db.get('task_failure_'+h)['attempts']==2 and q.adds==1
    e.request_check();e.reconcile()
    assert db.get('task_failure_'+h)['attempts']==3


def test_refusal_reaches_the_documented_six_hour_cap(setup,monkeypatch):
    db,q,e,c=setup;h=intent_task(db,q,e,c);db.set('downloader_preference','thunder_first')
    now=[time.time()];monkeypatch.setattr('anime.engine.time.time',lambda:now[0])
    waits=[]
    for _ in range(8):
        e.reconcile();record=db.get('task_failure_'+h);waits.append(record['next']-now[0]);now[0]=record['next']+1
    assert waits==[600,1200,2400,4800,9600,19200,21600,21600]


@pytest.mark.parametrize('change',['metadata','purpose','reference','fidelity','total'])
def test_new_gate_evidence_invalidates_a_cached_refusal(setup,change):
    db,q,e,c=setup;h=intent_task(db,q,e,c);t=db.one('SELECT * FROM tasks WHERE hash=?',(h,))
    e.note_failure(h,t,Refusal('blocked'),'blocked');assert e.refused(t)
    if change=='metadata':db.execute('UPDATE candidates SET source_metadata=? WHERE hash=?',(json.dumps({'media_mode':'raw'}),h))
    elif change=='purpose':db.execute("UPDATE tasks SET purpose='subtitle_sample' WHERE hash=?",(h,))
    elif change=='reference':db.execute("INSERT INTO subtitle_samples(candidate_id,show_id,episode,family,hash,state,evidence) VALUES(1,42,1,'test',?,'queued',?)",(h,json.dumps({'workflow_role':'subtitle_reference'})))
    elif change=='fidelity':db.set('source_fidelity','original')
    elif change=='total':db.execute('UPDATE shows SET total=24 WHERE id=42')
    assert not e.refused(db.one('SELECT * FROM tasks WHERE hash=?',(h,)))


def test_unknown_or_acknowledged_submission_is_not_skipped_by_refusal_cache(setup):
    db,q,e,c=setup;h=intent_task(db,q,e,c);t=db.one('SELECT * FROM tasks WHERE hash=?',(h,))
    for patch in ({'status':'uncertain'},{'acknowledged':1},{'external_id':123}):
        candidate={**t,**patch};e.note_failure(h,candidate,Refusal('blocked'),'blocked')
        assert not e.refused(candidate)
    assert q.adds==1


def test_complete_cache_rechecks_policy_and_download_progress_immediately(setup):
    db,q,e,c=setup;e.ingest(c());h=next(iter(q.tasks));q.fs[h][0]['progress']=1
    p=Path(q.tasks[h]['save_path'])/q.fs[h][0]['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    e.reconcile();q.files=Mock(side_effect=q.files)
    e.reconcile();assert q.files.call_count==0
    db.set('source_fidelity','original');e.reconcile();assert q.files.call_count==1
    q.tasks[h]['progress']=0.5;e.reconcile();assert q.files.call_count==2
    e.request_check()
    for _ in range(4):e.reconcile()
    assert q.files.call_count==3


def test_deferred_candidates_do_not_hide_ready_candidates_after_the_batch_limit(setup):
    db,q,e,c=setup
    for n in range(301):
        item=c(url=f'https://mikanani.me/{n}.torrent')
        if n<300:
            db.execute("UPDATE candidates SET status='retry' WHERE id=?",(item['id'],))
            db.set('candidate_retry_'+str(item['id']),{'next':time.time()+3600})
    e.ingest=Mock();e.ingest_show(42)
    assert e.ingest.call_count==1 and e.ingest.call_args.args[0]['id']==301


def test_candidate_backoff_reaches_six_hours(setup,monkeypatch):
    db,q,e,c=setup;candidate=c();e.sources.torrent=Mock(side_effect=ConnectionError('synthetic'))
    for _ in range(9):e.ingest(candidate)
    record=db.get('candidate_retry_'+str(candidate['id']))
    assert 21599<record['next']-time.time()<=21600


def add_second_show(db,q,e):
    """Show 43, configured like the fixture's show 42, with one synthetic task of its own."""
    db.upsert_subject({'id':43,'name':'Other','total_episodes':12})
    mapping={'aliases':['Other'],'season':1,'offset':0,'start':1,'end':12,'confirmed':True,'inventory_checked':True}
    db.execute("UPDATE shows SET mapping=?,selected=1,state='watching',authorized=1 WHERE id=43",(json.dumps(mapping),))
    cid=db.execute('INSERT INTO candidates(show_id,source_id,title,url,discovered) VALUES(43,1,?,?,?)',
                   ('[Group] Other S01E01 [1080p][CHS]','https://mikanani.me/o.torrent',time.time()))
    torrent=encode({b'announce':b'https://example.invalid/announce',b'info':{b'name':b'Other',b'piece length':16384,
        b'pieces':b'y'*20,b'files':[{b'length':100,b'path':[b'Other S01E01.mkv']}]}})
    before=set(q.tasks);original=e.sources;e.sources=FakeSources(torrent)
    try:e.ingest(db.one('SELECT * FROM candidates WHERE id=?',(cid,)))
    finally:e.sources=original
    (h,)=set(q.tasks)-before
    return h


def test_tracking_click_retries_only_that_shows_cached_refusal(setup):
    db,q,e,c=setup;h42=intent_task(db,q,e,c);h43=add_second_show(db,q,e)
    db.execute("UPDATE tasks SET status='intent',acknowledged=0 WHERE hash=?",(h43,));q.tasks.pop(h43);q.fs.pop(h43)
    adds=q.adds;db.set('downloader_preference','thunder_first')
    attempts=lambda:(db.get('task_failure_'+h42)['attempts'],db.get('task_failure_'+h43)['attempts'])
    for _ in range(3):e.reconcile()
    assert attempts()==(1,1)
    key=e.gate_key(db.one('SELECT * FROM tasks WHERE hash=?',(h42,)))
    # A round trip leaves the gate inputs unchanged, so only the per-show check can lift the refusal.
    e.state(42,'dropped');e.state(42,'watching')
    assert e.gate_key(db.one('SELECT * FROM tasks WHERE hash=?',(h42,)))==key
    for _ in range(3):e.reconcile()
    assert attempts()==(2,1)
    e.request_check()  # the global check still retries every show once
    for _ in range(3):e.reconcile()
    assert attempts()==(3,2) and q.adds==adds


def test_tracking_click_reverifies_only_that_shows_completed_package(setup):
    db,q,e,c=setup;e.ingest(c());h42=next(iter(q.tasks));h43=add_second_show(db,q,e)
    for h in (h42,h43):
        q.fs[h][0]['progress']=1
        p=Path(q.tasks[h]['save_path'])/q.fs[h][0]['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    e.reconcile()
    assert [db.one('SELECT status FROM tasks WHERE hash=?',(h,))['status'] for h in (h42,h43)]==['complete','complete']
    calls=[];original=q.files
    q.files=lambda h:(calls.append(h),original(h))[1]
    for _ in range(3):e.reconcile()
    assert calls==[]
    e.state(42,'dropped');e.state(42,'watching')
    for _ in range(3):e.reconcile()
    assert calls==[h42]
    e.request_check()
    for _ in range(3):e.reconcile()
    assert sorted(calls)==sorted([h42,h42,h43])
