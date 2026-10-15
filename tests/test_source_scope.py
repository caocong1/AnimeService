import datetime,json,time
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from test_reliability import setup
from test_source_review import ready_review,finish_sample
from anime.season import install_season


def enroll_fixture(setup,continuing=False):
    db,q,e,c=setup
    db.set('source_review_new_shows',True);db.set('auto_quarter_enabled',True);db.set('auto_quarter_start','2026-10')
    db.execute("UPDATE shows SET quarter='2026-10' WHERE id=42")
    listing=lambda **kw:[{**db.show(42),'quarter_kind':'continuing' if continuing else 'new'}]
    prepare=Mock(side_effect=AssertionError('existing authorization must not be reinitialized'))
    install_season(FastAPI(),db,e,listing,prepare,Mock())
    return db,q,e,prepare


def test_existing_authorized_new_series_is_enrolled_without_touching_episode_or_watch(setup):
    db,q,e,prepare=enroll_fixture(setup)
    db.execute("INSERT INTO episodes(show_id,episode,status,path,first_completed) VALUES(42,1,'complete','existing.mkv',100)")
    db.execute("INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)")
    show=db.show(42);episode=db.rows('SELECT * FROM episodes');watches=db.rows('SELECT * FROM watches')
    e.enroll_source_reviews(datetime.date(2026,10,6));policy=e.source_review.policy(42)
    assert policy['mode']=='sampling' and policy['enrollment']=='verified_existing_authorization'
    e.enroll_source_reviews(datetime.date(2026,10,6));assert e.source_review.policy(42)==policy
    assert db.show(42)==show and db.rows('SELECT * FROM episodes')==episode and db.rows('SELECT * FROM watches')==watches
    assert not prepare.called and q.adds==0 and not db.rows('SELECT * FROM tasks')


@pytest.mark.parametrize('protection',['paused','dropped','completed','cleanup_hold','unconfirmed','old','continuing','disabled','not_personally_selected'])
def test_enrollment_never_revives_protected_old_or_unverified_series(setup,protection):
    db,q,e,prepare=enroll_fixture(setup,protection=='continuing')
    if protection in ('paused','dropped','completed'):db.execute('UPDATE shows SET state=? WHERE id=42',(protection,))
    elif protection=='cleanup_hold':db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    elif protection=='unconfirmed':
        mapping=db.mapping(42);mapping['confirmed']=False;db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(mapping),))
    elif protection=='old':db.execute("UPDATE shows SET quarter='2026-07' WHERE id=42")
    elif protection=='disabled':db.set('source_review_new_shows',False)
    elif protection=='not_personally_selected':db.execute('UPDATE shows SET selected=0 WHERE id=42')
    e.enroll_source_reviews(datetime.date(2026,10,6))
    assert e.source_review.policy(42)=={} and q.adds==0 and not prepare.called


def test_enrollment_preserves_selected_policy(setup):
    db,q,e,prepare=enroll_fixture(setup)
    policy={'mode':'selected','primary':'old','backups':['backup'],'wait_hours':24}
    e.source_review.save(42,policy);e.enroll_source_reviews(datetime.date(2026,10,6))
    assert e.source_review.policy(42)==policy and q.adds==0


def test_explicit_early_quarter_member_is_enrolled_without_summer_backfill(setup):
    db,q,e,prepare=enroll_fixture(setup)
    db.execute("UPDATE shows SET quarter='2026-07' WHERE id=42")
    db.execute("INSERT INTO quarter_members VALUES(42,'2026-10','early','verified early premiere')")
    e.enroll_source_reviews(datetime.date(2026,10,6))
    assert e.source_review.policy(42)['mode']=='sampling' and q.adds==0 and not prepare.called


