"""dispatch-receipt/v1 (docs/DISPATCH-RECEIPT-CONTRACT.md): temporary DBs, fake clock, synthetic tickets."""
import json
import sys

import pytest

from anime.subtitle_delivery import SubtitleDelivery, RECEIPT_REQUIRED_KEY, validate_ticket
from test_subtitle_delivery import delivery as base_delivery, begin  # noqa: F401  (fixture and helper)


@pytest.fixture
def delivery(base_delivery):
    db, scheduler, ready, now = base_delivery
    # A synthetic local host adapter, not a JSON assertion accepted by production.
    scheduler.absence_verifier = lambda ticket, evidence, host_ref: evidence.get('terminal_operation') == 'synthetic-terminal-absent'
    return base_delivery


def events(db, identity, kind):
    return [json.loads(r['payload']) for r in db.rows('SELECT payload FROM subtitle_delivery_events WHERE job_id=? AND kind=? ORDER BY id', (identity, kind))]


def stamp(seconds):
    import datetime
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def absence(ticket, checked_at=None, **extra):
    return {'checked_at': checked_at or stamp(ticket['issued_at'] + 5), 'method': 'chat list read-only', 'checked_by': 'operator fixture',
            'job_id': ticket['job_id'], 'dispatch_token': ticket['dispatch_token'], 'file': 'absence.json', 'sha256': 'a' * 64, 'terminal_operation': 'synthetic-terminal-absent', **extra}


