"""Scheduler state machines use temporary databases and synthetic proof adapters."""
import copy
import json

import pytest

from anime.db import Store
from anime.subtitle_delivery import SubtitleDelivery, conservative_readiness
from anime.viewing_workflow import KEY, POLICY


class Readiness:
    def __init__(self):
        self.values = {}

    def __call__(self, db, sid, ep, payload):
        return copy.deepcopy(self.values.get((sid, ep), {
            'material_ready': True, 'clean_review_ready': True, 'capability_ready': True,
            'input_signature': f'original-source-bundle-{sid}-{ep}',
            'target_fingerprint': f'synthetic-media-{sid}-{ep}',
            'material_refs': ['original synthetic material fixture'],
            'capability_refs': ['original synthetic capability fixture'],
            'text_hashes': ['baseline-text'], 'reasons': [],
        }))


@pytest.fixture
def delivery(tmp_path):
    db = Store(tmp_path/'data'/'test.db')
    db.set(KEY, POLICY)
    for sid in (42, 43):
        db.upsert_subject({'id': sid, 'name': f'Example {sid}', 'eps': 3})
        mapping = {'season': 1, 'confirmed': True, 'inventory_checked': True, 'start': 1, 'end': 3}
        db.execute("UPDATE shows SET selected=1,state='watching',authorized=1,mapping=? WHERE id=?", (json.dumps(mapping), sid))
        db.execute('INSERT INTO source_policies VALUES(?,?,0)', (sid, json.dumps({'mode': 'selected'})))
    db.execute('''CREATE TABLE subtitle_studio_jobs(show_id INTEGER,episode INTEGER,state TEXT,payload TEXT,error TEXT,updated REAL,
        PRIMARY KEY(show_id,episode))''')
    for sid in (42, 43):
        for ep in (1, 2):
            db.execute("INSERT INTO subtitle_studio_jobs VALUES(?,?,'waiting_review',?,'',0)",
                       (sid, ep, json.dumps({'draft_ids': ['source-fixture'], 'target': {'fingerprint': f'synthetic-media-{sid}-{ep}'}})))
    now = [1000.0]
    ready = Readiness()
    scheduler = SubtitleDelivery(db, ready, lambda: now[0])
    scheduler.refresh()
    return db, scheduler, ready, now


def begin(scheduler, sid=42, ep=1, owner='test-owner', experience=False):
    identity = scheduler.episode_id(sid, ep)
    row = scheduler.get(identity)
    lease = scheduler.reserve(identity, owner, row['revision'], allow_experience=experience)
    intent = scheduler.begin_dispatch(identity, owner, lease['lease_token'], lease['revision'])
    return identity, intent


def accepted(scheduler, ready, sid=42, ep=1):
    identity, intent = begin(scheduler, sid, ep)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    assessment = ready(scheduler.db, sid, ep, {})
    ready.values[(sid, ep)] = {**assessment, 'accepted_verified': True, 'accepted_build_id': 'actual-fixture-build'}
    return scheduler.record_accepted(identity, 'actual-fixture-chat', 'actual-fixture-build')


def test_refresh_is_idempotent_and_never_changes_media_or_watch(delivery):
    db, scheduler, ready, now = delivery
    before = {table: db.rows(f'SELECT * FROM {table}') for table in ('episodes', 'watches', 'tasks', 'subtitle_studio_jobs')}
    events = db.rows('SELECT * FROM subtitle_delivery_events')
    assert scheduler.refresh()['changed'] == 0
    assert db.rows('SELECT * FROM subtitle_delivery_events') == events
    assert all(db.rows(f'SELECT * FROM {table}') == rows for table, rows in before.items())
    assert scheduler.get('episode:42:1')['state'] == 'ready'


@pytest.mark.parametrize('field', ['material_ready', 'clean_review_ready', 'capability_ready'])
def test_ready_requires_every_actual_material_and_capability_gate(delivery, field):
    db, scheduler, ready, now = delivery
    ready.values[(42, 1)] = {**ready(db, 42, 1, {}), field: False, 'reasons': [f'needs-{field}']}
    scheduler.refresh()
    row = scheduler.get('episode:42:1')
    assert row['state'] != 'ready'
    with pytest.raises(ValueError, match='Readiness'):
        scheduler.reserve(row['id'], 'owner', row['revision'])
    assert not scheduler.has_active(42, 1)


