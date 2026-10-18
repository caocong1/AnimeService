"""Caller protocol through the real CLI; temporary DB, fake host, no IPC or production access."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from anime.subtitle_delivery import validate_ticket
from test_subtitle_delivery import delivery  # noqa: F401


@pytest.fixture
def caller(delivery, monkeypatch, capsys):
    db, scheduler, ready, now = delivery
    spec = importlib.util.spec_from_file_location('caller_queue_cli', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    calls = []

    def invoke(*args):
        monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), *args])
        cli.main()
        return json.loads(capsys.readouterr().out)

    identity = scheduler.episode_id(42, 1)

    def begin():
        row = scheduler.get(identity)
        result = invoke('begin', identity, '--owner', 'synthetic-dispatcher', '--revision', str(row['revision']))
        ticket_path = Path(result['ticket_path'])
        assert ticket_path.is_file() and ticket_path.parent == db.path.parent / 'subtitle-delivery-tickets'
        ticket = validate_ticket(json.loads(ticket_path.read_text(encoding='utf-8')))
        assert ticket['job_id'] == identity and ticket['revision'] == result['revision']
        assert 'dispatch_token' not in result and 'lease_token' not in result
        return result, str(ticket_path), ticket

    def host(result, lose_response=False):
        # This is deliberately a fake transport, not a production automation adapter.
        calls.append(result['dispatch_action'])
        if lose_response:
            raise TimeoutError('synthetic response lost after host side effect')
        return result.get('responsible_thread_id') or 'synthetic-created-thread'

    return db, scheduler, identity, cli, invoke, begin, host, calls


def test_archive_failure_prevents_the_callers_external_step(caller, monkeypatch):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    def broken_archive(*args):
        raise OSError('synthetic archive failure after intent')
    monkeypatch.setattr(cli, 'save_ticket', broken_archive)
    def dispatch():
        result, path, ticket = begin()
        return host(result)
    with pytest.raises(OSError, match='archive failure'):
        dispatch()
    assert calls == []
    row = scheduler.get(identity)
    assert row['dispatch_state'] == 'committing' and row['dispatch_token'] and not row['thread_id']


@pytest.mark.parametrize('host_ref', [None, 'thread:synthetic-dispatcher/turn:synthetic-turn'])
def test_caller_passes_original_ticket_and_optional_host_ref_and_replays(caller, host_ref):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, ticket = begin()
    thread = host(result)
    args = ['confirm', identity, '--thread-id', thread, '--ticket', path]
    if host_ref:
        args += ['--host-ref', host_ref]
    first = invoke(*args)
    scheduler.record_blocked(identity, thread, ['synthetic later state'])
    before = scheduler.get(identity)
    replay = invoke(*args)
    assert first['state_changed'] == 1 and replay == {**first, 'replayed': True}
    assert scheduler.get(identity) == before and calls == ['create']
    ledger = scheduler.receipts(identity)
    assert len(ledger) == 1 and ledger[0]['envelope']['host_ref'] == (host_ref or '')
    assert ticket['dispatch_token'] not in json.dumps(ledger)


def test_lost_response_only_records_interrupted_and_keeps_committing(caller):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, ticket = begin()
    before = scheduler.get(identity)
    with pytest.raises(TimeoutError):
        host(result, lose_response=True)
    receipt = invoke('interrupted', identity, '--ticket', path, '--note', 'synthetic lost response')
    assert receipt['outcome'] == 'interrupted' and receipt['state_changed'] == 0
    assert scheduler.get(identity) == before and calls == ['create']
    assert before['dispatch_state'] == 'committing'


def test_original_callback_after_revision_move_does_not_borrow_current_identity(caller):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, ticket = begin()
    thread = host(result)
    invoke('unknown', identity)  # a legacy in-flight action advances revision
    before = scheduler.get(identity)
    late = invoke('confirm', identity, '--thread-id', thread, '--ticket', path)
    assert late['receipt_accepted'] == 0 and late['reason'] == 'revision_moved'
    assert scheduler.get(identity) == before and calls == ['create']


def test_reuse_diagnostics_and_invalid_confirmation_never_clear_the_owner(caller):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, ticket = begin()
    thread = host(result)
    invoke('confirm', identity, '--thread-id', thread, '--ticket', path)
    scheduler.record_blocked(identity, thread, ['synthetic stopped writer'], owner_stopped=True)
    result, path, ticket = begin()
    assert result['dispatch_action'] == 'resume' and ticket['dispatch_action'] == 'reuse'
    before = scheduler.get(identity)
    invoke('failed', identity, '--ticket', path, '--note', 'synthetic resume failure')
    with pytest.raises(ValueError):
        invoke('confirm', identity, '--thread-id', 'different-synthetic-thread', '--ticket', path)
    assert scheduler.get(identity) == before and before['thread_id'] == thread
    confirmed = invoke('confirm', identity, '--thread-id', host(result), '--ticket', path)
    assert confirmed['state_changed'] == 1 and calls == ['create', 'resume']


@pytest.mark.parametrize('change', ['scope', 'watched', 'stopped'])
def test_caller_preserves_changed_scope_watch_or_stopped_state(caller, change):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, ticket = begin()
    thread = host(result)
    if change == 'scope':
        db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif change == 'watched':
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    else:
        db.execute("UPDATE subtitle_delivery_jobs SET dispatch_state='blocked-stopped' WHERE id=?", (identity,))
    before = scheduler.get(identity)
    receipt = invoke('confirm', identity, '--thread-id', thread, '--ticket', path)
    assert receipt['state_changed'] == 0 and receipt['reason'] in ('scope_changed', 'state_conflict', 'revision_moved')
    assert scheduler.get(identity) == before


def test_bad_callback_stays_committing_and_legacy_inflight_still_works(caller, tmp_path):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, ticket = begin()
    before = scheduler.get(identity)
    malformed = tmp_path / 'bad-ticket.json'
    malformed.write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError):
        invoke('confirm', identity, '--thread-id', 'synthetic-thread', '--ticket', str(malformed))
    assert scheduler.get(identity) == before
    legacy = invoke('confirm', identity, '--thread-id', host(result))
    assert legacy['receipt'] == 'legacy' and legacy['dispatch_state'] == 'acknowledged'
    assert scheduler.get(identity)['thread_id'] == 'synthetic-created-thread'
    assert scheduler.receipts(identity)[0]['schema'] == 'dispatch-receipt/legacy'
