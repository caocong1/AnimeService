import json
from pathlib import Path

import pytest
from test_reliability import setup
from test_subtitle_delivery_integration import prepared_candidate
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_delivery_view import catalogue, report_progress, resources


def test_report_stage_requires_active_exact_owner_and_does_not_change_inputs(setup, monkeypatch):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    scheduler=SubtitleDelivery(db);job=scheduler.record_preparation(42,2)
    lease=scheduler.reserve(job['id'],'fixture dispatcher',job['revision'])
    intent=scheduler.begin_dispatch(job['id'],'fixture dispatcher',lease['lease_token'],lease['revision'])
    with pytest.raises(ValueError):report_progress(db,job['id'],'fixture writer','writing')
    scheduler.reconcile_dispatch(job['id'],intent['dispatch_token'],thread_id='fixture writer')
    before=(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'),scheduler.get(job['id']))
    with pytest.raises(ValueError):report_progress(db,job['id'],'different writer','rendering')
    with pytest.raises(ValueError):report_progress(db,job['id'],'fixture writer','writing',3,2)
    report_progress(db,job['id'],'fixture writer','writing',1,3)
    view=catalogue(db);row=next(x for x in view['jobs'] if x['job_id']==job['id'])
    assert row['progress']['stage']=='writing' and row['progress']['done']==1
    assert 'fixture writer' not in json.dumps(view) and str(db.path.parent) not in json.dumps(view)
    assert 'lease_token' not in json.dumps(view) and 'dispatch_token' not in json.dumps(view)
    assert before==(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'),scheduler.get(job['id']))
    scheduler.record_blocked(job['id'],'fixture writer',['fixture-only'],owner_stopped=True)
    with pytest.raises(ValueError):report_progress(db,job['id'],'fixture writer','checking')
    assert q.adds==0


def test_resource_counts_use_same_episode_actual_files_dedupe_and_fidelity(setup,monkeypatch):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    # Existing reconciled fixture master is a raw/soft candidate only when labelled.
    db.execute("UPDATE tasks SET title='[Group] Example S01E02 Multi-Subs'")
    assert resources(db,42,2)['original']['complete']==1
    assert sum(sum(v.values()) for v in resources(db,42,3).values())==0
    Path(target['path']).write_bytes(b'changed size')
    assert resources(db,42,2)['original']['complete']==0
    assert resources(db,42,2)['original']['missing']==1
    db.execute("UPDATE tasks SET title='[Skymoon-Raws] Example Multi-Subs'")
    db.set('source_publisher_blacklist',[{'publisher':'Skymoon-Raws','aliases':['Skymoon-Raws'],'reason':'synthetic advert exclusion fixture'}])
    # The actual project fidelity predicate owns exclusion; no downloader calls.
    from anime.fidelity import fidelity_blocker
    assert fidelity_blocker(db,'[Skymoon-Raws] Example Multi-Subs')
    assert sum(sum(v.values()) for v in resources(db,42,2).values())==0
    assert q.adds==0
