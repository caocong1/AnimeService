"""Plans are read-only observations, never replacement begin tickets or authorization."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_subtitle_delivery import delivery, begin  # noqa: F401


def pending(scheduler):
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'])
    receipt = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='synthetic-chat')
    assert receipt['reason'] == 'revision_moved'
    return identity, intent['ticket'], receipt['receipt_id']


def snapshot(db):
    return {table: db.rows('SELECT * FROM '+table) for table in ('subtitle_delivery_jobs', 'subtitle_delivery_events', 'dispatch_receipts', 'watches')}


def test_moved_revision_requires_explicit_host_reverification_and_never_mutates(delivery):
    db, scheduler, ready, now = delivery
    identity, ticket, receipt = pending(scheduler)
    before = snapshot(db)
    plan = scheduler.reconciliation_plan(ticket, receipt)
    assert plan['current_revision'] > plan['ticket_revision']
    assert plan['status'] == 'requires_host_reverification' and not plan['can_apply']
    assert ticket['dispatch_token'] not in json.dumps(plan) and ticket['lease_token'] not in json.dumps(plan)
    assert snapshot(db) == before


@pytest.mark.parametrize('mutation,reason', [
    ("UPDATE shows SET selected=0 WHERE id=42", 'scope_changed'),
    ("UPDATE subtitle_delivery_jobs SET lease_owner='another'", 'owner_mismatch'),
    ("UPDATE subtitle_delivery_jobs SET input_key='new-input'", 'input_moved'),
    ("UPDATE subtitle_delivery_jobs SET dispatch_state='blocked-stopped'", 'state_conflict')])
def test_plan_reports_current_protections_without_repairing_them(delivery, mutation, reason):
    db, scheduler, ready, now = delivery
    identity, ticket, receipt = pending(scheduler)
    db.execute(mutation)
    before = snapshot(db)
    plan = scheduler.reconciliation_plan(ticket, receipt)
    assert plan['status'] == 'blocked' and reason in plan['blockers'] and not plan['can_apply']
    assert snapshot(db) == before


def test_edited_ticket_wrong_receipt_and_non_created_fact_are_rejected(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler); ticket = intent['ticket']
    fact = scheduler.record_dispatch_receipt(ticket, 'failed')
    before = snapshot(db)
    for t, receipt in (({**ticket, 'issued_at': 0}, fact['receipt_id']), (ticket, 'a'*64), (ticket, fact['receipt_id']), (ticket, 'bad')):
        with pytest.raises(ValueError): scheduler.reconciliation_plan(t, receipt)
    assert snapshot(db) == before


def test_cli_plan_uses_actual_readonly_store_and_never_initializes(delivery, tmp_path, monkeypatch, capsys):
    db, scheduler, ready, now = delivery
    identity, ticket, receipt = pending(scheduler)
    path = tmp_path/'ticket.json';path.write_text(json.dumps(ticket),encoding='utf-8')
    spec=importlib.util.spec_from_file_location('queue_plan_cli',Path(__file__).resolve().parents[1]/'scripts/subtitle_delivery_queue.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    original=cli.SubtitleDelivery
    def no_initialize(store, initialize=True):
        assert initialize is False and isinstance(store,cli.ReadStore)
        assert store.con.execute('PRAGMA query_only').fetchone()[0] == 1
        return original(store,initialize=initialize)
    monkeypatch.setattr(cli,'SubtitleDelivery',no_initialize)
    monkeypatch.setattr(sys,'argv',['queue','--db',str(db.path),'reconcile-plan',identity,'--ticket',str(path),'--receipt-id',receipt])
    before=snapshot(db);cli.main();out=json.loads(capsys.readouterr().out)
    assert out['read_only'] and not out['can_apply'] and out['status']=='requires_host_reverification'
    assert snapshot(db)==before


def test_already_acknowledged_is_not_proposed_for_reapplication(delivery):
    db, scheduler, ready, now=delivery
    identity,intent=begin(scheduler)
    receipt=scheduler.record_dispatch_receipt(intent['ticket'],'created',thread_id='synthetic-chat')
    before=snapshot(db)
    plan=scheduler.reconciliation_plan(intent['ticket'],receipt['receipt_id'])
    assert plan['status']=='already_acknowledged' and not plan['can_apply']
    assert snapshot(db)==before
