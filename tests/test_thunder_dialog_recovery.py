"""Recover a known unsubmitted dialog without claiming unrelated work."""
import pytest
from test_reliability import setup, torrent
from test_thunder_bridge import ready


def two_jobs(setup):
    db, qb, engine, reader, first = ready(setup)
    engine.sources.data = torrent((2,))
    engine.ingest(setup[3]('[Group] Example S01E02 [1080p][CHS]', source=2))
    second = db.one('SELECT * FROM tasks WHERE hash<>?', (first['hash'],))
    return db, qb, engine, first, second


def test_recover_returned_dialog_even_when_another_job_is_older(setup):
    db, qb, engine, first, second = two_jobs(setup)
    job = engine.thunder.claim(task_hash=first['hash'])
    engine.thunder.report(first['hash'], job['lease'], 'directory input timed out; dialog left open')
    db.execute('UPDATE thunder_queue SET updated=0 WHERE hash=?', (second['hash'],))
    other_before = db.one('SELECT * FROM thunder_queue WHERE hash=?', (second['hash'],))
    recovered = engine.thunder.claim(task_hash=first['hash'])
    assert recovered['hash'] == first['hash'] and not recovered['uncertain']
    assert db.one('SELECT * FROM thunder_queue WHERE hash=?', (second['hash'],)) == other_before
    assert qb.adds == 0 and not qb.starts and not db.rows('SELECT * FROM watches')


def test_exact_recovery_respects_global_lease(setup):
    db, qb, engine, first, second = two_jobs(setup)
    engine.thunder.claim(task_hash=second['hash'])
    before = db.rows('SELECT * FROM thunder_queue ORDER BY hash')
    assert engine.thunder.claim(task_hash=first['hash']) is None
    assert db.rows('SELECT * FROM thunder_queue ORDER BY hash') == before and qb.adds == 0


def test_unavailable_target_never_falls_back_to_other_pending_work(setup):
    db, qb, engine, first, second = two_jobs(setup)
    before = db.rows('SELECT * FROM thunder_queue ORDER BY hash')
    assert engine.thunder.claim(task_hash='0' * 40) is None
    assert engine.thunder.claim(show_id=99, task_hash=first['hash']) is None
    assert db.rows('SELECT * FROM thunder_queue ORDER BY hash') == before
    job = engine.thunder.claim(task_hash=first['hash'])
    engine.thunder.preflight(first['hash'], job['lease'])
    engine.thunder.report(first['hash'], job['lease'], 'final click outcome unknown')
    before = db.rows('SELECT * FROM thunder_queue ORDER BY hash')
    assert engine.thunder.claim(task_hash=first['hash']) is None
    assert db.rows('SELECT * FROM thunder_queue ORDER BY hash') == before
    assert db.one('SELECT committing FROM thunder_queue WHERE hash=?', (first['hash'],))['committing'] == 1
    assert qb.adds == 0


def test_exact_recovery_does_not_revive_dropped_show(setup):
    db, qb, engine, reader, task = ready(setup)
    engine.state(42, 'dropped')
    assert engine.thunder.claim(task_hash=task['hash']) is None
    assert db.show(42)['state'] == 'dropped' and qb.adds == 0


@pytest.mark.parametrize('bad_hash', ['', 'a' * 39, 'A' * 40, 123])
def test_recovery_rejects_invalid_hash_before_queue_mutation(setup, bad_hash):
    db, qb, engine, reader, task = ready(setup)
    before = db.rows('SELECT * FROM thunder_queue')
    with pytest.raises(ValueError, match='hash'):
        engine.thunder.claim(task_hash=bad_hash)
    assert db.rows('SELECT * FROM thunder_queue') == before and qb.adds == 0


def test_claim_api_forwards_exact_hash_and_rejects_invalid_input(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db, qb, engine, first, second = two_jobs(setup)
    with TestClient(create_app(db, engine, False), base_url='http://127.0.0.1:4871') as client:
        headers = {'X-Anime-Token': client.get('/api/bootstrap').json()['token']}
        assert client.post('/api/thunder/claim', json={'hash': 'bad'}, headers=headers).status_code == 400
        assert client.post('/api/thunder/claim', json={'hash': '0' * 40}, headers=headers).json()['job'] is None
        response = client.post('/api/thunder/claim', json={'hash': second['hash']}, headers=headers)
        assert response.status_code == 200 and response.json()['job']['hash'] == second['hash']
    assert db.one('SELECT state FROM thunder_queue WHERE hash=?', (first['hash'],))['state'] == 'pending'
    assert qb.adds == 0 and not qb.starts
