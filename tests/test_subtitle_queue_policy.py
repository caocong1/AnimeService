"""Opt-in queue policies: first versions across shows before any v1, and the longest-unserved show first.

Both are keys of the existing `subtitle_delivery_policy` record and default off; with them off the
scheduler behaves exactly as before. Synthetic releases only: the release verifier is replaced.
"""
import copy
import json

import pytest

from anime.db import Store
from anime.subtitle_delivery import SubtitleDelivery
from anime.viewing_workflow import KEY, POLICY
from test_subtitle_delivery import Readiness

STAGED = {'first_release_mode': 'finished', 'editions': ['v0', 'v1']}


class ExactReadiness(Readiness):
    """Material refs name the exact completed media, as a finished receipt must prove."""
    def __call__(self, db, sid, ep, payload):
        value = super().__call__(db, sid, ep, payload)
        value['material_refs'] = [{'kind': 'completed_media', 'target': {'fingerprint': value['target_fingerprint'], 'size': 7}}]
        return value


@pytest.fixture
def staged(tmp_path, monkeypatch):
    db = Store(tmp_path / 'data' / 'test.db')
    db.set(KEY, POLICY)
    db.set('subtitle_delivery_policy', dict(STAGED))
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
    ready = ExactReadiness()
    scheduler = SubtitleDelivery(db, ready, lambda: now[0])
    scheduler.refresh()
    import anime.subtitle_finished_delivery as finished
    (tmp_path / 'release').mkdir()

    def verify_release(project, release, player_report, sid, ep, target):
        return {'release_id': f'synthetic-{sid}-{ep}-v0', 'edition': 'v0', 'delivery_status': 'finished', 'accepted': False,
                'formal_review_complete': False, 'manifest_sha256': 'f' * 64, 'release': str(tmp_path / 'release')}
    monkeypatch.setattr(finished, 'verify_release', verify_release)
    return db, scheduler, ready, now


def dispatched(scheduler, sid=42, ep=1, chat='original-episode-chat'):
    identity = scheduler.episode_id(sid, ep)
    row = scheduler.get(identity)
    lease = scheduler.reserve(identity, 'dispatcher', row['revision'])
    intent = scheduler.begin_dispatch(identity, 'dispatcher', lease['lease_token'], lease['revision'])
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id=chat)
    return identity


def order(scheduler):
    return [job['id'] for job in scheduler.snapshot()['jobs']]


def test_default_policy_keeps_the_v0_owner_on_the_slot_for_v1(staged):
    db, scheduler, ready, now = staged
    identity = dispatched(scheduler)
    first = scheduler.record_finished(identity, 'original-episode-chat', 'release', 'proof')
    assert first['continue_refinement'] and first['yielded_to'] is None
    assert first['state'] == 'production' and first['dispatch_state'] == 'acknowledged'
    other = scheduler.get('episode:43:1')
    with pytest.raises(ValueError, match='single production owner'):
        scheduler.reserve(other['id'], 'dispatcher', other['revision'])
    assert order(scheduler)[0] == identity  # pending v1 still ranks first


def test_v0_first_policy_yields_the_slot_to_another_show_and_queues_v1_behind_first_versions(staged):
    db, scheduler, ready, now = staged
    db.set('subtitle_delivery_policy', {**STAGED, 'v0_first_across_shows': True})
    identity = dispatched(scheduler)
    before = (db.rows('SELECT * FROM episodes'), db.rows('SELECT * FROM watches'))
    first = scheduler.record_finished(identity, 'original-episode-chat', 'release', 'proof')
    assert not first['continue_refinement'] and first['yielded_to'] == {'show_id': 43, 'episode': 1}
    assert first['state'] == 'finished' and first['dispatch_state'] == 'completed' and not scheduler.has_active(42, 1)
    assert db.get('subtitle_delivery_v0_42_1')['release_id'] == 'synthetic-42-1-v0'
    assert db.get('subtitle_delivery_finished_42_1')['edition'] == 'v0'
    assert scheduler.record_finished(identity, 'original-episode-chat', 'release', 'proof')['duplicate']
    scheduler.refresh()
    refinement = next(job for job in scheduler.snapshot()['jobs'] if job['kind'] == 'refinement')
    assert refinement['show_id'] == 42 and refinement['episode'] == 1 and refinement['state'] == 'ready'
    assert refinement['thread_id'] == 'original-episode-chat' and refinement['payload']['edition'] == 'v1'
    ranked = order(scheduler)
    assert ranked.index('episode:43:1') < ranked.index(refinement['id']) < ranked.index(identity)
    other = scheduler.get('episode:43:1')
    lease = scheduler.reserve(other['id'], 'dispatcher', other['revision'])  # the slot is free for the other show
    scheduler.release(other['id'], 'dispatcher', lease['revision'], lease['lease_token'])
    job = scheduler.get(refinement['id'])
    lease = scheduler.reserve(job['id'], 'dispatcher', job['revision'])
    intent = scheduler.begin_dispatch(job['id'], 'dispatcher', lease['lease_token'], lease['revision'])
    assert intent['action'] == 'reuse-thread' and intent['thread_id'] == 'original-episode-chat'
    assert before == (db.rows('SELECT * FROM episodes'), db.rows('SELECT * FROM watches'))


