import json
from test_reliability import setup
from test_source_review import ready_review,finish_sample


def add_offline_qbit(db,q):
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,provider,created,updated) VALUES('offline',42,'Old qB sample','complete','{}','old','qbit',1,1)")
    q.online=False
    return db.one("SELECT * FROM tasks WHERE hash='offline'")


def test_offline_qbit_does_not_block_safe_thunder_scope_recovery(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    db.execute('UPDATE shows SET selected=0 WHERE id=42');e.reconcile()
    assert db.one('SELECT state FROM thunder_queue')['state']=='cancelled'
    db.execute('UPDATE shows SET selected=1 WHERE id=42')
    previous=add_offline_qbit(db,q);before=db.show(42)
    e.reconcile()
    assert db.one('SELECT state FROM thunder_queue')['state']=='pending'
    assert db.one("SELECT * FROM tasks WHERE hash='offline'")==previous and db.show(42)==before
    assert not db.get('qbit_health')['ok'] and q.adds==0 and not q.starts


def test_offline_qbit_does_not_block_verified_thunder_completion(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    previous=add_offline_qbit(db,q)
    t,s,fp=finish_sample(e,r)
    assert db.one('SELECT status FROM tasks WHERE hash=?',(t['hash'],))['status']=='complete'
    assert db.one("SELECT * FROM tasks WHERE hash='offline'")==previous
    assert not db.get('qbit_health')['ok'] and q.adds==0


def test_completed_readonly_sample_can_publish_without_adopting_downloader(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    db.execute("UPDATE tasks SET status='review',external_id=NULL,acknowledged=0 WHERE hash=?",(t['hash'],))
    db.execute("UPDATE thunder_queue SET state='review',committing=0,lease='' WHERE hash=?",(t['hash'],))
    db.execute("UPDATE source_samples SET state='queued',path='' WHERE hash=?",(t['hash'],))
    readonly=e.thunder.link_reviewed_completed_file(t['hash'],81)
    db.set('play_completed_premiere_enabled',True)
    e.source_review.publish_available()
    episode=db.one('SELECT * FROM episodes')
    assert episode and episode['hash'] is None and episode['path']==readonly['path'] and episode['first_completed'] is None
    assert db.one('SELECT status,acknowledged,external_id FROM tasks')=={'status':'linked','acknowledged':0,'external_id':None}
    assert not db.rows('SELECT * FROM watches') and q.adds==0


def test_first_episode_scope_is_rechecked_at_thunder_claim_and_final_click(setup):
    import pytest
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);job=e.thunder.claim()
    m=db.mapping(42);m['start']=2;db.execute('UPDATE shows SET mapping=?',(json.dumps(m),))
    # The current policy rejects the invalid mapping at the outer scope gate.
    with pytest.raises(ValueError,match='想看评审范围'):e.thunder.preflight(job['hash'],job['lease'])
    assert db.one('SELECT committing FROM thunder_queue')['committing']==0
    db.execute("UPDATE thunder_queue SET state='pending',lease='',expires=0")
    assert e.thunder.claim() is None and q.adds==0
