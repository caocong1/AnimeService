import json
from pathlib import Path

import pytest
from test_reliability import setup
from test_subtitle_delivery_integration import prepared_candidate
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_delivery_view import catalogue, report_progress, resources
from test_subtitle_delivery import delivery, begin


@pytest.mark.requires_lab
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


def test_public_live_projection_shares_queue_gates_and_never_changes_business_data(delivery):
    from anime.subtitle_queue import production_queue
    db, scheduler, _, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='private-original-owner')
    scheduler.record_blocked(identity, 'private-original-owner', ['private encoder path'],
                             owner_stopped=True, user_stopped=True)
    before = {t: db.rows(f'SELECT * FROM {t}') for t in
              ('episodes', 'watches', 'tasks', 'subtitle_delivery_jobs', 'subtitle_delivery_events')}
    view = catalogue(db)
    queue = production_queue(db)
    row = next(j for j in view['jobs'] if j['job_id'] == identity)
    item = next(j for j in queue['items'] if j['show_id'] == 42 and j['episode'] == 1)
    assert view['live'] is True
    assert row['work']['status'] == item['status'] == 'blocked'
    assert row['work']['stop_class'] == item['stop_class'] == 'user_stopped'
    text = json.dumps(view)
    for private in ('private-original-owner', 'private encoder path', 'lease_token', 'dispatch_token', str(db.path.parent)):
        assert private not in text
    assert all(db.rows(f'SELECT * FROM {t}') == value for t, value in before.items())


def test_future_and_watched_facts_are_live_and_do_not_depend_on_export(delivery):
    db, _, _, _ = delivery
    db.set('airing_sort_offset_42', 13)
    db.execute("INSERT INTO airings(show_id,episode,airdate,title) VALUES(42,14,'2999-01-24','future E1')")
    row = next(j for j in catalogue(db)['jobs'] if j['job_id'] == 'episode:42:1')
    assert row['work']['status'] == 'unreleased' and row['work']['release_date'] == '2999-01-24'
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(43,1,1)')
    row = next(j for j in catalogue(db)['jobs'] if j['job_id'] == 'episode:43:1')
    assert row['watched'] is True


def test_public_projection_batches_resources_and_uses_one_read_snapshot(delivery, monkeypatch):
    import anime.read_projection as projection
    db, _, _, _ = delivery
    original = projection.sqlite3.connect
    calls, resource_reads = [], []
    def connect(*args, **kwargs):
        calls.append(args[0])
        con = original(*args, **kwargs)
        con.set_trace_callback(lambda sql: resource_reads.append(sql) if sql.startswith('SELECT show_id,hash,title') else None)
        return con
    monkeypatch.setattr(projection.sqlite3, 'connect', connect)
    assert len(catalogue(db)['jobs']) == 4
    assert len(calls) == len(resource_reads) == 1 and str(calls[0]).endswith('?mode=ro')


def test_library_queue_positions_share_free_capacity_and_live_exclusions(delivery):
    from anime.subtitle_queue import production_queue
    db, scheduler, _, _ = delivery
    db.set('subtitle_delivery_policy', {'max_active_productions': 3})
    begin(scheduler)  # An uncertain start keeps its slot occupied.
    for excluded in (None, 'watched', 'future'):
        if excluded == 'watched':
            db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(43,1,1)')
        elif excluded == 'future':
            db.execute("INSERT INTO airings(show_id,episode,airdate,title) VALUES(42,2,'2999-01-24','future E2')")
        before = db.rows('SELECT * FROM subtitle_delivery_jobs')
        queue = production_queue(db)
        view = catalogue(db)
        rows = {r['job_id']: r for r in view['jobs']}
        pending = [i for i in queue['items'] if i['status'] == 'queued']
        assert len(pending) == 3 - (excluded is not None) - (excluded == 'future')
        assert view['capacity'] == queue['capacity'] == {'limit': 3, 'occupied': 1, 'available': 2}
        for item in pending:
            current = rows[f"episode:{item['show_id']}:{item['episode']}"]['work']
            assert {k: current[k] for k in ('status', 'position', 'ahead', 'capacity_available')} == {
                k: item[k] for k in ('status', 'position', 'ahead', 'capacity_available')}
        assert db.rows('SELECT * FROM subtitle_delivery_jobs') == before


@pytest.mark.parametrize('separate_job', [True, False])
def test_library_v1_and_v0_wait_in_the_same_global_line(delivery, separate_job):
    from anime.subtitle_queue import production_queue
    db, scheduler, _, now = delivery
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'fixture-v0', 'edition': 'v0',
           'delivery_status': 'finished', 'manifest_sha256': 'f' * 64, 'accepted': False})
    identity = 'episode:42:1'
    if separate_job:
        identity = 'refinement:42:1:fixture'
        db.execute('''INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,created,updated)
                      VALUES(?,42,1,'refinement','ready','fixture-input',?,?,?)''',
                   (identity, json.dumps({'assessment': {}, 'edition': 'v1'}), now[0], now[0]))
    else:
        job = scheduler.get(identity)
        db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?',
                   (json.dumps({**job['payload'], 'refinement_pending': True, 'edition': 'v1'}), identity))
    queue = production_queue(db)
    view = catalogue(db)
    first = next(i for i in queue['items'] if (i['show_id'], i['episode']) == (42, 1))
    current = next(r['work'] for r in view['jobs'] if r['job_id'] == identity)
    assert first['status'] == 'v0_ready' and current['status'] == first['refinement']['status'] == 'queued'
    assert current['position'] == first['refinement']['position'] == 1
    other = next(r['work'] for r in view['jobs'] if r['job_id'] == 'episode:43:1')
    assert other['position'] == 2 and other['ahead'] == 1 and other['capacity_available'] == 1


def test_uninitialized_snapshot_has_complete_read_only_contract(tmp_path):
    from anime.db import Store
    db = Store(tmp_path / 'data/test.db')
    before = db.rows("SELECT name FROM sqlite_master WHERE type='table'")
    snapshot = SubtitleDelivery(db, initialize=False).snapshot()
    assert snapshot['jobs'] == [] and 'capacity' in snapshot and 'recovery' in snapshot
    assert snapshot['preparation']['available'] == []
    assert catalogue(db)['jobs'] == []
    assert db.rows("SELECT name FROM sqlite_master WHERE type='table'") == before


@pytest.mark.requires_lab
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
