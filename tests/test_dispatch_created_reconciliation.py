"""Synthetic host responses and isolated DBs; no production host or media calls."""
import copy
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from anime.dispatch_reconciliation import HOST_SCHEMA, reconcile_created, _absolute_cwd
from anime.subtitle_delivery import SubtitleDelivery
from test_subtitle_delivery import delivery, begin  # noqa: F401


def tool(body):
    return {'content': [{'type': 'text', 'text': json.dumps(body)}], 'isError': False}


def bundle(ticket, source_id, revision, root, now, thread='synthetic-chat'):
    turn = {'id': 'synthetic-turn', 'status': 'completed'}
    return {
        'schema': HOST_SCHEMA, 'checked_at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        'checked_by': 'isolated-test-operator', 'authorization_ref': 'synthetic explicit user recovery',
        'job_id': ticket['job_id'], 'input_key': ticket['input_key'], 'source_receipt_id': source_id,
        'expected_current_revision': revision, 'thread_id': thread, 'host_id': 'local',
        'dispatch_tool': 'create_thread' if ticket['dispatch_action'] == 'create' else 'send_message_to_thread',
        'original_dispatch_result': tool({'threadId': thread, 'hostId': 'local'}),
        'read_thread_result': tool({'thread': {
            'id': thread, 'hostId': 'local', 'kind': 'codex', 'cwd': str(root), 'status': {'type': 'notLoaded'},
            'preview': f"job={ticket['job_id']}，input={ticket['input_key']}。",
        }, 'turns': [turn]}),
        'wait_threads_result': tool({'polls': [{
            'thread': {'id': thread, 'hostId': 'local', 'status': {'type': 'notLoaded'}},
            'latestTurn': turn, 'latestToolMarker': {'id': 'synthetic-tool', 'turnId': turn['id'], 'status': 'completed'},
        }]}),
    }


@pytest.fixture
def pending(delivery, tmp_path):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    # Simulate a projection refresh between the original begin and callback.
    db.execute('UPDATE subtitle_delivery_jobs SET revision=revision+1 WHERE id=?', (identity,))
    ticket = intent['ticket']
    receipt = scheduler.record_dispatch_receipt(ticket, 'created', thread_id='synthetic-chat')
    assert receipt['reason'] == 'revision_moved'
    revision = scheduler.get(identity)['revision']
    value = bundle(ticket, receipt['receipt_id'], revision, db.path.parent.parent, now[0])
    path = tmp_path/'host-evidence.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return scheduler, ticket, receipt, revision, path, value, now


def apply(pending, **override):
    scheduler, ticket, source, revision, path, value, now = pending
    args = dict(delivery=scheduler, ticket=ticket, source_receipt_id=source['receipt_id'],
                expected_revision=revision, thread_id='synthetic-chat', host_evidence_path=path)
    args.update(override)
    return reconcile_created(**args)


def tables(db):
    return {t: db.rows('SELECT * FROM '+t) for t in ('subtitle_delivery_jobs', 'subtitle_delivery_events', 'dispatch_receipts', 'watches', 'episodes')}


