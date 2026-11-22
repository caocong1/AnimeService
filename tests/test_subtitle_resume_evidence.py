import json
import copy
import hashlib
import pytest
from test_subtitle_evidence import archive, write, capability, rows_snapshot
from test_subtitle_delivery import delivery
from anime.subtitle_evidence import register_evidence


def stopped(delivery):
    db, scheduler, ready, now = delivery
    j = scheduler.get('episode:42:1')
    baseline = {'release_id': 'fixture-v0', 'video_sha256': 'a' * 64}
    p = {**j['payload'], 'refinement_pending': True, 'owner_stopped_writing': True,
         'v0_observation': baseline}
    db.execute("UPDATE subtitle_delivery_jobs SET state='blocked',dispatch_state='blocked-stopped',thread_id='original',payload=? WHERE id=?",
               (json.dumps(p), j['id']))
    db.set('subtitle_delivery_finished_42_1', baseline)
    return db, scheduler, j


def test_stopped_owner_refresh_preserves_input_baseline_and_dispatch(delivery):
    db, scheduler, j = stopped(delivery)
    report = write(archive(db), 'fresh.json', capability())
    result = register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                              thread_id='original', expected_input=j['input_key'])
    after = scheduler.get(j['id'])
    assert result['registered'] and after['state'] == 'blocked'
    assert after['dispatch_state'] == 'blocked-stopped' and after['thread_id'] == 'original'
    assert after['input_key'] == j['input_key'] and after['payload']['owner_stopped_writing']
    assert after['payload']['v0_observation']['release_id'] == 'fixture-v0'


@pytest.mark.parametrize('change', ['owner', 'input', 'watched', 'active', 'lease', 'baseline', 'target', 'missing_flag'])
def test_recovery_rejects_changed_protected_facts(delivery, change):
    db, scheduler, j = stopped(delivery)
    report = write(archive(db), 'fresh.json', capability())
    owner, key = 'original', j['input_key']
    if change == 'owner': owner = 'other'
    if change == 'input': key = 'stale'
    if change == 'watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    if change == 'active': db.execute("UPDATE subtitle_delivery_jobs SET dispatch_state='acknowledged' WHERE id=?", (j['id'],))
    if change == 'lease': db.execute('UPDATE subtitle_delivery_jobs SET lease_expires=9999999999 WHERE id=?', (j['id'],))
    if change == 'baseline': db.set('subtitle_delivery_finished_42_1', {'release_id': 'different'})
    if change == 'target': db.set('subtitle_delivery_readiness_42_1', {'target': {'fingerprint': 'different'}})
    if change == 'missing_flag':
        p = scheduler.get(j['id'])['payload'];p['refinement_pending'] = False
        db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?', (json.dumps(p),j['id']))
    before = rows_snapshot(db)
    with pytest.raises(ValueError):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report, thread_id=owner, expected_input=key)
    assert rows_snapshot(db) == before


def bundle_signature(assessment):
    drafts = [{k: v for k, v in d.items() if k not in
               ('kind', 'target_matching_status', 'candidate_not_reliable_base')}
              for d in assessment['material_refs'] if d['kind'] == 'archived_draft']
    return hashlib.sha256(json.dumps({'target': assessment['target_fingerprint'], 'drafts': drafts},
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def append_reference(delivery, v0=False):
    db, scheduler, ready, now = delivery
    j = scheduler.get('episode:42:1')
    a = copy.deepcopy(j['payload']['assessment'])
    a['material_refs'] = [
        {'kind': 'completed_media', 'hash': 'media-task',
         'target': {'fingerprint': 'synthetic-media-42-1', 'path': 'unchanged.mkv', 'size': 10}},
        {'kind': 'archived_draft', 'id': 'zh', 'raw_hash': 'a' * 64, 'text_hash': 'baseline-text',
         'language': 'chi', 'estimated': False, 'mixed': False, 'origin': 'embedded',
         'target_fingerprint': 'synthetic-media-42-1'}]
    a['input_signature'] = bundle_signature(a)
    p = {**j['payload'], 'assessment': a, 'owner_stopped_writing': True}
    if v0:
        p.update(refinement_pending=True, v0_observation={'release_id': 'v0', 'video_sha256': 'b' * 64})
        db.set('subtitle_delivery_finished_42_1', p['v0_observation'])
        db.set('subtitle_delivery_policy', {'editions': ['v0', 'v1']})
    db.execute("UPDATE subtitle_delivery_jobs SET state='blocked', dispatch_state='blocked-stopped', thread_id='original', input_key=?, payload=? WHERE id=?",
               (a['input_signature'], json.dumps(p), j['id']))
    live = copy.deepcopy(a)
    live['material_refs'].append({'kind': 'archived_draft', 'id': 'ja', 'raw_hash': 'c' * 64,
                                 'text_hash': 'new-reference-text', 'language': 'jpn', 'estimated': False,
                                 'mixed': False, 'origin': 'jimaku', 'target_fingerprint': 'synthetic-media-42-1'})
    live['input_signature'] = bundle_signature(live)
    ready.values[(42, 1)] = live
    return db, scheduler, ready, scheduler.get(j['id']), live


@pytest.mark.parametrize('v0', [False, True])
def test_stopped_owner_additional_reference_keeps_fixed_bundle_and_can_resume(delivery, v0):
    db, scheduler, ready, j, live = append_reference(delivery, v0)
    original_assessment = copy.deepcopy(j['payload']['assessment'])
    report = write(archive(db), 'fresh-append.json', capability())
    register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                      thread_id='original', expected_input=j['input_key'])
    after = scheduler.get(j['id'])
    assert after['payload']['assessment'] == original_assessment
    assert after['input_key'] == j['input_key']
    assert after['payload']['latest_assessment']['assessment']['observed_input_signature'] == live['input_signature']
    assert after['payload']['recovery_evidence_history'][0]['assessment']['input_signature'] == j['input_key']
    before = rows_snapshot(db)
    assert register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                             thread_id='original', expected_input=j['input_key'])['replayed']
    assert rows_snapshot(db) == before
    lease = scheduler.resume(j['id'], 'original', 'dispatcher', after['revision'], manual_recovery=True)
    ticket = scheduler.begin_dispatch(j['id'], 'dispatcher', lease['lease_token'], lease['revision'])
    assert ticket['input_key'] == j['input_key'] and ticket['ticket']['dispatch_action'] == 'reuse'
    assert ticket['ticket']['thread_id'] == 'original'