def test_overview_includes_pending_series_without_fabricating_reviews_or_exposing_secrets(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    policy=e.source_review.policy(42);before=db.rows('SELECT * FROM source_samples')
    result=e.source_review.overview();show=result['shows'][0]
    assert result['summary']=={'enrolled':1,'selected':0,'sampling':1,'protected':0}
    assert show['pending_samples']==1 and show['reviewed_chinese_samples']==0 and not result['review_queue']
    assert show['stage']=='waiting_samples'
    assert not any(x in json.dumps(result) for x in ('selection','lease','https://mikanani.me','token'))
    assert db.rows('SELECT * FROM source_samples')==before and e.source_review.policy(42)==policy and q.adds==0


def test_overview_completion_order_is_independent_of_updated_timestamp(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    db.execute('UPDATE source_samples SET evidence=?,updated=? WHERE candidate_id=?',
        (json.dumps({'fingerprint':fp,'completion_observed_at':100}),time.time(),c['id']))
    result=e.source_review.overview()
    assert result['review_queue']==[42] and result['shows'][0]['first_completed_observed_at']==100
    assert result['shows'][0]['reviewed_chinese_samples']==0


def test_overview_endpoint_is_read_only(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        assert client.get('/api/source-review/overview').json()['summary']['sampling']==1
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches') and q.adds==0


def test_default_quarter_authorization_does_not_enable_personal_review(setup):
    db,q,e,r,c=ready_review(setup)
    db.execute("UPDATE shows SET selected=0,state='wish',auto_download=1 WHERE id=42")
    assert db.allowed(42,1) and not db.review_wanted(42)
    with pytest.raises(ValueError,match='想看'):e.source_review.start(42)
    assert not db.rows('SELECT * FROM source_samples') and q.adds==0


def test_unwanted_review_history_is_hidden_from_active_overview_and_cannot_be_changed(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);finish_sample(e,r)
    samples=db.rows('SELECT * FROM source_samples');db.execute('UPDATE shows SET selected=0 WHERE id=42')
    assert e.source_review.overview()['summary']['enrolled']==0
    with pytest.raises(ValueError,match='想看'):e.source_review.record_review(42,c['id'],{})
    assert db.rows('SELECT * FROM source_samples')==samples


def test_unsubmitted_unwanted_samples_cancel_without_touching_ordinary_download_authorization(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    db.execute('UPDATE shows SET selected=0 WHERE id=42');show=db.show(42)
    e.reconcile();e.reconcile()
    assert db.one('SELECT status FROM tasks')['status']=='held'
    assert db.one('SELECT state FROM thunder_queue')['state']=='cancelled'
    assert db.one('SELECT state FROM source_samples')['state']=='scope_hold'
    assert e.thunder.claim() is None and db.show(42)==show and q.adds==0
    assert db.allowed(42,1)


def test_scope_change_after_claim_blocks_final_sample_click_without_erasing_lease(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);job=e.thunder.claim()
    db.execute('UPDATE shows SET selected=0 WHERE id=42')
    before=db.one('SELECT * FROM thunder_queue')
    with pytest.raises(ValueError,match='想看'):e.thunder.preflight(job['hash'],job['lease'])
    assert db.one('SELECT * FROM thunder_queue')==before and not before['committing']


def test_scope_change_preserves_uncertain_submission(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);job=e.thunder.claim();e.thunder.preflight(job['hash'],job['lease'])
    db.execute("UPDATE thunder_queue SET lease='',expires=0 WHERE hash=?",(job['hash'],))
    db.execute('UPDATE shows SET selected=0 WHERE id=42')
    before=db.one('SELECT * FROM thunder_queue');e.reconcile()
    assert db.one('SELECT * FROM thunder_queue')==before and before['committing']==1
    assert e.thunder.claim() is None


def test_personally_selecting_series_reenables_scope_held_review(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    db.execute('UPDATE shows SET selected=0 WHERE id=42');e.reconcile()
    e.source_review.save(42,{'mode':'scope_hold','scope_previous_mode':'sampling','scope_hold_reason':'not wanted','primary':'','backups':[],'wait_hours':24})
    db,q,e,prepare=enroll_fixture((db,q,e,c));e.state(42,'watching');e.enroll_source_reviews(datetime.date(2026,10,6))
    assert e.source_review.policy(42)['mode']=='sampling'
    e.source_review.start(42);e.reconcile()
    assert db.one('SELECT state FROM source_samples')['state']=='queued'
    assert db.one('SELECT state FROM thunder_queue')['state']=='pending' and q.adds==0