def test_v0_first_policy_continues_v1_when_no_other_show_is_waiting(staged):
    db, scheduler, ready, now = staged
    db.set('subtitle_delivery_policy', {**STAGED, 'v0_first_across_shows': True})
    for ep in (1, 2):  # the other show has nothing ready
        ready.values[(43, ep)] = {**ready(db, 43, ep, {}), 'material_ready': False, 'reasons': ['needs_verified_completed_media']}
    scheduler.refresh()
    identity = dispatched(scheduler)
    first = scheduler.record_finished(identity, 'original-episode-chat', 'release', 'proof')
    assert first['continue_refinement'] and first['yielded_to'] is None
    assert first['state'] == 'production' and first['dispatch_state'] == 'acknowledged'
    # The same show's next episode never counts as another show waiting.
    assert scheduler.get('episode:42:2')['state'] == 'ready'


def test_longest_unserved_show_goes_first_only_when_enabled(staged):
    db, scheduler, ready, now = staged
    assert order(scheduler) == ['episode:42:1', 'episode:43:1', 'episode:42:2', 'episode:43:2']
    db.execute("INSERT INTO subtitle_delivery_events(job_id,kind,payload,recorded) VALUES('episode:42:1','finished-observed','{}',?)", (now[0],))
    assert order(scheduler) == ['episode:42:1', 'episode:43:1', 'episode:42:2', 'episode:43:2']
    db.set('subtitle_delivery_policy', {**STAGED, 'queue_order': 'longest_unserved'})
    assert order(scheduler) == ['episode:43:1', 'episode:43:2', 'episode:42:1', 'episode:42:2']
    db.execute("INSERT INTO subtitle_delivery_events(job_id,kind,payload,recorded) VALUES('episode:43:2','finished-observed','{}',?)", (now[0] + 10,))
    assert order(scheduler) == ['episode:42:1', 'episode:42:2', 'episode:43:1', 'episode:43:2']


def test_priority_lane_show_goes_before_everything_unfinished(staged):
    db, scheduler, ready, now = staged
    from anime.subtitle_queue import production_queue
    db.set('subtitle_delivery_policy', {**STAGED, 'queue_order': 'longest_unserved', 'priority_show_ids': [43]})
    db.execute("INSERT INTO subtitle_delivery_events(job_id,kind,payload,recorded) VALUES('episode:43:2','finished-observed','{}',?)", (now[0],))
    assert order(scheduler) == ['episode:43:1', 'episode:43:2', 'episode:42:1', 'episode:42:2']  # served recently, still first
    queue = production_queue(db, now=now[0])
    lane = [(i['show_id'], i['episode'], i['position'], i['priority_lane']) for i in queue['items'] if i['status'] == 'queued']
    assert lane == [(43, 1, 1, True), (43, 2, 2, True), (42, 1, 3, False), (42, 2, 4, False)]
    db.set('subtitle_delivery_policy', {**STAGED, 'priority_show_ids': ['43']})  # only integer ids count
    assert order(scheduler) == ['episode:42:1', 'episode:43:1', 'episode:42:2', 'episode:43:2']