def test_ready_does_not_require_four_stage_acceptance_but_requires_exact_proofs(delivery):
    db, scheduler, ready, now = delivery
    row = scheduler.get('episode:42:1')
    assert row['state'] == 'ready' and not row['accepted_build_id']
    ready.values[(42, 1)] = {**ready(db, 42, 1, {}), 'capability_refs': []}
    scheduler.refresh()
    assert scheduler.get('episode:42:1')['state'] != 'ready'


@pytest.mark.parametrize('change', ['wish', 'unselected', 'unauthorized', 'cleanup', 'mapping', 'policy', 'bounds'])
def test_live_scope_rechecked_before_claim(delivery, change):
    db, scheduler, ready, now = delivery
    row = scheduler.get('episode:42:1')
    if change == 'wish':
        db.execute("UPDATE shows SET state='wish',auto_download=1 WHERE id=42")
    elif change == 'unselected':
        db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif change == 'unauthorized':
        db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif change == 'cleanup':
        db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    elif change == 'policy':
        db.execute('UPDATE source_policies SET config=? WHERE show_id=42', (json.dumps({'mode': 'scope_hold'}),))
    else:
        mapping = db.mapping(42)
        mapping['confirmed' if change == 'mapping' else 'start'] = False if change == 'mapping' else 2
        db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    with pytest.raises(ValueError):
        scheduler.reserve(row['id'], 'owner', row['revision'])
    assert not scheduler.has_active(42, 1)
    scheduler.refresh()
    assert scheduler.get(row['id'])['state'] == 'blocked'


def test_finished_samples_low_priority_and_not_automatically_dispatched(delivery):
    db, scheduler, ready, now = delivery
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    watch = db.one('SELECT * FROM watches')
    scheduler.refresh()
    snapshot = scheduler.snapshot()
    assert snapshot['jobs'][0]['id'] != 'episode:42:1'
    row = scheduler.get('episode:42:1')
    with pytest.raises(ValueError, match='Finished'):
        scheduler.reserve(row['id'], 'owner', row['revision'])
    lease = scheduler.reserve(row['id'], 'experience-owner', row['revision'], allow_experience=True)
    assert lease['state'] == 'production' and db.one('SELECT * FROM watches') == watch


def test_global_single_owner_and_revision_conflict(delivery):
    db, scheduler, ready, now = delivery
    row = scheduler.get('episode:42:1')
    lease = scheduler.reserve(row['id'], 'first-owner', row['revision'])
    other = scheduler.get('episode:43:1')
    with pytest.raises(ValueError, match='single production owner'):
        scheduler.reserve(other['id'], 'second-owner', other['revision'])
    with pytest.raises(ValueError, match='revision'):
        scheduler.reserve(row['id'], 'first-owner', row['revision'])
    assert scheduler.reserve(row['id'], 'first-owner', lease['revision'])['lease_token'] == lease['lease_token']


def test_only_undispatched_expired_lease_can_be_reclaimed(delivery):
    db, scheduler, ready, now = delivery
    row = scheduler.get('episode:42:1')
    lease = scheduler.reserve(row['id'], 'lost-reserver', row['revision'], lease_seconds=30)
    now[0] += 31
    replacement = scheduler.reserve(row['id'], 'new-reserver', lease['revision'])
    assert replacement['lease_token'] != lease['lease_token']
    with pytest.raises(ValueError, match='lease/revision'):
        scheduler.begin_dispatch(row['id'], 'lost-reserver', lease['lease_token'], lease['revision'])


def test_unknown_dispatch_survives_restart_and_expired_lease(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'])
    now[0] += 100000
    restarted = SubtitleDelivery(Store(db.path), ready, lambda: now[0])
    assert restarted.has_active(42, 1)
    row = restarted.get(identity)
    with pytest.raises(ValueError, match='reconciled'):
        restarted.reserve(identity, 'new-owner', row['revision'])
    other = restarted.get('episode:43:1')
    with pytest.raises(ValueError, match='single production owner'):
        restarted.reserve(other['id'], 'new-owner', other['revision'])
    result = restarted.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='receipt-arrived-late')
    assert result['thread_id'] == 'receipt-arrived-late'
    assert restarted.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='receipt-arrived-late')['duplicate']
    with pytest.raises(ValueError, match='already exists'):
        restarted.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='another-chat')


