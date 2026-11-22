"""An unresolved external dispatch retains its original immutable receipt ticket."""
import pytest

from test_subtitle_delivery import delivery, begin  # noqa: F401


@pytest.mark.parametrize('dispatch_state', ['committing', 'unknown'])
@pytest.mark.parametrize('change', ['unchanged', 'new_reference', 'capability_expired'])
def test_pending_refresh_preserves_entire_job_and_original_created_callback(delivery, dispatch_state, change):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    if dispatch_state == 'unknown':
        db.execute("UPDATE subtitle_delivery_jobs SET dispatch_state='unknown' WHERE id=?", (identity,))
    current = ready(db, 42, 1, {})
    if change == 'new_reference':
        current['input_signature'] = 'newly-archived-reference'
        current['material_refs'].append({'kind': 'archived_draft', 'raw_hash': 'new-reference'})
    if change == 'capability_expired':
        current.update(capability_ready=False, reasons=['expired'])
    ready.values[(42, 1)] = current
    before = scheduler.get(identity)
    scheduler.refresh()
    assert scheduler.get(identity) == before
    # Diagnostics can observe the changed live readiness without changing the
    # original intent or pretending that a changed source is its fixed input.
    observed = scheduler._dispatch_assessment(before)
    if change == 'new_reference': assert observed['input_signature'] != intent['input_key']
    if change == 'capability_expired': assert observed['ready'] is False
    result = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='original-chat')
    assert (result['receipt_accepted'], result['state_changed'], result['reason']) == (1, 1, '')
    assert result['job']['input_key'] == intent['input_key']


@pytest.mark.parametrize('change', ['authorization', 'selection', 'watched'])
def test_pending_refresh_preserves_ticket_but_callback_rechecks_current_scope(delivery, change):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    if change == 'authorization': db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    if change == 'selection': db.execute('UPDATE shows SET selected=0 WHERE id=42')
    if change == 'watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    before = scheduler.get(identity)
    # Empty watching snapshot also exercises the scope-cleanup refresh branch.
    scheduler.refresh({'queues': {'watching': []}})
    scheduler.refresh()
    assert scheduler.get(identity) == before
    result = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='original-chat')
    assert (result['receipt_accepted'], result['state_changed'], result['reason']) == (1, 0, 'scope_changed')
    assert scheduler.get(identity) == before


def test_new_reference_refresh_preserves_reused_original_owner_ticket(delivery):
    from test_subtitle_resume_evidence import append_reference
    db, scheduler, ready, j, live = append_reference(delivery)
    lease = scheduler.resume(j['id'], 'original', 'dispatcher', j['revision'], manual_recovery=True)
    intent = scheduler.begin_dispatch(j['id'], 'dispatcher', lease['lease_token'], lease['revision'])
    assert intent['ticket']['dispatch_action'] == 'reuse'
    before = scheduler.get(j['id'])
    scheduler.refresh()
    assert scheduler.get(j['id']) == before
    result = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='original')
    assert (result['receipt_accepted'], result['state_changed'], result['reason']) == (1, 1, '')
    assert result['job']['thread_id'] == 'original' and result['job']['input_key'] == j['input_key']


@pytest.mark.parametrize('field,reason', [('input_key', 'input_moved'), ('dispatch_token', 'dispatch_mismatch')])
def test_refresh_does_not_repair_a_different_input_or_external_intent(delivery, field, reason):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    db.execute(f"UPDATE subtitle_delivery_jobs SET {field}='different' WHERE id=?", (identity,))
    before = scheduler.get(identity)
    scheduler.refresh()
    assert scheduler.get(identity) == before
    result = scheduler.record_dispatch_receipt(intent['ticket'], 'created', thread_id='original-chat')
    assert (result['receipt_accepted'], result['state_changed'], result['reason']) == (0, 0, reason)
    assert scheduler.get(identity) == before
