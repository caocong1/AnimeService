"""Future begin-only calls use synthetic authority and temporary databases only."""
import copy
import json
from pathlib import Path

import pytest

from anime.dispatch_caller import DispatchFailure
from scripts import subtitle_begin_once as helper
from test_dispatch_caller import archive
from test_dispatch_caller_contract import caller  # noqa: F401
from test_subtitle_delivery import delivery, begin as create_intent  # noqa: F401


@pytest.fixture
def synthetic_begin(delivery, tmp_path, monkeypatch):
    db, scheduler, _, _ = delivery
    identity, intent = create_intent(scheduler)
    result = archive(tmp_path, intent)
    calls = []
    monkeypatch.setattr(helper, 'protect_ticket_directory', lambda path: None)
    def run(args, journal, number, *, db_path):
        calls.append((args,journal,number,db_path))
        child = copy.deepcopy(result)
        if '--queue-host' in args:
            from anime.subtitle_host_handoff import prepare
            child['host_handoff'] = prepare(db, child)
        return child
    monkeypatch.setattr(helper, 'run_delivery_cli', run)
    options = dict(job_id=identity, owner='test-owner', revision=intent['revision']-2,
                   expected_input=intent['input_key'], journal=tmp_path/'new-private-call', db_path=db.path)
    return options, result, calls


def test_begin_helper_verifies_real_original_archive_and_returns_redacted_result(synthetic_begin):
    options, result, calls = synthetic_begin
    output = helper.begin_once(**options)
    assert output == helper.public(result) and len(calls)==1
    assert calls[0][0][-2:] == ['--expected-input', options['expected_input']]
    assert calls[0][3] == options['db_path']
    verification = json.loads((options['journal']/'verified.json').read_text())
    assert verification['verified'] and not verification['host_call_started']
    assert verification['not_created_proven'] is False
    assert 'dispatch_token' not in json.dumps(output)
    assert 'lease_token' not in json.dumps(verification)


def test_begin_helper_refuses_old_journal_without_launching(synthetic_begin):
    options, result, calls = synthetic_begin
    options['journal'].mkdir()
    sentinel = options['journal']/'old-evidence'; sentinel.write_bytes(b'preserve')
    with pytest.raises(FileExistsError): helper.begin_once(**options)
    assert not calls and sentinel.read_bytes()==b'preserve'


def test_helper_cli_recovers_original_owner_with_verified_short_packet(caller, tmp_path, monkeypatch, capsys):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    result, path, _ = begin()
    thread = host(result)
    invoke('confirm', identity, '--thread-id', thread, '--ticket', path)
    scheduler.record_blocked(identity, thread, ['test system stop'], owner_stopped=True,
                             recovery_class='system', recovery_turn='original-turn')
    row = scheduler.get(identity)
    monkeypatch.setattr(helper, 'run_delivery_cli', lambda args, journal, number, db_path: invoke(*args))
    monkeypatch.setattr(helper.sys, 'argv', ['begin_once', identity, '--owner', 'synthetic-dispatcher',
                         '--revision', str(row['revision']), '--expected-input', row['input_key'],
                         '--journal', str(tmp_path/'new-call'), '--db', str(db.path),
                         '--auto-recovery-turn', 'original-turn'])
    assert helper.main() == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet['verified'] and packet['responsible_thread_id'] == thread
    assert packet['dispatch_action'] == 'resume' and len(packet['session_prompt']) < 512
    assert 'dispatch_token' not in packet and 'lease_token' not in packet
    assert len(db.rows("SELECT * FROM subtitle_delivery_events WHERE kind='owner-auto-recovery'")) == 1
    assert calls == ['create']


def test_begin_helper_timeout_never_writes_verified_or_retries(synthetic_begin, monkeypatch):
    options, result, calls = synthetic_begin
    def timeout(*args, **kwargs):
        calls.append('one call')
        raise DispatchFailure('synthetic timeout')
    monkeypatch.setattr(helper, 'run_delivery_cli', timeout)
    with pytest.raises(DispatchFailure): helper.begin_once(**options)
    assert calls == ['one call']
    assert (options['journal']/'verified.json').read_bytes()==b''


def test_begin_helper_expected_input_mismatch_preserves_original_ticket(synthetic_begin):
    options, result, calls = synthetic_begin
    original = Path(result['ticket_path']).read_bytes()
    options['expected_input']='different-observed-input'
    with pytest.raises(DispatchFailure, match='identity differs'): helper.begin_once(**options)
    assert len(calls)==1 and Path(result['ticket_path']).read_bytes()==original
    assert (options['journal']/'verified.json').read_bytes()==b''


def test_queue_begin_returns_without_static_export_and_emits_ordered_safe_phases(caller, monkeypatch):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    monkeypatch.setattr(cli, 'protect_ticket_directory', lambda path: None)
    import anime.subtitle_delivery_view as view
    monkeypatch.setattr(view, 'export_view', lambda *args, **kwargs: pytest.fail('begin must not export the whole library'))
    phases=[]
    monkeypatch.setattr(cli, 'begin_phase', lambda *values: phases.append(values))
    result,path,ticket=begin('--expected-input',scheduler.get(identity)['input_key'])
    assert [x[0] for x in phases]==[
        'cli_started','archive_preflight_started','archive_preflight_passed','reserve_started',
        'lease_reserved','intent_started','intent_committed','ticket_archive_started',
        'ticket_verified','result_ready','output_flushed']
    assert all(x[1]==identity for x in phases)
    assert ticket['dispatch_token'] not in json.dumps(phases)
    assert scheduler.get(identity)['dispatch_state']=='committing' and not calls