def test_verified_absence_required_to_release_dispatch(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    with pytest.raises(ValueError, match='absence evidence'):
        scheduler.reconcile_dispatch(identity, intent['dispatch_token'], definitely_not_created=True)
    result = scheduler.reconcile_dispatch(identity, intent['dispatch_token'], definitely_not_created=True,
                                          absence_evidence='Synthetic verified no-create receipt fixture')
    assert result['state'] == 'ready' and not scheduler.has_active(42, 1)


def test_refresh_keeps_active_input_and_chat_while_new_sources_arrive(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='one-responsible-chat')
    ready.values[(42, 1)] = {**ready(db, 42, 1, {}), 'input_signature': 'late-source-bundle'}
    scheduler.refresh()
    row = scheduler.get(identity)
    assert row['thread_id'] == 'one-responsible-chat' and row['input_key'] == intent['input_key']
    assert row['payload']['latest_assessment']['assessment']['input_signature'] == 'late-source-bundle'
    assert scheduler.has_active(42, 1)
    assert scheduler.refresh()['changed'] == 0


def test_blocker_keeps_same_owner_and_acceptance_is_verified_observation(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    scheduler.record_blocked(identity, 'actual-fixture-chat', ['actual audio capability unavailable'])
    assert scheduler.has_active(42, 1)
    with pytest.raises(ValueError, match='accepted build'):
        scheduler.record_accepted(identity, 'actual-fixture-chat', 'invented')
    assessment = ready(db, 42, 1, {})
    ready.values[(42, 1)] = {**assessment, 'accepted_verified': True, 'accepted_build_id': 'actual-fixture-build'}
    result = scheduler.record_accepted(identity, 'actual-fixture-chat', 'actual-fixture-build')
    assert result['state'] == 'accepted' and not result['published'] and not scheduler.has_active(42, 1)
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')


def test_late_sources_same_text_are_deduplicated_and_new_text_coalesces(delivery):
    db, scheduler, ready, now = delivery
    initial = accepted(scheduler, ready)
    baseline = initial['accepted_build_id']
    same = scheduler.enqueue_late_review(42, 1, 'new-packaging', 'baseline-text', ['original synthetic provenance'])
    assert same['disposition'] == 'same-text' and same['job_id'] is None
    first = scheduler.enqueue_late_review(42, 1, 'new-source-a', 'different-text-a', ['original synthetic provenance'])
    again = scheduler.enqueue_late_review(42, 1, 'new-source-a', 'different-text-a', ['original synthetic provenance'])
    assert again['duplicate'] and first['job_id'] == again['job_id']
    repackage = scheduler.enqueue_late_review(42, 1, 'different-package', 'different-text-a', ['original synthetic provenance'])
    assert repackage['job_id'] == first['job_id'] and repackage['disposition'] == 'same-text'
    second = scheduler.enqueue_late_review(42, 1, 'new-source-b', 'different-text-b', ['original synthetic provenance'])
    assert second['job_id'] == first['job_id']
    row = scheduler.get(first['job_id'])
    assert row['state'] == 'late-review' and len(row['payload']['late_sources']) == 2
    assert scheduler.get(initial['id'])['accepted_build_id'] == baseline
    assert not db.rows('SELECT * FROM episodes')


def test_late_review_active_inputs_are_not_overwritten_and_use_one_owner(delivery):
    db, scheduler, ready, now = delivery
    accepted(scheduler, ready)
    late = scheduler.enqueue_late_review(42, 1, 'source-a', 'text-a', ['synthetic provenance'])
    row = scheduler.get(late['job_id'])
    lease = scheduler.reserve(row['id'], 'v2-owner', row['revision'])
    intent = scheduler.begin_dispatch(row['id'], 'v2-owner', lease['lease_token'], lease['revision'])
    assert intent['action'] == 'reuse-thread' and intent['thread_id'] == 'actual-fixture-chat'
    scheduler.reconcile_dispatch(row['id'], intent['dispatch_token'], thread_id='actual-fixture-chat')
    pending = scheduler.enqueue_late_review(42, 1, 'source-b', 'text-b', ['synthetic provenance'])
    assert pending['job_id'] != row['id']
    assert len(scheduler.get(row['id'])['payload']['late_sources']) == 1
    nextrow = scheduler.get(pending['job_id'])
    with pytest.raises(ValueError, match='single production owner'):
        scheduler.reserve(nextrow['id'], 'v2-second-owner', nextrow['revision'])


def test_late_review_requires_baseline_and_preserves_immutable_source(delivery):
    db, scheduler, ready, now = delivery
    with pytest.raises(ValueError, match='accepted baseline'):
        scheduler.enqueue_late_review(42, 1, 'source-a', 'text-a', ['synthetic provenance'])
    accepted(scheduler, ready)
    scheduler.enqueue_late_review(42, 1, 'source-a', 'text-a', ['synthetic provenance'])
    with pytest.raises(ValueError, match='conflicting text'):
        scheduler.enqueue_late_review(42, 1, 'source-a', 'wrong-other-text', ['synthetic provenance'])


def test_readonly_snapshot_and_conservative_existing_completed_media(delivery):
    db, scheduler, ready, now = delivery
    before = db.rows('SELECT * FROM subtitle_delivery_jobs')
    snapshot = scheduler.snapshot()
    assert all('lease_token' not in job and 'dispatch_token' not in job for job in snapshot['jobs'])
    assert db.rows('SELECT * FROM subtitle_delivery_jobs') == before
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection) VALUES('fixture-hash',42,'RAW MKV','complete',?)",
               (json.dumps({'selected': {'3': {'name': 'episode3.mkv'}}}),))
    assessment = conservative_readiness(db, 42, 3, {})
    assert not assessment['material_ready'] and not assessment['capability_ready']
    assert 'completed_media_needs_inventory_or_reconcile' in assessment['reasons']
    assert not assessment['clean_review_ready']