def test_reconcile_original_fact_once_without_rewriting_it_and_replay_after_stop(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    original = copy.deepcopy(scheduler.receipts(ticket['job_id']))
    first = apply(pending)
    assert (first['receipt_accepted'], first['state_changed'], first['reason']) == (1, 1, '')
    assert first['schema'] == 'dispatch-reconciliation/v1'
    assert first['resulting_revision'] == revision + 1
    assert first['job']['thread_id'] == 'synthetic-chat'
    assert first['job']['dispatch_state'] == 'acknowledged'
    assert scheduler.receipts(ticket['job_id'])[:len(original)] == original
    scheduler.record_blocked(ticket['job_id'], 'synthetic-chat', ['synthetic stopped'], owner_stopped=True)
    now[0] += 3600
    before = tables(scheduler.db)
    assert apply(pending) == {**first, 'replayed': True}
    assert tables(scheduler.db) == before
    encoded = json.dumps(scheduler.receipts(ticket['job_id']))
    assert ticket['dispatch_token'] not in encoded and ticket['lease_token'] not in encoded


@pytest.mark.parametrize('sql,reason', [
    ("UPDATE subtitle_delivery_jobs SET revision=revision+1", 'revision_moved'),
    ("UPDATE subtitle_delivery_jobs SET input_key='new'", 'input_moved'),
    ("UPDATE subtitle_delivery_jobs SET dispatch_token='new'", 'dispatch_mismatch'),
    ("UPDATE subtitle_delivery_jobs SET lease_owner='new'", 'owner_mismatch'),
    ("UPDATE subtitle_delivery_jobs SET lease_token='new'", 'owner_mismatch'),
    ("UPDATE subtitle_delivery_jobs SET thread_id='another'", 'state_conflict'),
    ("UPDATE subtitle_delivery_jobs SET dispatch_state='blocked-stopped'", 'state_conflict'),
    ("UPDATE subtitle_delivery_jobs SET state='finished'", 'state_conflict'),
    ("UPDATE shows SET selected=0 WHERE id=42", 'scope_changed'),
    ("UPDATE shows SET cleanup_hold=1 WHERE id=42", 'scope_changed'),
    ("UPDATE shows SET authorized=0 WHERE id=42", 'scope_changed'),
    ("UPDATE source_policies SET config='{}' WHERE show_id=42", 'scope_changed'),
    ("INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)", 'scope_changed'),
])
def test_cas_and_current_protections_preserve_all_jobs_media_watches(pending, sql, reason):
    scheduler, ticket, source, revision, path, value, now = pending
    scheduler.db.execute(sql)
    before = tables(scheduler.db)
    result = apply(pending)
    assert result['reason'] == reason and result['state_changed'] == 0
    after = tables(scheduler.db)
    for key in ('subtitle_delivery_jobs', 'watches', 'episodes'):
        assert after[key] == before[key]


def test_explicit_stopped_payload_and_other_active_owner_are_protected(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    job = scheduler.get(ticket['job_id'])
    payload = {**job['payload'], 'owner_stopped_writing': True}
    scheduler.db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?', (json.dumps(payload), ticket['job_id']))
    assert apply(pending)['reason'] == 'state_conflict'


def test_another_owner_cannot_be_preempted(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    scheduler.db.execute("UPDATE subtitle_delivery_jobs SET dispatch_state='acknowledged',thread_id='other-chat' WHERE id='episode:43:1'")
    assert apply(pending)['reason'] == 'other_owner_active'


@pytest.mark.parametrize('field', ['issued_at', 'thread_id', 'dispatch_token', 'lease_token', 'owner', 'input_key'])
def test_edited_original_ticket_never_borrows_current_credentials(pending, field):
    scheduler, ticket, source, revision, path, value, now = pending
    bad = dict(ticket)
    bad[field] = 0 if field == 'issued_at' else 'edited'
    before = tables(scheduler.db)
    with pytest.raises(ValueError):
        apply(pending, ticket=bad)
    assert tables(scheduler.db) == before


@pytest.mark.parametrize('mutation', ['active', 'wrong_thread', 'running_tool', 'input', 'project', 'error', 'stale', 'future', 'boolean_revision', 'token'])
def test_host_evidence_rejects_stale_unbound_or_active_facts(pending, mutation):
    scheduler, ticket, source, revision, path, value, now = pending
    if mutation in ('active', 'wrong_thread', 'running_tool', 'error'):
        waited = json.loads(value['wait_threads_result']['content'][0]['text'])
        if mutation == 'active': waited['polls'][0]['thread']['status']['type'] = 'active'
        if mutation == 'wrong_thread': waited['polls'][0]['thread']['id'] = 'another'
        if mutation == 'running_tool': waited['polls'][0]['latestToolMarker']['status'] = 'inProgress'
        if mutation == 'error': waited['errors'] = [{'message': 'unavailable'}]
        value['wait_threads_result'] = tool(waited)
    elif mutation in ('input', 'project'):
        read = json.loads(value['read_thread_result']['content'][0]['text'])
        read['thread']['preview' if mutation == 'input' else 'cwd'] = 'different'
        value['read_thread_result'] = tool(read)
    elif mutation == 'stale': now[0] += 301
    elif mutation == 'future': now[0] -= 31
    elif mutation == 'boolean_revision': value['expected_current_revision'] = True
    elif mutation == 'token': value['unexpected'] = ticket['dispatch_token']
    path.write_text(json.dumps(value), encoding='utf-8')
    before = tables(scheduler.db)
    with pytest.raises(ValueError): apply(pending)
    assert tables(scheduler.db) == before


def test_conflicting_created_fact_is_not_resolved_by_picking_one(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    scheduler.record_dispatch_receipt(ticket, 'created', thread_id='different-chat')
    before = tables(scheduler.db)
    with pytest.raises(ValueError, match='Conflicting'): apply(pending)
    assert tables(scheduler.db) == before


@pytest.mark.parametrize('failure', ['event', 'receipt'])
def test_failed_event_or_receipt_commit_rolls_back_the_job(pending, monkeypatch, failure):
    scheduler, ticket, source, revision, path, value, now = pending
    if failure == 'event':
        def fail(*args): raise RuntimeError('synthetic event failure')
        monkeypatch.setattr(scheduler, '_event', fail)
    else:
        scheduler.db.execute("CREATE TRIGGER fail_reconciliation BEFORE INSERT ON dispatch_receipts WHEN NEW.schema='dispatch-reconciliation/v1' BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END")
    before = tables(scheduler.db)
    with pytest.raises(Exception, match='synthetic'): apply(pending)
    assert tables(scheduler.db) == before


def test_concurrent_identical_requests_commit_once(pending):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(pending), range(2)))
    assert sum(bool(r.get('replayed')) for r in results) == 1
    scheduler, ticket, source, revision, path, value, now = pending
    assert scheduler.get(ticket['job_id'])['revision'] == revision + 1
    assert len([r for r in scheduler.receipts(ticket['job_id']) if r['schema'] == 'dispatch-reconciliation/v1']) == 1


def test_scope_change_during_host_evidence_read_is_rechecked_inside_transaction(pending, monkeypatch):
    import anime.dispatch_reconciliation as module
    scheduler = pending[0]
    original = module._host_observation
    def changed(*args):
        result = original(*args)
        scheduler.db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
        return result
    monkeypatch.setattr(module, '_host_observation', changed)
    before = scheduler.get(pending[1]['job_id'])
    result = apply(pending)
    assert result['reason'] == 'scope_changed' and result['state_changed'] == 0
    assert scheduler.get(pending[1]['job_id']) == before


def test_real_cli_process_uses_only_the_isolated_database(pending, tmp_path):
    scheduler, ticket, source, revision, path, value, now = pending
    value['checked_at'] = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc).isoformat()
    path.write_text(json.dumps(value), encoding='utf-8')
    ticket_path = tmp_path/'cli-ticket.json'
    ticket_path.write_text(json.dumps(ticket), encoding='utf-8')
    proc = subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/subtitle_delivery_queue.py'),
        '--db', str(scheduler.db.path), 'confirm', ticket['job_id'], '--thread-id', 'synthetic-chat',
        '--ticket', str(ticket_path), '--reconcile-receipt', source['receipt_id'],
        '--expected-revision', str(revision), '--host-evidence', str(path),
    ], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    result = json.loads(proc.stdout)
    assert result['state_changed'] == 1 and result['source_receipt_id'] == source['receipt_id']
    assert scheduler.get(ticket['job_id'])['thread_id'] == 'synthetic-chat'
    assert ticket['dispatch_token'].encode() not in proc.stdout + proc.stderr


