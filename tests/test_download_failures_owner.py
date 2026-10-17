"""Refusal cache invalidation and polling bounds; synthetic downloader only."""
import json
import time
from pathlib import Path
from unittest.mock import Mock
import pytest
from test_reliability import setup
from test_download_failures import intent_task,task_events
from anime.download_policy import Refusal,THUNDER_PENDING


def test_pending_manual_check_is_consumed_once_even_if_cycle_has_not_finished(setup):
    db,q,e,c=setup;h=intent_task(db,q,e,c);db.set('downloader_preference','thunder_first')
    e.reconcile();db.set('check_requested',123)
    for _ in range(6):e.reconcile()
    assert db.get('task_failure_'+h)['attempts']==2 and q.adds==1
    db.set('check_requested',124);e.reconcile()
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
    db.set('check_requested',20)
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
