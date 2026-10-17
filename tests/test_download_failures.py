"""Download failures carry their reason, stop repeating while inputs are unchanged, and do not flood events."""
import time
from unittest.mock import Mock
import pytest
from test_reliability import setup,torrent
from anime.db import Store
from anime.net import safe_error
from anime.download_policy import Refusal,THUNDER_PENDING


def intent_task(db,q,e,c):
    """A task the engine created and started, put back to intent as if the submission had never been confirmed."""
    e.ingest(c());h=next(iter(q.tasks))
    db.execute("UPDATE tasks SET status='intent',acknowledged=0 WHERE hash=?",(h,));q.tasks.clear();q.fs.clear()
    return h


def task_events(db,h):return db.rows("SELECT * FROM events WHERE scope=? AND level='error'",('task:'+h,))


def test_policy_refusal_is_explained_once_and_not_retried_until_inputs_change(setup):
    db,q,e,c=setup;h=intent_task(db,q,e,c)
    db.set('downloader_preference','thunder_first')
    for _ in range(6):e.reconcile()
    task=db.one('SELECT * FROM tasks WHERE hash=?',(h,))
    assert task['status']=='intent' and task['error']==THUNDER_PENDING and q.adds==1
    record=db.get('task_failure_'+h)
    assert record['attempts']==1 and record['next']>time.time() and record['gate_key'] and len(task_events(db,h))==1
    db.set('check_requested',db.get('check_completed',0)+1)  # an explicit check retries once, with the same answer
    e.reconcile();db.set('check_completed',db.get('check_requested'))
    assert db.get('task_failure_'+h)['attempts']==2 and len(task_events(db,h))==1
    db.set('downloader_preference','qbit')  # changed inputs: the refusal no longer stands
    e.reconcile()
    task=db.one('SELECT * FROM tasks WHERE hash=?',(h,))
    assert q.adds==2 and task['status']=='downloading' and task['error']=='' and db.get('task_failure_'+h) is None


def test_refusal_rechecks_with_growing_spacing(setup,monkeypatch):
    db,q,e,c=setup;h=intent_task(db,q,e,c);db.set('downloader_preference','thunder_first')
    now=[time.time()];monkeypatch.setattr('anime.engine.time.time',lambda:now[0])
    waits=[]
    for _ in range(4):
        e.reconcile();record=db.get('task_failure_'+h);waits.append(round(record['next']-now[0]));now[0]=record['next']+1
    assert waits==[600,1200,2400,4800] and db.get('task_failure_'+h)['attempts']==4


def test_transient_errors_keep_type_and_location_but_report_sparsely(setup):
    db,q,e,c=setup;e.ingest(c());h=next(iter(q.tasks))
    def files(hash_):raise ConnectionError('socket detail that must stay private')
    q.files=files
    for _ in range(30):e.reconcile()
    task=db.one('SELECT * FROM tasks WHERE hash=?',(h,))
    assert task['error'].startswith('ConnectionError：请求失败') and 'engine.reconcile:' in task['error'] and 'private' not in task['error']
    record=db.get('task_failure_'+h)
    assert record['attempts']==30 and record['next']==0 and [ev['message'] for ev in task_events(db,h)].count(task['error'])==3


def test_completed_tasks_are_reverified_hourly_not_every_pass(setup,monkeypatch):
    db,q,e,c=setup;e.ingest(c());h=next(iter(q.tasks))
    q.fs[h][0]['progress']=1;p=__import__('pathlib').Path(q.tasks[h]['save_path'])/q.fs[h][0]['name']
    p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100);e.reconcile()
    assert db.one('SELECT status FROM tasks WHERE hash=?',(h,))['status']=='complete' and db.get('task_verified_'+h)
    updated=db.one('SELECT updated FROM tasks WHERE hash=?',(h,))['updated']
    calls=Mock(side_effect=q.files);q.files=calls
    for _ in range(10):e.reconcile()
    assert calls.call_count==0 and db.one('SELECT updated FROM tasks WHERE hash=?',(h,))['updated']==updated
    db.set('task_verified_'+h,time.time()-3601);e.reconcile()
    assert calls.call_count==1
    q.tasks[h]['state']='missingFiles';e.reconcile()  # a downloader error state is looked at immediately
    task=db.one('SELECT status,error FROM tasks WHERE hash=?',(h,))
    assert calls.call_count==2 and (task['status'],task['error'])==('downloading','missingFiles')


def test_candidates_are_ingested_once_per_show_per_cycle_and_retry_backs_off(setup):
    db,q,e,c=setup
    for n in (1,2,3):db.execute('INSERT INTO sources(show_id,kind,url) VALUES(42,?,?)',('mikan',f'https://mikanani.me/rss{n}'))
    c();c(title='[Group] Example S01E02 [1080p][CHS]',url='https://mikanani.me/b.torrent')
    source=Mock();source.rss.return_value=True;e.sources=source;e.catalog=Mock()
    seen=[];original=e.ingest
    def ingest(candidate):seen.append(candidate['id']);return original(candidate)
    e.ingest=ingest;e.cycle(force=True)
    assert source.rss.call_count==3 and sorted(seen)==[1,2]
    seen.clear();e.sources.torrent=Mock(side_effect=ConnectionError('boom'))
    db.execute("UPDATE candidates SET status='discovered',hash=NULL");db.execute('DELETE FROM tasks');db.execute('DELETE FROM episodes')
    e.cycle(force=True)
    assert seen==[1,2] and all(r['status']=='retry' for r in db.rows('SELECT status FROM candidates'))
    assert db.get('candidate_retry_1')['attempts']==1 and db.get('candidate_retry_1')['next']>time.time()
    seen.clear();e.cycle(force=True)
    assert seen==[]  # backing off: not retried this cycle
    db.set('candidate_retry_1',{'reason':'x','attempts':1,'next':0});seen.clear();e.cycle(force=True)
    assert seen==[1]


def test_safe_error_keeps_refusal_text_and_adds_only_a_code_location(tmp_path):
    assert safe_error(Refusal('理由'))=='理由'
    try:Store(tmp_path/'x.db').rows('SELECT secret FROM nowhere')
    except Exception as e:message=safe_error(e)
    assert message.startswith('OperationalError：请求失败') and '（db.' in message and 'secret' not in message and 'nowhere' not in message
    assert safe_error(KeyError('jobs'))=='KeyError：请求失败，请检查网络或本机接口'