def test_cli_existing_confirm_requires_explicit_bundle_and_uses_original_ticket(pending, monkeypatch, capsys, tmp_path):
    scheduler, ticket, source, revision, path, value, now = pending
    spec = importlib.util.spec_from_file_location('reconciliation_cli', Path(__file__).resolve().parents[1]/'scripts/subtitle_delivery_queue.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    ticket_path = tmp_path/'original-ticket.json'
    ticket_path.write_text(json.dumps(ticket), encoding='utf-8')
    monkeypatch.setattr(cli, 'SubtitleDelivery', lambda *args, **kwargs: scheduler)
    args = ['queue', '--db', str(scheduler.db.path), 'confirm', ticket['job_id'], '--thread-id', 'synthetic-chat',
            '--ticket', str(ticket_path), '--reconcile-receipt', source['receipt_id'], '--expected-revision', str(revision)]
    before = tables(scheduler.db)
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(ValueError, match='Reconciliation requires'): cli.main()
    assert tables(scheduler.db) == before
    monkeypatch.setattr(sys, 'argv', args + ['--host-evidence', str(path)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out['state_changed'] == 1 and out['job']['thread_id'] == 'synthetic-chat'
    assert json.loads(ticket_path.read_text()) == ticket
    assert ticket['dispatch_token'] not in json.dumps(out)


@pytest.mark.parametrize('cwd', ['missing', None, '', ' ', '.', 'relative', './nested', True])
def test_project_binding_never_uses_the_callers_directory(pending, monkeypatch, cwd):
    scheduler, ticket, source, revision, path, value, now = pending
    read = json.loads(value['read_thread_result']['content'][0]['text'])
    if cwd == 'missing':
        del read['thread']['cwd']
    else:
        read['thread']['cwd'] = cwd
    value['read_thread_result'] = tool(read)
    path.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.chdir(scheduler.db.path.parent.parent)
    before = tables(scheduler.db)
    with pytest.raises(ValueError, match='Explicit absolute'):
        apply(pending)
    assert tables(scheduler.db) == before


@pytest.mark.parametrize('spelling', ['extended', 'mixed_case', 'forward_slashes', 'extended_forward_slashes'])
@pytest.mark.skipif(os.name != 'nt', reason='Native Windows path identity')
def test_absolute_windows_project_spellings_preserve_original_host_response(pending, spelling):
    scheduler, ticket, source, revision, path, value, now = pending
    original = str(scheduler.db.path.parent.parent)
    cwd = {'extended': '\\\\?\\' + original,
           'mixed_case': original.swapcase(),
           'forward_slashes': original.replace('\\', '/'),
           'extended_forward_slashes': '//?/' + original.replace('\\', '/')}[spelling]
    read = json.loads(value['read_thread_result']['content'][0]['text'])
    read['thread']['cwd'] = cwd
    value['read_thread_result'] = tool(read)
    path.write_text(json.dumps(value), encoding='utf-8')
    archived = path.read_bytes()
    assert apply(pending)['state_changed'] == 1
    assert path.read_bytes() == archived


@pytest.mark.parametrize('value,expected', [
    ('\\\\?\\UNC\\synthetic-host\\synthetic-share\\project', '\\\\synthetic-host\\synthetic-share\\project'),
    ('//?/unc/synthetic-host/synthetic-share/project', '\\\\synthetic-host\\synthetic-share\\project'),
    ('\\\\?\\C:\\synthetic\\project', 'C:\\synthetic\\project'),
    ('C:/synthetic/project', 'C:\\synthetic\\project'),
])
def test_windows_prefix_normalization_is_lexical_without_network_access(value, expected):
    assert _absolute_cwd(value, windows=True) == expected


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows path identity')
def test_extended_unc_and_case_share_the_same_resolved_comparison_without_network(monkeypatch):
    from anime.dispatch_reconciliation import _project_directory
    monkeypatch.setattr(Path, 'resolve', lambda self: self)
    assert _project_directory('\\\\?\\UNC\\Synthetic-Host\\Share\\Project') == _project_directory('\\\\synthetic-host\\share\\project')


@pytest.mark.parametrize('value', [None, '', ' ', '.', './project', 'project', 'C:project', '\\project',
                                  '\\\\?\\C:project', '\\\\?\\UNC\\host', True])
def test_windows_absolute_validation_rejects_implicit_or_incomplete_roots(value):
    with pytest.raises(ValueError, match='Explicit absolute'):
        _absolute_cwd(value, windows=True)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows path identity')
def test_extended_absolute_other_project_still_rejected_without_any_writes(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    read = json.loads(value['read_thread_result']['content'][0]['text'])
    read['thread']['cwd'] = '\\\\?\\' + str(scheduler.db.path.parent.parent / 'another-project')
    value['read_thread_result'] = tool(read)
    path.write_text(json.dumps(value), encoding='utf-8')
    before = tables(scheduler.db)
    with pytest.raises(ValueError, match='another project'):
        apply(pending)
    assert tables(scheduler.db) == before


def encoded_token(token, form):
    if form == 'plain':
        return token
    value = ''.join('\\u%04x' % ord(c) for c in token[:8]) + token[8:]
    return value if form == 'unicode' else value.replace('\\', '\\\\')


@pytest.mark.parametrize('authority', ['dispatch_token', 'lease_token'])
@pytest.mark.parametrize('field', ['checked_by', 'authorization_ref', 'filename', 'recorded_by',
                                  'native_result', 'extra_value', 'extra_key', 'job_payload'])
@pytest.mark.parametrize('form', ['plain', 'unicode', 'nested_escape'])
def test_authority_never_enters_metadata_or_readback(pending, authority, field, form):
    scheduler, ticket, source, revision, path, value, now = pending
    token = ticket[authority]
    encoded = encoded_token(token, form)
    override = {}
    if field in ('checked_by', 'authorization_ref'):
        value[field] = encoded
    elif field == 'filename':
        # It need not exist: rejection must happen before opening/echoing it.
        override['host_evidence_path'] = path.parent / ('evidence-' + encoded + '.json')
    elif field == 'recorded_by':
        override['recorded_by'] = encoded
    elif field == 'native_result':
        read = json.loads(value['read_thread_result']['content'][0]['text'])
        read['thread']['private_note'] = encoded
        value['read_thread_result'] = tool(read)
    elif field == 'extra_value':
        value['extra'] = [{'note': encoded}]
    elif field == 'extra_key':
        value[encoded] = 'synthetic annotation'
    elif field == 'job_payload':
        payload = scheduler.get(ticket['job_id'])['payload']
        payload['synthetic_metadata'] = encoded
        scheduler.db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?',
                             (json.dumps(payload), ticket['job_id']))
    path.write_text(json.dumps(value), encoding='utf-8')
    before = tables(scheduler.db)
    with pytest.raises(ValueError, match='must not contain authority') as error:
        apply(pending, **override)
    assert token not in str(error.value) and encoded not in str(error.value)
    assert tables(scheduler.db) == before


@pytest.mark.parametrize('authority', ['dispatch_token', 'lease_token'])
def test_wire_unicode_token_is_rejected_without_receipt(pending, authority):
    scheduler, ticket, source, revision, path, value, now = pending
    token = ticket[authority]
    value['checked_by'] = token
    raw = json.dumps(value).replace(token, ''.join('\\u%04x' % ord(c) for c in token))
    assert token not in raw
    path.write_text(raw, encoding='utf-8')
    before = tables(scheduler.db)
    with pytest.raises(ValueError, match='must not contain authority'):
        apply(pending)
    assert tables(scheduler.db) == before


def test_excessive_escape_depth_is_rejected_without_mutation(pending):
    scheduler, ticket, source, revision, path, value, now = pending
    encoded = encoded_token(ticket['dispatch_token'], 'unicode')
    for _ in range(9):
        encoded = encoded.replace('\\', '\\\\')
    value['extra'] = encoded
    path.write_text(json.dumps(value), encoding='utf-8')
    before = tables(scheduler.db)
    with pytest.raises(ValueError, match='encoding depth'):
        apply(pending)
    assert tables(scheduler.db) == before
