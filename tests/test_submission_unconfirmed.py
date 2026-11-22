"""A sent add that qB has not listed yet is a pending state: no error, no retry, never a second add."""
from test_reliability import setup,torrent
from test_download_recovery import pending
from test_viewing_workflow import split,candidate
from anime.download_recovery import recover_downloads

NOTE='提交后尚未确认入队，下一轮先按hash查询'


def lag(q,times=1):
    """qB accepts the add but lists it only after `times` lookups by hash."""
    info,add=q.info,q.add;state={'left':0}
    def lagging_add(*a):
        add(*a);state['left']=times
    def lagging_info(h=None):
        if h is not None and state['left']:
            state['left']-=1;return []
        return info(h)
    q.add,q.info=lagging_add,lagging_info


def test_unlisted_add_keeps_candidate_reserved_and_confirms_by_hash(setup):
    db,q,e,c=setup;lag(q);cand=c()
    e.ingest(cand)
    task=db.one('SELECT * FROM tasks')
    assert q.adds==1 and task['status']=='uncertain' and task['error']==NOTE and not task['acknowledged']
    assert db.one('SELECT status FROM candidates WHERE id=?',(cand['id'],))['status']=='reserved'
    assert db.get('candidate_retry_'+str(cand['id'])) is None
    assert not db.rows("SELECT * FROM events WHERE level='error'")
    e.reconcile()
    task=db.one('SELECT * FROM tasks')
    assert q.adds==1 and task['status']=='downloading' and task['acknowledged']==1 and task['error']==''


def test_add_never_listed_is_never_resubmitted(setup):
    db,q,e,c=setup;lag(q,times=10**6);e.ingest(c())
    e.reconcile();e.reconcile()
    task=db.one('SELECT * FROM tasks')
    assert q.adds==1 and task['status']=='uncertain' and task['error']!=''
    assert db.get('task_failure_'+task['hash'])  # a silently dropped add stays visible


def test_studio_acquire_reports_pending_confirmation_without_error(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    from scripts.repair_subtitle_enrollment import enroll
    db,q,e,r,_=split(setup)
    db.execute('DELETE FROM source_policies WHERE show_id=42');enroll(db,42,[1])
    c=candidate(setup,1,'简繁内封',1);e.sources.data=torrent((1,));lag(q,times=10**6)
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        headers={'X-Anime-Token':client.get('/api/bootstrap').json()['token']}
        route='/api/subtitle-studio/42/1/acquire';body={'candidate_ids':[c['id']]}
        response=client.post(route,json=body,headers=headers)
        assert response.status_code==202 and response.json()['pending_confirmation'] is True
        assert response.json()['not_processed']==[]
        assert client.post(route,json=body,headers=headers).status_code==200
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='uncertain'
    assert not db.rows("SELECT * FROM events WHERE scope='api'")


def test_fallback_handoff_with_unlisted_add_keeps_batch_healthy(setup):
    db,q,e,r,c=pending(setup);lag(q)
    recover_downloads(e)
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='uncertain'
    assert db.get('qbit_fallback_health')['ok'] is True
    assert not db.rows("SELECT * FROM events WHERE scope='recovery'")
    e.reconcile()
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='downloading'