def test_record_preparation_creates_no_dispatch(delivery):
    db, scheduler, ready, now = delivery
    row = scheduler.record_preparation(42, 3)
    assert row['state'] == 'ready' and not row['thread_id'] and not row['dispatch_token']
    assert not scheduler.has_active(42, 3)


def test_unready_materials_preparing_and_capability_only_failure_blocked(delivery):
    db, scheduler, ready, now = delivery
    ready.values[(42, 1)] = {**ready(db, 42, 1, {}), 'material_ready': False, 'capability_ready': False}
    ready.values[(42, 2)] = {**ready(db, 42, 2, {}), 'capability_ready': False}
    scheduler.refresh()
    assert scheduler.get('episode:42:1')['state'] == 'preparing'
    assert scheduler.get('episode:42:2')['state'] == 'blocked'


def test_stopped_blocker_releases_global_slot_but_resume_reuses_original_chat(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='original-episode-chat')
    scheduler.record_blocked(identity, 'original-episode-chat', ['specific unavailable modality'], owner_stopped=True)
    assert not scheduler.has_active(42, 1)
    row = scheduler.get('episode:43:1')
    other_lease = scheduler.reserve(row['id'], 'other-owner', row['revision'], lease_seconds=30)
    old = scheduler.get(identity)
    with pytest.raises(ValueError, match='single production owner'):
        scheduler.resume(identity, 'original-episode-chat', 'same-owner', old['revision'])
    now[0] += 31
    resumed = scheduler.resume(identity, 'original-episode-chat', 'same-owner', old['revision'])
    followup = scheduler.begin_dispatch(identity, 'same-owner', resumed['lease_token'], resumed['revision'])
    assert followup['action'] == 'reuse-thread' and followup['thread_id'] == 'original-episode-chat'
    scheduler.reconcile_dispatch(identity, followup['dispatch_token'], thread_id='original-episode-chat')
    assert scheduler.has_active(42, 1)


def test_unknown_followup_cannot_be_released_as_stopped_blocker(delivery):
    db, scheduler, ready, now = delivery
    accepted(scheduler, ready)
    late = scheduler.enqueue_late_review(42, 1, 'source-a', 'text-a', ['synthetic provenance'])
    row = scheduler.get(late['job_id'])
    lease = scheduler.reserve(row['id'], 'v2-owner', row['revision'])
    intent = scheduler.begin_dispatch(row['id'], 'v2-owner', lease['lease_token'], lease['revision'])
    scheduler.reconcile_dispatch(row['id'], intent['dispatch_token'])
    with pytest.raises(ValueError, match='Unknown dispatch'):
        scheduler.record_blocked(row['id'], 'actual-fixture-chat', ['ordinary blocker'], owner_stopped=True)
    assert scheduler.has_active(42, 1)


