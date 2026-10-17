import datetime
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from test_reliability import setup
from anime.season import install_season
from anime.subtitle_delivery import SubtitleDelivery, conservative_readiness
from anime.subtitle_enrollment import coverage, reconcile
from anime.viewing_workflow import KEY, POLICY


def enabled(setup):
    db,q,e,c=setup
    db.set(KEY,POLICY)
    db.execute("UPDATE shows SET quarter='2026-07' WHERE id=42")
    install_season(FastAPI(),db,e,Mock(side_effect=AssertionError('no cohort lookup')),
                   Mock(side_effect=AssertionError('no authorization granted')),Mock())
    return db,q,e,c


def test_continuing_watching_enrolls_even_when_premiere_switches_are_off(setup):
    db,q,e,c=enabled(setup)
    protected={table:db.rows(f'SELECT * FROM {table}') for table in ('shows','episodes','watches','tasks')}
    db.set('source_review_new_shows',False);db.set('auto_quarter_enabled',False)
    e.enroll_source_reviews(datetime.date(2026,10,16))
    assert e.source_review.policy(42)['mode']=='subtitle_only'
    assert db.get('subtitle_delivery_readiness_42_1')['requested_editions']==['v0','v1']
    assert q.adds==0 and all(db.rows(f'SELECT * FROM {t}')==rows for t,rows in protected.items())
    policy=db.one('SELECT * FROM source_policies')
    assert reconcile(db)=={'policies_added':[],'requests_added':0,'skipped':[]}
    assert db.one('SELECT * FROM source_policies')==policy


@pytest.mark.parametrize('guard',['unauthorized','unconfirmed','inventory','bounds','paused','hold','unselected','policy_hold','disabled'])
def test_automatic_repair_never_grants_or_restores_protected_scope(setup,guard):
    db,q,e,c=enabled(setup)
    if guard=='unauthorized':db.execute('UPDATE shows SET authorized=0')
    elif guard=='unconfirmed' or guard=='inventory' or guard=='bounds':
        m=db.mapping(42)
        m[{'unconfirmed':'confirmed','inventory':'inventory_checked','bounds':'start'}[guard]]=2 if guard=='bounds' else False
        db.execute('UPDATE shows SET mapping=?',(json.dumps(m),))
    elif guard=='paused':db.execute("UPDATE shows SET state='paused'")
    elif guard=='hold':db.execute('UPDATE shows SET cleanup_hold=1')
    elif guard=='unselected':db.execute('UPDATE shows SET selected=0')
    elif guard=='policy_hold':e.source_review.save(42,{'mode':'scope_hold','scope_previous_mode':'sampling'})
    elif guard=='disabled':db.set(KEY,{'version':1,'enabled':False})
    before=db.rows('SELECT * FROM source_policies')
    e.enroll_source_reviews()
    assert db.rows('SELECT * FROM source_policies')==before
    assert db.get('subtitle_delivery_readiness_42_1') is None and q.adds==0


def test_state_transition_enrolls_without_waiting_for_next_quarter(setup):
    db,q,e,c=enabled(setup)
    db.execute("UPDATE shows SET state='wish'")
    assert not reconcile(db)['policies_added']
    e.state(42,'watching')
    assert e.source_review.policy(42)['mode']=='subtitle_only' and q.adds==0


def test_refresh_recovers_deleted_policy_and_later_material_without_touching_owner(setup):
    db,q,e,c=enabled(setup)
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    for ep in (1,2,5):
        db.execute("INSERT INTO episodes(show_id,episode,status,path) VALUES(42,?,'complete','fixture')",(ep,))
    scheduler=SubtitleDelivery(db,conservative_readiness)
    initial=scheduler.refresh()
    assert initial['enrollment']['policies_added']==[42]
    assert {j['episode'] for j in initial['jobs']}=={2,5}
    assert db.get('subtitle_delivery_readiness_42_1') is None
    for ep in (2,5):
        request=db.get(f'subtitle_delivery_readiness_42_{ep}')
        assert request['dispatch_standard']=='finished_v1' and 'material_ready' not in request
    db.execute("UPDATE subtitle_delivery_jobs SET state='production',dispatch_state='acknowledged',thread_id='existing-owner' WHERE episode=2")
    owner=scheduler.get('episode:42:2')
    db.execute('DELETE FROM source_policies')
    assert coverage(db)['missing_eligible_policies'][0]['show_id']==42
    db.execute("INSERT INTO episodes(show_id,episode,status,path) VALUES(42,6,'complete','fixture')")
    scheduler.refresh()
    after=scheduler.get('episode:42:2')
    assert all(after[k]==owner[k] for k in ('state','thread_id','dispatch_state','input_key'))
    assert not scheduler.snapshot()['enrollment_coverage']['missing_eligible_policies']
    assert scheduler.get('episode:42:6')['state']=='preparing'
    before=db.rows('SELECT * FROM kv')
    scheduler.snapshot()
    assert db.rows('SELECT * FROM kv')==before and q.adds==0


def test_existing_policy_and_evidence_are_preserved(setup):
    db,q,e,c=enabled(setup)
    e.source_review.save(42,{'mode':'selected','primary':'reviewed','backups':['comparison']})
    db.set('subtitle_delivery_readiness_42_1',{'target':{'fingerprint':'retained'},'custom_fact':True})
    policy=db.one('SELECT * FROM source_policies')
    reconcile(db)
    assert db.one('SELECT * FROM source_policies')==policy
    assert db.get('subtitle_delivery_readiness_42_1')['target']=={'fingerprint':'retained'}


def test_all_watched_show_gets_policy_without_reopening_any_episode(setup):
    db,q,e,c=enabled(setup)
    for ep in range(1,13):
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,?,1)',(ep,))
    assert reconcile(db)['policies_added']==[42]
    assert not db.rows("SELECT * FROM kv WHERE key LIKE 'subtitle_delivery_readiness_%'")
    assert not coverage(db)['missing_eligible_policies'] and q.adds==0


def test_racing_authorization_revocation_is_rechecked_in_write_transaction(setup,monkeypatch):
    db,q,e,c=enabled(setup)
    from anime.subtitle_enrollment import enroll
    original=db.allowed
    def revoke(sid,ep):
        allowed=original(sid,ep)
        db.execute('UPDATE shows SET authorized=0')
        return allowed
    monkeypatch.setattr(db,'allowed',revoke)
    with pytest.raises(ValueError):enroll(db,42,[1])
    assert not db.rows('SELECT * FROM source_policies')
    assert db.get('subtitle_delivery_readiness_42_1') is None