@pytest.mark.parametrize('change', ['remove', 'raw_hash', 'text_hash', 'origin', 'language', 'estimated',
                                    'mixed', 'target_fingerprint', 'id', 'media', 'target', 'signature',
                                    'original_signature', 'duplicate', 'chinese_hash', 'no_addition'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_reference_append_cannot_hide_a_changed_original_bundle(delivery, change, dry_run):
    db, scheduler, ready, j, live = append_reference(delivery)
    if change == 'remove': live['material_refs'].pop(1)
    elif change in ('raw_hash', 'text_hash', 'origin', 'language', 'target_fingerprint', 'id'):
        live['material_refs'][1][change] = 'changed'
    elif change in ('estimated', 'mixed'): live['material_refs'][1][change] = True
    elif change == 'media': live['material_refs'][0]['target']['path'] = 'other.mkv'
    elif change == 'target': live['target_fingerprint'] = 'different'
    elif change == 'duplicate': live['material_refs'].append(copy.deepcopy(live['material_refs'][1]))
    elif change == 'chinese_hash': live['text_hashes'] = []
    elif change == 'no_addition': live['material_refs'].pop()
    elif change == 'original_signature':
        p = j['payload']; p['assessment']['input_signature'] = 'unreplayable'
        db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?', (json.dumps(p), j['id']))
    live['input_signature'] = 'arbitrary-drift' if change in ('signature', 'no_addition') else bundle_signature(live)
    report = write(archive(db), 'rejected-append.json', capability())
    before = rows_snapshot(db)
    with pytest.raises(ValueError):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                          thread_id='original', expected_input=j['input_key'], dry_run=dry_run)
    assert rows_snapshot(db) == before
    assessment = scheduler._dispatch_assessment(scheduler.get(j['id']))
    assert not assessment['ready'] or assessment['input_signature'] != j['input_key']


def test_new_reference_does_not_allow_ordinary_evidence_entry_or_new_owner(delivery):
    db, scheduler, ready, j, live = append_reference(delivery)
    report = write(archive(db), 'guarded-append.json', capability())
    before = rows_snapshot(db)
    with pytest.raises(ValueError, match='in flight'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report)
    with pytest.raises(ValueError, match='original owner'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                          thread_id='other', expected_input=j['input_key'])
    assert rows_snapshot(db) == before


def test_dispatch_revalidates_v0_baseline_after_evidence_registration(delivery):
    db, scheduler, ready, j, live = append_reference(delivery, v0=True)
    report = write(archive(db), 'v0-fresh.json', capability())
    register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                      thread_id='original', expected_input=j['input_key'])
    after = scheduler.get(j['id'])
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'other', 'video_sha256': 'b' * 64})
    with pytest.raises(ValueError, match='Readiness changed'):
        scheduler.resume(j['id'], 'original', 'dispatcher', after['revision'])


def test_recovery_rechecks_archive_proofs_before_commit(delivery):
    db, scheduler, ready, j, live = append_reference(delivery)
    calls = []
    def changed_archive(*args):
        value = ready(*args)
        calls.append(True)
        if len(calls) == 1:
            live['material_refs'].pop(1)
            live['input_signature'] = bundle_signature(live)
        return value
    scheduler.readiness = changed_archive
    report = write(archive(db), 'concurrent-archive.json', capability())
    before = rows_snapshot(db)
    with pytest.raises(ValueError, match='source evidence changed'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report,
                          thread_id='original', expected_input=j['input_key'])
    assert len(calls) == 2 and rows_snapshot(db) == before
