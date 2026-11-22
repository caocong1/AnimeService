"""Bounded parallel dispatch with synthetic proofs and isolated databases only."""
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from anime.subtitle_delivery import SubtitleDelivery, production_limit
from test_subtitle_delivery import delivery, begin
from test_subtitle_queue_policy import staged, dispatched
from test_dispatch_created_reconciliation import pending, apply
from test_delivery_operator_tools import queue_cli
from test_codex_subtitle_owner_guard import guard


@pytest.mark.parametrize('value', [None, False, True, 0, -1, 4, 3.0, '3', [], {}])
def test_invalid_capacity_retains_default(value):
    assert production_limit({'max_active_productions': value}) == 1


def reserve(scheduler, identity, owner='dispatcher'):
    return scheduler.reserve(identity, owner, scheduler.get(identity)['revision'])


def test_three_distinct_episodes_fill_capacity_including_unknown_and_live_lease(delivery):
    db, scheduler, ready, now = delivery
    scheduler.set_production_limit(3)
    first, intent = begin(scheduler)
    scheduler.reconcile_dispatch(first, intent['dispatch_token'])
    now[0] += 100000  # uncertainty survives expired leases
    reserve(scheduler, 'episode:43:1')
    third, intent = begin(scheduler, 42, 2)
    scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='third-owner')
    assert scheduler.snapshot()['capacity'] == {'limit': 3, 'occupied': 3, 'available': 0}
    with pytest.raises(ValueError, match='capacity'):
        reserve(scheduler, 'episode:43:2')
    assert scheduler.get(first)['dispatch_state'] == 'unknown'


def test_capacity_configuration_preserves_all_owners_receipts_and_other_policy(delivery):
    db, scheduler, ready, now = delivery
    original = {'enabled': True, 'editions': ['v0', 'v1'], 'priority_show_ids': [42]}
    db.set('subtitle_delivery_policy', original)
    scheduler.set_production_limit(3)
    intents = [begin(scheduler, *episode)[1] for episode in [(42, 1), (43, 1), (42, 2)]]
    protected = {table: db.rows('SELECT * FROM ' + table) for table in
                 ('subtitle_delivery_jobs', 'dispatch_receipts', 'episodes', 'watches')}
    assert scheduler.set_production_limit(1)['capacity'] == {'limit': 1, 'occupied': 3, 'available': 0}
    assert db.get('subtitle_delivery_policy') == {**original, 'max_active_productions': 1}
    assert all(db.rows('SELECT * FROM ' + table) == rows for table, rows in protected.items())
    with pytest.raises(ValueError, match='capacity'):
        reserve(scheduler, 'episode:43:2')
    # A real callback for a previously reserved slot is retained after lowering.
    for index, intent in enumerate(intents):
        result = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id=f'owner-{index}')
        assert result['state_changed'] == 1
    assert scheduler.snapshot()['capacity']['occupied'] == 3


def test_capacity_setter_rejects_bad_values_and_malformed_existing_policy(delivery):
    db, scheduler, ready, now = delivery
    before = db.rows('SELECT * FROM kv')
    for value in [True, 0, 4, 2.0, '2']:
        with pytest.raises(ValueError, match='integer'):
            scheduler.set_production_limit(value)
        assert db.rows('SELECT * FROM kv') == before
    db.set('subtitle_delivery_policy', ['retain-invalid-policy'])
    with pytest.raises(ValueError, match='object'):
        scheduler.set_production_limit(3)
    assert db.get('subtitle_delivery_policy') == ['retain-invalid-policy']


def test_concurrent_claims_cannot_overbook_last_slot(delivery):
    db, scheduler, ready, now = delivery
    scheduler.set_production_limit(2)
    reserve(scheduler, 'episode:42:1')
    # Separate Store locks model two independent coordinator processes. SQLite's
    # transaction must arbitrate the final slot, not a shared Python RLock.
    clients = [SubtitleDelivery(queue_cli.ControlStore(db.path), ready, lambda: now[0], initialize=False)
               for _ in range(2)]
    together = threading.Barrier(2)
    def claim(candidate):
        client, identity = candidate
        together.wait(timeout=10)
        try:
            return reserve(client, identity)['id']
        except ValueError as error:
            assert 'capacity' in str(error)
            return None
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, zip(clients, ['episode:43:1', 'episode:42:2'])))
    assert len([result for result in results if result]) == 1
    assert scheduler.snapshot()['capacity'] == {'limit': 2, 'occupied': 2, 'available': 0}