def test_refresh_automatically_deduplicates_late_archive_and_sends_only_differences(delivery):
    db, scheduler, ready, now = delivery
    baseline = {'kind': 'archived_draft', 'id': 'original-draft', 'raw_hash': 'original-raw', 'text_hash': 'baseline-text'}
    ready.values[(42, 1)] = {**ready(db, 42, 1, {}), 'material_refs': [baseline]}
    scheduler.refresh()
    accepted(scheduler, ready)
    ready.values[(42, 1)]['material_refs'].extend([
        {'kind': 'archived_draft', 'id': 'same-package', 'raw_hash': 'same-package-raw', 'text_hash': 'baseline-text'},
        {'kind': 'archived_draft', 'id': 'new-draft', 'raw_hash': 'new-draft-raw', 'text_hash': 'new-independent-text'},
    ])
    scheduler.refresh()
    late = db.rows("SELECT * FROM subtitle_delivery_jobs WHERE kind='late'")
    assert len(late) == 1 and json.loads(late[0]['payload'])['late_sources'][0]['source_hash'] == 'new-draft-raw'
    assert len(db.rows('SELECT * FROM subtitle_delivery_late_inputs')) == 2
    events = db.rows('SELECT * FROM subtitle_delivery_events')
    assert scheduler.refresh()['changed'] == 0
    assert db.rows('SELECT * FROM subtitle_delivery_events') == events


def test_accepted_observation_rechecks_scope_and_target_and_v2_advances_baseline(delivery):
    db, scheduler, ready, now = delivery
    accepted(scheduler, ready)
    late = scheduler.enqueue_late_review(42, 1, 'source-a', 'text-a', ['synthetic provenance'])
    row = scheduler.get(late['job_id'])
    lease = scheduler.reserve(row['id'], 'v2-owner', row['revision'])
    intent = scheduler.begin_dispatch(row['id'], 'v2-owner', lease['lease_token'], lease['revision'])
    scheduler.reconcile_dispatch(row['id'], intent['dispatch_token'], thread_id='actual-fixture-chat')
    ready.values[(42, 1)]['accepted_build_id'] = 'actual-v2-build'
    ready.values[(42, 1)]['target_fingerprint'] = 'wrong-target'
    with pytest.raises(ValueError, match='accepted build'):
        scheduler.record_accepted(row['id'], 'actual-fixture-chat', 'actual-v2-build')
    ready.values[(42, 1)]['target_fingerprint'] = 'synthetic-media-42-1'
    db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    with pytest.raises(ValueError, match='scope changed'):
        scheduler.record_accepted(row['id'], 'actual-fixture-chat', 'actual-v2-build')
    db.execute('UPDATE shows SET cleanup_hold=0 WHERE id=42')
    scheduler.record_accepted(row['id'], 'actual-fixture-chat', 'actual-v2-build')
    initial = scheduler.get('episode:42:1')
    assert initial['accepted_build_id'] == 'actual-v2-build'
    assert initial['payload']['accepted_history'] == ['actual-fixture-build']


def test_separate_scheduler_connections_compete_for_one_global_owner(delivery):
    from concurrent.futures import ThreadPoolExecutor
    db, scheduler, ready, now = delivery
    first = SubtitleDelivery(Store(db.path), ready, lambda: now[0])
    second = SubtitleDelivery(Store(db.path), ready, lambda: now[0])
    def claim(instance, identity):
        row = instance.get(identity)
        try:
            return instance.reserve(identity, identity, row['revision'])['id']
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda pair: claim(*pair), [(first, 'episode:42:1'), (second, 'episode:43:1')]))
    assert len([claim_id for claim_id in claims if claim_id]) == 1
    assert len(db.rows('SELECT id FROM subtitle_delivery_jobs WHERE lease_expires>?', (now[0],))) == 1


def test_scope_revoked_by_assessment_is_rechecked_in_claim_transaction(delivery):
    db, scheduler, ready, now = delivery
    def revoked(current_db, sid, ep, payload):
        result = ready(current_db, sid, ep, payload)
        current_db.execute('UPDATE shows SET authorized=0 WHERE id=?', (sid,))
        return result
    scheduler.readiness = revoked
    row = scheduler.get('episode:42:1')
    with pytest.raises(ValueError, match='scope changed during claim'):
        scheduler.reserve(row['id'], 'owner', row['revision'])
    assert not scheduler.has_active(42, 1)


def test_old_pending_v2_cannot_start_after_baseline_advanced(delivery):
    db, scheduler, ready, now = delivery
    accepted(scheduler, ready)
    late = scheduler.enqueue_late_review(42, 1, 'source-a', 'text-a', ['synthetic provenance'])
    db.execute("UPDATE subtitle_delivery_jobs SET accepted_build_id='newer-host-build' WHERE id='episode:42:1'")
    row = scheduler.get(late['job_id'])
    with pytest.raises(ValueError, match='baseline changed'):
        scheduler.reserve(row['id'], 'v2-owner', row['revision'])