def test_created_receipt_applies_once_and_replays_the_first_result_after_the_job_moved(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    assert validate_ticket(ticket) is ticket and ticket['dispatch_action'] == 'create' and ticket['revision'] == intent['revision']
    first = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1', host_ref='op-77', recorded_by='test')
    assert first['receipt_accepted'] == 1 and first['state_changed'] == 1 and first['reason'] == ''
    assert first['resulting_dispatch_state'] == 'acknowledged' and first['resulting_revision'] == ticket['revision'] + 1
    assert 'dispatch_token' not in first['job'] and 'lease_token' not in first['job']
    assert events(db, identity, 'dispatch-reconciled')[-1] == {'result': 'acknowledged', 'thread_id': 'chat-1', 'receipt_id': first['receipt_id']}
    scheduler.record_blocked(identity, 'chat-1', ['waiting for audio'])  # the job advances
    again = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1', host_ref='op-77', recorded_by='someone else')
    assert again == {**first, 'replayed': True}  # exact first result, not re-evaluated against the new revision
    ledger = scheduler.receipts(identity)
    assert [r['schema'] for r in ledger] == ['dispatch-receipt/v1'] and ledger[0]['envelope']['dispatch_digest'] != ticket['dispatch_token']
    assert ticket['dispatch_token'] not in json.dumps(ledger)


def test_binding_failures_are_not_accepted_and_change_nothing(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    before = scheduler.get(identity)
    cases = {'dispatch_mismatch': {**ticket, 'dispatch_token': 'stale-token'},
             'input_moved': {**ticket, 'input_key': 'another-input'},
             'owner_mismatch': {**ticket, 'owner': 'someone-else'}}
    for reason, bad in cases.items():
        r = scheduler.record_dispatch_receipt(bad, 'created', thread_id='chat-1')
        assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (0, 0, reason), reason
    assert scheduler.get(identity) == before
    applied = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    assert applied['state_changed'] == 1
    late = scheduler.record_dispatch_receipt(ticket, 'failed', evidence={'note': 'arrived after the acknowledgement'})
    assert (late['receipt_accepted'], late['state_changed'], late['reason']) == (0, 0, 'revision_moved')
    assert scheduler.get(identity)['dispatch_state'] == 'acknowledged'
    assert len(scheduler.receipts(identity)) == 5 and sum(r['receipt_accepted'] for r in scheduler.receipts(identity)) == 1


def test_concurrent_different_outcomes_keep_only_the_first_and_record_the_other(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    created = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    absent = scheduler.record_dispatch_receipt(ticket, 'not_created', evidence=absence(ticket))
    assert created['state_changed'] == 1 and (absent['receipt_accepted'], absent['state_changed'], absent['reason']) == (0, 0, 'revision_moved')
    assert scheduler.get(identity)['thread_id'] == 'chat-1'


def test_true_facts_that_cannot_be_applied_are_kept_without_state_change(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    db.execute('UPDATE shows SET selected=0 WHERE id=42')
    r = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (1, 0, 'scope_changed')
    assert scheduler.get(identity)['dispatch_state'] == 'committing' and scheduler.get(identity)['thread_id'] == ''
    db.execute('UPDATE shows SET selected=1 WHERE id=42')
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    r = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1', host_ref='second attempt')
    assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (1, 0, 'scope_changed')
    db.execute('DELETE FROM watches WHERE show_id=42 AND episode=1')
    r = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1', host_ref='third attempt')
    assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (1, 1, '')
    assert [x['envelope']['thread_id'] for x in scheduler.receipts(identity)] == ['chat-1'] * 3


def test_not_created_never_clears_a_responsible_thread_and_resume_tickets_confirm_only_their_thread(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='chat-1')
    scheduler.record_blocked(identity, 'chat-1', ['owner paused'], owner_stopped=True)
    row = scheduler.get(identity)
    lease = scheduler.resume(identity, 'chat-1', 'test-owner', row['revision'])
    resumed = scheduler.begin_dispatch(identity, 'test-owner', lease['lease_token'], lease['revision'])
    ticket = resumed['ticket']
    assert ticket['dispatch_action'] == 'reuse' and ticket['thread_id'] == 'chat-1'
    with pytest.raises(ValueError):
        scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-2')
    r = scheduler.record_dispatch_receipt(ticket, 'not_created', evidence=absence(ticket))
    assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (1, 0, 'state_conflict')
    assert scheduler.get(identity)['thread_id'] == 'chat-1' and scheduler.get(identity)['dispatch_state'] == 'committing'
    r = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    assert (r['state_changed'], r['resulting_dispatch_state']) == (1, 'acknowledged')


def test_a_legacy_unknown_step_moves_the_revision_so_the_old_ticket_no_longer_binds(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    scheduler.reconcile_dispatch(identity, ticket['dispatch_token'])  # legacy lost-response bookkeeping advances the revision
    r = scheduler.record_dispatch_receipt(ticket, 'not_created', evidence=absence(ticket))
    assert (r['receipt_accepted'], r['reason']) == (0, 'revision_moved')  # explicit re-verification, no borrowed credentials


def test_fresh_not_created_applies_and_strips_the_raw_token_from_the_ledger(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    r = scheduler.record_dispatch_receipt(ticket, 'not_created', evidence=absence(ticket, extra_note='x'))
    assert (r['receipt_accepted'], r['state_changed'], r['resulting_state'], r['resulting_dispatch_state']) == (1, 1, 'ready', 'not-created')
    stored = scheduler.receipts(identity)[0]['envelope']['evidence']
    assert 'dispatch_token' not in stored and stored['dispatch_digest'] and stored['extra_note'] == 'x'
    assert events(db, identity, 'dispatch-reconciled')[-1]['absence_evidence']['checked_by'] == 'operator fixture'


def test_lease_expiry_after_a_real_create_does_not_discard_the_acknowledgement(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    now[0] += 3600  # the lease is long gone
    r = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='chat-1')
    assert r['state_changed'] == 1 and scheduler.get(identity)['thread_id'] == 'chat-1'


def test_failed_and_interrupted_are_diagnostics_only(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    before = scheduler.get(identity)
    for outcome in ('failed', 'interrupted'):
        r = scheduler.record_dispatch_receipt(ticket, outcome, evidence={'note': outcome})
        assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (1, 0, '')
    assert scheduler.get(identity) == before  # no release, no retry, no owner change
    assert [e['outcome'] for e in events(db, identity, 'dispatch-receipt')] == ['failed', 'interrupted']
    with pytest.raises(ValueError):
        scheduler.record_dispatch_receipt(ticket, 'failed', thread_id='chat-1')  # only created names a thread


def test_reused_receipt_id_with_different_content_is_rejected_and_recorded(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    rid = 'f' * 64
    r = scheduler.record_dispatch_receipt(ticket, 'failed', evidence={'note': 'one'}, receipt_id=rid)
    assert r['receipt_id'] == rid
    with pytest.raises(ValueError):
        scheduler.record_dispatch_receipt(ticket, 'failed', evidence={'note': 'two'}, receipt_id=rid)
    assert events(db, identity, 'dispatch-receipt-rejected') == [{'receipt_id': rid, 'reason': 'id_reused'}]
    assert scheduler.record_dispatch_receipt(ticket, 'failed', evidence={'note': 'one'}, receipt_id=rid)['replayed'] is True
    with pytest.raises(ValueError):
        scheduler.record_dispatch_receipt(ticket, 'failed', receipt_id='not-a-digest')


def test_interruption_before_commit_leaves_nothing_and_replay_after_restart_returns_the_stored_result(delivery, monkeypatch):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    original = scheduler._event
    def crash(c, job, kind, payload):
        if kind == 'dispatch-reconciled':
            raise RuntimeError('process died before commit')
        return original(c, job, kind, payload)
    monkeypatch.setattr(scheduler, '_event', crash)
    with pytest.raises(RuntimeError):
        scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    assert scheduler.receipts(identity) == [] and scheduler.get(identity)['dispatch_state'] == 'committing'
    monkeypatch.setattr(scheduler, '_event', original)
    first = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    restarted = SubtitleDelivery(db, ready, lambda: now[0])
    assert restarted.record_dispatch_receipt(ticket, 'created', thread_id='chat-1') == {**first, 'replayed': True}


def test_malformed_tickets_and_evidence_are_errors_not_receipts(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    bad_tickets = [{**ticket, 'schema': 'other'}, {**ticket, 'revision': '3'}, {**ticket, 'dispatch_action': 'reuse'},
                   {**ticket, 'issued_at': 'yesterday'}, {k: v for k, v in ticket.items() if k != 'owner'}]
    for bad in bad_tickets:
        with pytest.raises(ValueError):
            scheduler.record_dispatch_receipt(bad, 'created', thread_id='chat-1')
    bad_evidence = [absence(ticket, checked_at='2026-10-18 12:00'), absence(ticket, checked_at=stamp(ticket['issued_at'] - 1)),
                    absence(ticket, checked_at=stamp(now[0] + 3600)), {**absence(ticket), 'dispatch_token': 'other'},
                    {**absence(ticket), 'job_id': 'episode:1:1'}, {k: v for k, v in absence(ticket).items() if k != 'sha256'},
                    absence(ticket, padding='x' * 20000)]
    for bad in bad_evidence:
        with pytest.raises(ValueError):
            scheduler.record_dispatch_receipt(ticket, 'not_created', evidence=bad)
    with pytest.raises(ValueError):
        scheduler.record_dispatch_receipt(ticket, 'created', thread_id='')
    with pytest.raises(ValueError):
        scheduler.record_dispatch_receipt(ticket, 'done')
    assert scheduler.receipts(identity) == [] and scheduler.get(identity)['dispatch_state'] == 'committing'


def test_legacy_callers_keep_working_with_provenance_until_enforcement_is_switched_on(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    legacy = scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='chat-legacy')
    assert legacy['dispatch_state'] == 'acknowledged'
    ledger = scheduler.receipts(identity)
    assert [(r['schema'], r['outcome'], r['reason']) for r in ledger] == [('dispatch-receipt/legacy', 'created', 'legacy')]
    assert intent['dispatch_token'] not in json.dumps(ledger)
    scheduler.record_blocked(identity, 'chat-legacy', ['owner paused'], owner_stopped=True)  # frees the single production slot
    db.set(RECEIPT_REQUIRED_KEY, True)
    identity2, intent2 = begin(scheduler, 43, 1)
    with pytest.raises(ValueError):
        scheduler.reconcile_dispatch(identity2, intent2['dispatch_token'], thread_id='chat-2')
    assert scheduler.record_dispatch_receipt(intent2['ticket'], 'created', thread_id='chat-2')['state_changed'] == 1


def test_cli_begin_archives_the_ticket_and_v1_commands_use_it(delivery, tmp_path, monkeypatch, capsys):
    db, scheduler, ready, now = delivery
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('queue_cli', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
    queue_cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(queue_cli)
    identity = scheduler.episode_id(42, 1); row = scheduler.get(identity)
    monkeypatch.setattr(queue_cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(queue_cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    dbfile = tmp_path / 'db' / 'anime.db'; dbfile.parent.mkdir(); dbfile.write_bytes(b'')
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(dbfile), 'begin', identity, '--owner', 'cli-owner', '--revision', str(row['revision'])])
    queue_cli.main()
    out = json.loads(capsys.readouterr().out)
    ticket_path = Path(out['ticket_path'])
    assert ticket_path.parent == dbfile.parent / 'subtitle-delivery-tickets' and ticket_path.is_file()
    assert 'dispatch_token' not in json.dumps(out) and 'lease_token' not in json.dumps(out)
    ticket = json.loads(ticket_path.read_text(encoding='utf-8'))
    assert ticket['dispatch_token'] and ticket['job_id'] == identity
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(dbfile), 'failed', identity, '--ticket', str(ticket_path), '--note', 'tool crashed'])
    queue_cli.main()
    failed = json.loads(capsys.readouterr().out)
    assert failed['outcome'] == 'failed' and failed['state_changed'] == 0 and failed['receipt_accepted'] == 1
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(dbfile), 'confirm', identity, '--thread-id', 'chat-cli', '--ticket', str(ticket_path), '--host-ref', 'synthetic-operation-1'])
    queue_cli.main()
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed['schema'] == 'dispatch-receipt/v1' and confirmed['state_changed'] == 1 and confirmed['job']['thread_id'] == 'chat-cli'
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'receipts', identity])  # a real read-only store on the fixture file
    queue_cli.main()
    ledger = json.loads(capsys.readouterr().out)
    assert [r['outcome'] for r in ledger] == ['failed', 'created'] and ticket['dispatch_token'] not in json.dumps(ledger)
    assert ledger[1]['envelope']['host_ref'] == 'synthetic-operation-1'


def test_cli_not_created_with_ticket_binds_the_evidence_file(delivery, tmp_path, monkeypatch, capsys):
    db, scheduler, ready, now = delivery
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('queue_cli', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
    queue_cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(queue_cli)
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    tpath = tmp_path / 'ticket.json'; tpath.write_text(json.dumps(ticket), encoding='utf-8')
    evidence = tmp_path / 'absence.json'
    evidence.write_text(json.dumps({'checked_at': stamp(ticket['issued_at'] + 5), 'method': 'chat list read-only', 'checked_by': 'operator',
                                    'job_id': identity, 'dispatch_token': ticket['dispatch_token'], 'sha256': 'spoofed', 'terminal_operation': 'synthetic-terminal-absent'}), encoding='utf-8')
    monkeypatch.setattr(queue_cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(queue_cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(tmp_path / 'anime.db'), 'not-created', identity, '--ticket', str(tpath), '--evidence', str(evidence)])
    queue_cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out['state_changed'] == 1 and out['resulting_dispatch_state'] == 'not-created'
    stored = scheduler.receipts(identity)[0]['envelope']['evidence']
    import hashlib
    assert stored['sha256'] == hashlib.sha256(evidence.read_bytes()).hexdigest() and stored['file'] == 'absence.json'  # tool-computed, not spoofed
    assert 'dispatch_token' not in stored


def test_production_default_rejects_absence_assertions_without_a_trusted_host_verifier(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    default = SubtitleDelivery(db, ready, lambda: now[0])
    before = default.get(identity)
    r = default.record_dispatch_receipt(intent['ticket'], 'not_created', evidence=absence(intent['ticket']))
    assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (0, 0, 'evidence_invalid')
    assert default.get(identity) == before and default.get(identity)['dispatch_state'] == 'committing'
    scheduler.absence_verifier = lambda *args: (_ for _ in ()).throw(RuntimeError('host unavailable'))
    r = scheduler.record_dispatch_receipt(intent['ticket'], 'not_created', evidence=absence(intent['ticket'], note='host down'))
    assert r['reason'] == 'evidence_invalid' and scheduler.get(identity) == before


def test_server_digest_binds_ticket_issue_time_action_and_extra_fields(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    before = scheduler.get(identity)
    for bad in ({**ticket, 'issued_at': ticket['issued_at'] - 100},
                {**ticket, 'dispatch_action': 'reuse', 'thread_id': 'chat-1'},
                {**ticket, 'extension': 'changed'}):
        r = scheduler.record_dispatch_receipt(bad, 'created', thread_id='chat-1')
        assert (r['receipt_accepted'], r['state_changed'], r['reason']) == (0, 0, 'evidence_invalid')
    assert scheduler.get(identity) == before
    first = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')
    assert first['state_changed'] == 1
    assert scheduler.record_dispatch_receipt(ticket, 'created', thread_id='chat-1')['replayed']


def test_real_concurrent_completion_serializes_to_one_state_change(delivery):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    barrier = Barrier(2)
    def submit(outcome):
        barrier.wait(timeout=5)
        return scheduler.record_dispatch_receipt(intent['ticket'], outcome,
            **({'thread_id': 'chat-1'} if outcome == 'created' else {'evidence': absence(intent['ticket'])}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ['created', 'not_created']))
    assert sum(r['state_changed'] for r in results) == 1
    assert sorted(r['reason'] for r in results) == ['', 'revision_moved']
    assert len(scheduler.receipts(identity)) == 2
    assert len(events(db, identity, 'dispatch-reconciled')) == 1


def test_concurrent_id_reuse_records_its_rejection_even_after_precheck_race(delivery, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    original = db.one
    barrier = Barrier(2)
    def synchronized(sql, args=()):
        result = original(sql, args)
        if sql.startswith('SELECT request_digest FROM dispatch_receipts'):
            barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(db, 'one', synchronized)
    def submit(note):
        try:
            return scheduler.record_dispatch_receipt(intent['ticket'], 'failed', evidence={'note': note}, receipt_id='f' * 64)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ['first', 'second']))
    assert sum(r is not None for r in results) == 1
    assert len(scheduler.receipts(identity)) == 1
    assert events(db, identity, 'dispatch-receipt-rejected') == [{'receipt_id': 'f' * 64, 'reason': 'id_reused'}]


def test_nested_authority_tokens_are_not_persisted_in_diagnostic_evidence(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    for secret in (intent['ticket']['dispatch_token'], intent['ticket']['lease_token']):
        with pytest.raises(ValueError, match='raw authority tokens'):
            scheduler.record_dispatch_receipt(intent['ticket'], 'interrupted', evidence={'nested': {'note': secret}})
    assert scheduler.receipts(identity) == []


def test_cli_unknown_ticket_is_diagnostic_and_no_ticket_remains_legacy(delivery, tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    path = tmp_path / 'ticket.json'; path.write_text(json.dumps(intent['ticket']), encoding='utf-8')
    spec = importlib.util.spec_from_file_location('queue_unknown_cli', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    before = scheduler.get(identity)
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'unknown', identity, '--ticket', str(path)])
    cli.main(); result = json.loads(capsys.readouterr().out)
    assert result['outcome'] == 'interrupted' and result['state_changed'] == 0
    assert scheduler.get(identity) == before
    db.set(RECEIPT_REQUIRED_KEY, True)
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'unknown', identity])
    with pytest.raises(ValueError, match='ticket required'):
        cli.main()
    db.set(RECEIPT_REQUIRED_KEY, False)
    cli.main(); result = json.loads(capsys.readouterr().out)
    assert result['receipt'] == 'legacy' and result['dispatch_state'] == 'unknown'


def test_ticket_archive_is_exclusive_and_does_not_accept_job_paths(delivery, tmp_path):
    import importlib.util
    from pathlib import Path
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    spec = importlib.util.spec_from_file_location('queue_save_cli', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    result = {**intent, 'id': '../../escape'}
    path = Path(cli.save_ticket(tmp_path / 'anime.db', result))
    assert path.resolve().parent == (tmp_path / 'subtitle-delivery-tickets').resolve()
    content = path.read_bytes()
    with pytest.raises(FileExistsError):
        cli.save_ticket(tmp_path / 'anime.db', result)
    assert path.read_bytes() == content


def test_ticket_acl_failure_never_writes_a_ticket_or_releases_committing(delivery, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    spec = importlib.util.spec_from_file_location('queue_acl_cli', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    def denied(path):
        raise ValueError('cannot secure the ticket directory')
    monkeypatch.setattr(cli, 'protect_ticket_directory', denied)
    with pytest.raises(ValueError, match='cannot secure'):
        cli.save_ticket(tmp_path / 'anime.db', intent)
    assert list((tmp_path / 'subtitle-delivery-tickets').iterdir()) == []
    assert scheduler.get(identity)['dispatch_state'] == 'committing'


def test_ledger_preserves_recording_order_when_receipt_times_are_equal(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.record_dispatch_receipt(intent['ticket'], 'failed', receipt_id='f' * 64)
    scheduler.record_dispatch_receipt(intent['ticket'], 'interrupted', receipt_id='a' * 64)
    ledger = scheduler.receipts(identity)
    assert [r['outcome'] for r in ledger] == ['failed', 'interrupted']
    assert ledger[0]['recorded_at'] == ledger[1]['recorded_at']
