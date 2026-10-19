import json

from test_subtitle_delivery import delivery
from anime.subtitle_preparation import preparation_plan


def media(db, tmp_path, sid=42, ep=1):
    path = tmp_path / f'{sid}-{ep}.mkv'
    path.write_bytes(b'actual fixture bytes')
    selection = {'selected': {str(ep): {'name': path.name, 'size': path.stat().st_size}}}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path) VALUES(?,?,?,'complete',?,?)",
               (f'{sid}-{ep}', sid, 'Fixture Multi-Subs', json.dumps(selection), str(tmp_path)))
    db.execute('CREATE TABLE IF NOT EXISTS subtitle_studio_media(show_id,episode,updated)')
    db.execute('INSERT INTO subtitle_studio_media VALUES(?,?,1500)', (sid, ep))
    return path


def jobs(scheduler):
    rows = scheduler.snapshot()['jobs']
    for row in rows:
        row['state'] = 'preparing'
        row['payload']['assessment']['reasons'] = ['needs_reliable_chinese_dialogue_base']
    return rows


def test_completed_media_not_ready_and_plan_read_only(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    before = db.rows('SELECT * FROM subtitle_delivery_jobs'), db.rows('SELECT * FROM watches')
    result = preparation_plan(db, rows, now=30000)
    assert result['summary']['available'] == 1
    assert result['available'][0]['actions'] == ['find_or_review_chinese_dialogue']
    assert result['available'][0]['attention_required']
    assert result['summary']['awaiting_media'] == 3
    assert before == (db.rows('SELECT * FROM subtitle_delivery_jobs'), db.rows('SELECT * FROM watches'))
    assert str(tmp_path) not in json.dumps(result)


def test_unknown_stopped_owner_scope_and_missing_file_protected(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    path = media(db, tmp_path)
    for patch in ({'dispatch_state': 'committing'}, {'dispatch_state': 'blocked-stopped'},
                  {'thread_id': 'actual-owner'}, {'lease_owner': 'dispatcher'},
                  {'scope_blocker': 'outside scope'}):
        rows = jobs(scheduler)
        rows[0].update(patch)
        assert not preparation_plan(db, rows)['available']
    path.unlink()
    assert not preparation_plan(db, jobs(scheduler))['available']


def test_missed_delivery_requires_observed_media_before_watch(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    db.execute('INSERT INTO watches(show_id,episode,finished,updated) VALUES(42,1,1,2000)')
    result = preparation_plan(db, jobs(scheduler), now=30000)
    assert not result['available']
    assert result['watched_before_delivery'][0]['action'] == 'retain_history_no_rework'
    db.execute('UPDATE subtitle_studio_media SET updated=2500')
    assert not preparation_plan(db, jobs(scheduler))['watched_before_delivery']


def test_refresh_timestamp_does_not_hide_wait_and_active_owner_does_not_hide_other_show(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path, 42, 1)
    media(db, tmp_path, 43, 1)
    media(db, tmp_path, 43, 2)
    db.execute('UPDATE subtitle_studio_media SET updated=1200 WHERE show_id=43 AND episode=2')
    rows = jobs(scheduler)
    next(r for r in rows if r['id'] == 'episode:42:1')['dispatch_state'] = 'acknowledged'
    row = next(r for r in rows if r['id'] == 'episode:43:1')
    row['updated'] = 29999
    result = preparation_plan(db, rows, now=30000)
    assert [r['job_id'] for r in result['available']] == ['episode:43:1', 'episode:43:2']
    assert result['available'][0]['waiting_seconds'] == 28500


def test_projection_reads_consistent_snapshot_and_rejects_mutation(delivery):
    import pytest
    import sqlite3
    from anime.read_projection import read_projection
    db, scheduler, _, _ = delivery
    with read_projection(db.path) as readonly:
        before = readonly.get('snapshot-fixture', 'before')
        db.set('snapshot-fixture', 'after')
        assert readonly.get('snapshot-fixture', 'before') == before
        with pytest.raises(ValueError):
            readonly.execute('DELETE FROM watches')
        with pytest.raises(sqlite3.OperationalError):
            readonly.con.execute('DELETE FROM watches')
        with readonly.connect() as connection:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute('DELETE FROM watches')
    assert db.get('snapshot-fixture') == 'after'
