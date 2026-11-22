import threading
from concurrent.futures import ThreadPoolExecutor

from anime.db import Store
from anime.engine import Engine
from test_reliability import setup


def test_drop_and_reselect_cannot_replay_a_submission_with_no_receipt(setup):
    db, qb, engine, candidate = setup
    qb.ambiguous = True
    engine.ingest(candidate())
    qb.tasks.clear()
    engine.state(42, 'dropped')
    engine.reconcile()
    assert db.one('SELECT status FROM tasks')['status'] == 'uncertain'
    engine.state(42, 'watching')
    engine.reconcile()
    assert qb.adds == 1 and not qb.starts


def test_busy_request_is_durable_latest_wins_and_never_calls_provider(setup):
    db, qb, engine, _ = setup
    qb.info = lambda *args: (_ for _ in ()).throw(AssertionError('foreground provider I/O'))
    with ThreadPoolExecutor(max_workers=1) as pool, db.gate:
        for state in ('trial', 'dropped'):
            result = pool.submit(engine.request_state, 42, state).result(timeout=2)
            assert result['pending'] == state
            assert result['state'] == 'watching'
    restarted = Engine(Store(db.path), qbit=qb, library=engine.library)
    assert restarted.tracking_status(42)['pending'] == 'dropped'
    thread = threading.Thread(target=restarted.tracking_loop)
    thread.start()
    try:
        assert restarted.wake.wait(2)
        assert restarted.tracking_status(42) == {'pending': None, 'state': 'dropped', 'selected': 1}
        assert not db.eligible(42)
        assert restarted.request_state(42, 'watching')['pending'] is None
        assert restarted.check_for_show(42) > restarted.check_for_show(777)
    finally:
        restarted.stop_event.set()
        restarted.tracking_wake.set()
        thread.join(2)