def test_another_job_of_same_episode_never_gets_a_second_writer(delivery):
    db, scheduler, ready, now = delivery
    scheduler.set_production_limit(3)
    first, intent = begin(scheduler)
    db.execute('''INSERT INTO subtitle_delivery_jobs
        (id,show_id,episode,kind,state,input_key,payload,created,updated)
        SELECT 'synthetic-duplicate',show_id,episode,kind,'ready',input_key,payload,created,updated
        FROM subtitle_delivery_jobs WHERE id=?''', (first,))
    with pytest.raises(ValueError, match='single production owner'):
        reserve(scheduler, 'synthetic-duplicate')
    assert scheduler.snapshot()['capacity']['occupied'] == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_one_thread_cannot_confirm_two_active_episodes(delivery, legacy):
    db, scheduler, ready, now = delivery
    scheduler.set_production_limit(3)
    first, intent = begin(scheduler)
    scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='unique-owner')
    second, intent = begin(scheduler, 43, 1)
    before = scheduler.get(second)
    if legacy:
        with pytest.raises(ValueError, match='thread_owner_active'):
            scheduler.reconcile_dispatch(second, intent['dispatch_token'], thread_id='unique-owner')
    else:
        result = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='unique-owner')
        assert result['reason'] == 'thread_owner_active' and result['state_changed'] == 0
    assert scheduler.get(second) == before
    assert scheduler.get(first)['thread_id'] == 'unique-owner'


@pytest.mark.parametrize('change', ['watched', 'unauthorized'])
def test_parallel_capacity_does_not_bypass_scope(delivery, change):
    db, scheduler, ready, now = delivery
    scheduler.set_production_limit(3)
    if change == 'watched':
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    else:
        db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    with pytest.raises(ValueError):
        reserve(scheduler, 'episode:42:1')
    assert scheduler.snapshot()['capacity']['occupied'] == 0


def test_parallel_v0_keeps_its_same_owner_for_v1(staged):
    db, scheduler, ready, now = staged
    scheduler.set_production_limit(3)
    first = dispatched(scheduler, chat='first-owner')
    second = dispatched(scheduler, sid=43, chat='second-owner')
    other = scheduler.get(second)
    result = scheduler.record_finished(first, 'first-owner', 'release', 'proof')
    assert result['continue_refinement'] and result['thread_id'] == 'first-owner'
    assert scheduler.get(second) == other
    assert scheduler.snapshot()['capacity']['occupied'] == 2


def test_explicit_created_reconciliation_uses_configured_capacity(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    scheduler.set_production_limit(3)
    other, intent = begin(scheduler, 43, 1)
    scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='other-owner')
    before = scheduler.get(other)
    result = apply(pending)
    assert result['state_changed'] == 1 and result['job']['thread_id'] == 'synthetic-chat'
    assert scheduler.get(other) == before


def test_capacity_cli_writes_only_the_requested_policy_key(delivery, monkeypatch, capsys):
    db, scheduler, ready, now = delivery
    db.set('subtitle_delivery_policy', {'enabled': True, 'priority_show_ids': [43]})
    monkeypatch.setattr(queue_cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(queue_cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'capacity', '--limit', '3'])
    queue_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['capacity'] == {'limit': 3, 'occupied': 0, 'available': 3}
    assert db.get('subtitle_delivery_policy') == {'enabled': True, 'priority_show_ids': [43], 'max_active_productions': 3}


def test_guard_tracks_distinct_episode_owners_and_rejects_duplicate_identity(tmp_path):
    path = tmp_path / 'guard.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE shows(id,selected,state,authorized,cleanup_hold);
            CREATE TABLE watches(show_id,episode,finished);
            CREATE TABLE subtitle_delivery_jobs(id,show_id,episode,state,dispatch_state,thread_id,input_key,revision,payload);
            INSERT INTO shows VALUES(1,1,'watching',1,0);
            INSERT INTO subtitle_delivery_jobs VALUES('a',1,1,'production','acknowledged','owner-a','i-a',1,'{}');
            INSERT INTO subtitle_delivery_jobs VALUES('b',1,2,'production','acknowledged','owner-b','i-b',1,'{}');''')
    assert {owner['thread_id'] for owner in guard.owners(path)} == {'owner-a', 'owner-b'}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE subtitle_delivery_jobs SET episode=1 WHERE id='b'")
    with pytest.raises(ValueError, match='Conflicting'):
        guard.owners(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE subtitle_delivery_jobs SET episode=2,thread_id='owner-a' WHERE id='b'")
    with pytest.raises(ValueError, match='Conflicting'):
        guard.owners(path)