def test_queue_expected_input_mismatch_fails_before_lease(caller, monkeypatch):
    db, scheduler, identity, cli, invoke, begin, host, calls = caller
    monkeypatch.setattr(cli, 'protect_ticket_directory', lambda path: None)
    before=scheduler.get(identity)
    with pytest.raises(ValueError, match='Expected delivery input changed'):
        begin('--expected-input','wrong-observation')
    assert scheduler.get(identity)==before and not calls
    assert not db.rows("SELECT * FROM subtitle_delivery_events WHERE kind IN ('reserved','dispatch-started')")


def test_begin_phase_write_failure_does_not_break_the_business_operation(caller, monkeypatch):
    *_, cli, invoke, begin, host, calls = caller
    class BrokenLog:
        def write(self, value): raise OSError('synthetic full diagnostic volume')
    with monkeypatch.context() as patch:
        patch.setattr(cli.sys, 'stderr', BrokenLog())
        cli.begin_phase('intent_committed','synthetic-job',8)

def test_queued_host_call_survives_restart_and_has_one_atomic_claim(synthetic_begin):
    from concurrent.futures import ThreadPoolExecutor
    from anime.subtitle_host_handoff import waiting, claim
    from scripts.subtitle_delivery_queue import ControlStore
    options, begin, _ = synthetic_begin
    stored = helper.begin_once(**options, queue_host=True)
    assert 'session_prompt' not in stored
    assert stored['id'] == stored['job_id'] == begin['id']
    db = ControlStore(options['db_path'])
    with db.connect() as c:
        assert waiting(c) == [{'id':begin['id'],'job_id':begin['id'],'revision':begin['revision']}]
    def take():
        try:
            entry = claim(ControlStore(db.path),begin['id'],begin['revision'])
            assert entry['job_id'] == entry['id'] == begin['id']
            return entry['state']
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: take(),range(2))) == ['No unsent handoff','claimed']
    with db.connect() as c:
        assert not waiting(c)


def test_saved_host_return_confirms_after_restart_without_resending(synthetic_begin):
    from anime.subtitle_delivery import SubtitleDelivery
    from anime.subtitle_host_handoff import claim, reconcile_replies
    from scripts.subtitle_delivery_queue import ControlStore
    options, begin, _ = synthetic_begin
    helper.begin_once(**options, queue_host=True)
    db = ControlStore(options['db_path'])
    original = claim(db,begin['id'],begin['revision'])
    service = SubtitleDelivery(db, initialize=False)
    assert not reconcile_replies(service)
    assert service.get(begin['id'])['dispatch_state'] == 'committing'
    Path(original['host_reply_path']).write_text(json.dumps({'threadId':'returned-episode-thread'}),encoding='utf8')
    assert reconcile_replies(service)[0]['accepted']
    job = service.get(begin['id'])
    assert job['dispatch_state'] == 'acknowledged' and job['thread_id'] == 'returned-episode-thread'
    assert not reconcile_replies(service)

def test_native_original_reply_restores_confirmation_if_local_save_was_interrupted(synthetic_begin, tmp_path, monkeypatch):
    import sqlite3
    from anime.subtitle_delivery import SubtitleDelivery
    from anime.subtitle_host_handoff import claim, reconcile_replies
    from scripts.subtitle_delivery_queue import ControlStore
    monkeypatch.setenv('CODEX_THREAD_ID','original-coordinator')
    options, begin, _ = synthetic_begin
    helper.begin_once(**options, queue_host=True)
    db = ControlStore(options['db_path'])
    dispatched = claim(db, begin['id'], begin['revision'])
    history = tmp_path/'host.sqlite'
    at = dispatched['claimed_at']
    call = {'tool':'create_thread','id':'native-item','server':'codex_app','environmentId':'local',
            'status':'completed','arguments':{'prompt':dispatched['session_prompt']},
            'result':{'content':[{'type':'text','text':'{"threadId":"returned-native-owner"}'}]}}
    with sqlite3.connect(history) as c:
        c.execute('CREATE TABLE thread_turns(thread_id,turn_id,started_at,completed_at)')
        c.execute('CREATE TABLE thread_items(thread_id,turn_id,item_type,created_at_ms,item_json)')
        c.execute('INSERT INTO thread_turns VALUES(?,?,?,?)',('original-coordinator','original-turn',at-10,at+10))
        def insert(message):
            c.execute('INSERT INTO thread_items VALUES(?,?,?,?,?)',('original-coordinator','original-turn',
                      'mcpToolCall',int(at*1000),json.dumps(message)))
        insert({**call,'arguments':{'prompt':begin['session_prompt']}})
    service = SubtitleDelivery(db, initialize=False)
    assert not reconcile_replies(service,history)
    with sqlite3.connect(history) as c:
        insert(call)
        from anime.subtitle_host_handoff import native_reply
        reuse = dict(dispatched, dispatch_action='reuse', responsible_thread_id='original-owner')
        insert(dict(call, tool='send_message_to_thread', arguments={
            'threadId':'other-owner', 'prompt':dispatched['session_prompt']}))
    assert native_reply(reuse, history) is None
    with sqlite3.connect(history) as c:
        insert(dict(call, tool='send_message_to_thread', arguments={
            'threadId':'original-owner', 'prompt':dispatched['session_prompt']},
            result={'content':[{'type':'text','text':'{"threadId":"original-owner"}'}]}))
    assert native_reply(reuse, history)[0]['threadId'] == 'original-owner'
    assert native_reply(dict(reuse, dispatch_action='resume'), history)[0]['threadId'] == 'original-owner'
    assert reconcile_replies(service,history)[0]['advanced']
    assert service.get(begin['id'])['thread_id']=='returned-native-owner'
    assert not reconcile_replies(service,history)
