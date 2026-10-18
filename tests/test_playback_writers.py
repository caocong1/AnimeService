"""Writer-v2 core only: fake clocks, synthetic media and fresh DBs; no production HTTP."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from anime.db import Store
from anime.playback_writers import PlaybackWriters, WriterConflict


@pytest.fixture
def core(tmp_path):
    db = Store(tmp_path / 'test.db')
    db.execute('CREATE TABLE web_progress(media_id TEXT PRIMARY KEY,position REAL,duration REAL,last_played REAL,updated REAL)')
    now, enabled = [1000.0], [True]
    media = {'ordinary': 'ordinary-key', 'alias': 'ordinary-key', 'v0': 'finished:sha-v0', 'v1': 'finished:sha-v1'}
    writers = PlaybackWriters(db, lambda key: media.get(key), enabled=lambda: enabled[0], clock=lambda: now[0])
    return db, writers, now, enabled


def claim(device='web-a', generation=0, mode='open', request=None, media='ordinary'):
    return {'media_key': media, 'session': device + '-session-123456789', 'request_id': request or device + '-request-123456789',
            'writer_token': device + '-token-123456789012345678901234567890', 'device_id': device,
            'expected_generation': generation, 'mode': mode}


def packet(c, generation=1, sequence=1, position=30):
    return {k: c[k] for k in ('session', 'writer_token')} | {'generation': generation, 'sequence': sequence,
            'position': position, 'duration': 1421, 'playing': True}


def test_read_does_not_claim_and_default_feature_is_off(core):
    db, w, now, enabled = core
    default = PlaybackWriters(db, w.resolve_media, clock=lambda: now[0])
    assert not default.capabilities()['enabled']
    assert default.state('ordinary')['generation'] == 0
    assert not db.rows('SELECT * FROM playback_writers')
    assert default.claim(claim())['reason'] == 'disabled'
    assert not db.rows('SELECT * FROM playback_writers') and not db.rows('SELECT * FROM web_progress')


def test_explicit_web_to_tv_takeover_fences_old_progress_heartbeat_and_release(core):
    db, w, now, enabled = core
    a, b = claim(), claim('tv-b', 1, 'takeover', request='tv-takeover-123456789')
    assert w.claim(a)['generation'] == 1
    assert w.update('ordinary', packet(a), action='progress')['ok']
    assert w.claim(claim('tv-b', 1))['reason'] == 'takeover_required'
    assert w.claim(b)['generation'] == 2
    before = db.rows('SELECT * FROM playback_writers')
    for action in ('progress', 'heartbeat', 'release'):
        assert w.update('ordinary', packet(a, sequence=99), action=action)['reason'] == 'fenced'
    assert db.rows('SELECT * FROM playback_writers') == before
    assert w.update('ordinary', packet(b, generation=2, position=90), action='progress')['ok']
    assert db.one('SELECT position FROM web_progress')['position'] == 90
    assert not db.rows('SELECT * FROM watches')


def test_simultaneous_expected_generation_cas_accepts_exactly_one(core):
    db, w, now, enabled = core
    barrier = Barrier(2)
    def run(device):
        barrier.wait(timeout=5)
        return w.claim(claim(device))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['web-a', 'tv-b']))
    assert sum(r['ok'] for r in results) == 1
    assert next(r for r in results if not r['ok'])['reason'] == 'generation_moved'
    assert w.state('ordinary')['generation'] == 1


def test_successful_replay_after_restart_and_takeover_returns_original_result(core):
    db, w, now, enabled = core
    a = claim(); first = w.claim(a)
    w.claim(claim('tv-b', 1, 'takeover'))
    restarted = PlaybackWriters(db, w.resolve_media, enabled=lambda: True, clock=lambda: now[0])
    assert restarted.claim(a) == {**first, 'replayed': True}
    assert restarted.state('ordinary')['generation'] == 2
    assert restarted.update('ordinary', packet(a))['reason'] == 'fenced'
    with pytest.raises(ValueError, match='reused'):
        restarted.claim({**a, 'device_id': 'different'})
    serialized = json.dumps(db.rows('SELECT * FROM playback_writers') + db.rows('SELECT * FROM playback_claim_receipts'))
    assert a['writer_token'] not in serialized and a['writer_token'] not in json.dumps(restarted.state('ordinary'))


def test_conflict_receipt_is_idempotent_and_takeover_needs_a_new_request_id(core):
    db, w, now, enabled = core
    w.claim(claim())
    b = claim('tv-b', 1); result = w.claim(b)
    assert result['reason'] == 'takeover_required'
    assert w.claim(b) == {**result, 'replayed': True}
    with pytest.raises(ValueError, match='reused'):
        w.claim({**b, 'mode': 'takeover'})
    assert w.claim({**b, 'mode': 'takeover', 'request_id': 'new-request-123456789'})['generation'] == 2


def test_expiry_and_release_retain_generation_and_never_restore_legacy(core):
    db, w, now, enabled = core
    a = claim(); w.claim(a)
    now[0] += 121
    assert w.update('ordinary', packet(a), action='progress')['reason'] == 'expired'
    assert w.state('ordinary')['generation'] == 1 and not w.state('ordinary')['active']
    b = claim('tv-b', 1); assert w.claim(b)['generation'] == 2
    assert w.update('ordinary', packet(b, 2), action='release')['ok']
    assert w.state('ordinary')['generation'] == 2 and not w.state('ordinary')['active']
    with db.connect() as c, pytest.raises(WriterConflict, match='session_required'):
        w.guard_legacy(c, 'ordinary')
    assert w.update('ordinary', packet(b, 2, 2))['reason'] == 'expired'


def test_receipt_retention_cannot_regrant_an_old_successful_claim(core):
    db, w, now, enabled = core
    a = claim(); w.claim(a)
    now[0] += 8 * 86400
    result = w.claim(a)
    assert not result['ok'] and result['reason'] == 'generation_moved'
    assert w.state('ordinary')['generation'] == 1 and not w.state('ordinary')['active']


def test_sequence_is_shared_by_progress_heartbeat_release_and_aliases(core):
    db, w, now, enabled = core
    a = claim(); w.claim(a)
    w.update('alias', packet(a, sequence=2), action='heartbeat')
    for action in ('progress', 'heartbeat', 'release'):
        assert w.update('ordinary', packet(a, sequence=1), action=action)['reason'] == 'out_of_order'
    assert not db.rows('SELECT * FROM web_progress')
    assert w.update('ordinary', packet(a, sequence=3), action='progress')['ok']
    assert w.update('alias', packet(a, sequence=3), action='release')['reason'] == 'out_of_order'
    assert w.state('ordinary')['active']


def test_versions_keep_independent_fences_and_progress(core):
    db, w, now, enabled = core
    a, b = claim(media='v0'), claim('tv-b', media='v1')
    w.claim(a); w.claim(b)
    w.update('v0', packet(a, position=100), action='progress')
    w.update('v1', packet(b, position=200), action='progress')
    assert {r['media_id']: r['position'] for r in db.rows('SELECT * FROM web_progress')} == {'finished:sha-v0': 100, 'finished:sha-v1': 200}
    assert w.update('v1', packet(a, sequence=2), action='progress')['reason'] == 'fenced'


def test_disabled_rollback_rejects_v2_and_keeps_legacy_fence_on_claimed_keys(core):
    db, w, now, enabled = core
    a = claim(); w.claim(a); enabled[0] = False
    assert w.update('ordinary', packet(a), action='progress')['reason'] == 'disabled'
    now[0] += 1000
    with db.connect() as c, pytest.raises(WriterConflict, match='session_required'):
        w.guard_legacy(c, 'ordinary')
    with db.connect() as c:
        w.guard_legacy(c, 'v1')  # never fenced, feature off
    assert not db.rows('SELECT * FROM web_progress')


def test_enabled_legacy_guard_blocks_even_before_the_first_claim(core):
    db, w, now, enabled = core
    with db.connect() as c, pytest.raises(WriterConflict, match='session_required'):
        w.guard_legacy(c, 'ordinary')
    assert not db.rows('SELECT * FROM playback_writers')


def test_canonical_alias_replay_and_cross_media_request_reuse(core):
    db, w, now, enabled = core
    a = claim(); first = w.claim(a)
    assert w.claim({**a, 'media_key': 'alias'}) == {**first, 'replayed': True}
    with pytest.raises(ValueError, match='reused'): w.claim({**a, 'media_key': 'v1'})
    assert not w.state('v1')['active']


def test_fault_between_fence_and_progress_rolls_back_both(core):
    db, w, now, enabled = core
    a = claim(); w.claim(a)
    db.execute("CREATE TRIGGER reject_progress BEFORE INSERT ON web_progress BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    before = db.rows('SELECT * FROM playback_writers')
    with pytest.raises(Exception, match='synthetic failure'):
        w.update('ordinary', packet(a), action='progress')
    assert db.rows('SELECT * FROM playback_writers') == before and not db.rows('SELECT * FROM web_progress')


def test_fault_between_claim_and_receipt_rolls_back_generation(core):
    db, w, now, enabled = core
    db.execute("CREATE TRIGGER reject_claim BEFORE INSERT ON playback_claim_receipts BEGIN SELECT RAISE(ABORT,'claim failed'); END")
    with pytest.raises(Exception, match='claim failed'): w.claim(claim())
    assert w.state('ordinary')['generation'] == 0
    assert not db.rows('SELECT * FROM playback_claim_receipts')


def test_same_claim_race_returns_one_original_result(core):
    db, w, now, enabled = core
    barrier = Barrier(2)
    def run(_):
        barrier.wait(timeout=5)
        return w.claim(claim())
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b = list(pool.map(run, range(2)))
    assert a['generation'] == b['generation'] == 1
    assert sum(r.get('replayed', False) for r in (a,b)) == 1
    assert len(db.rows('SELECT * FROM playback_claim_receipts')) == 1


@pytest.mark.parametrize('change', [{'expected_generation': True}, {'expected_generation': -1}, {'writer_token': 'short'},
                                    {'session': 'short'}, {'device_id': 'bad\nlabel'}, {'mode': 'automatic'}, {'media_key': 'unknown'}])
def test_malformed_claim_never_mutates_a_fence(core, change):
    db, w, now, enabled = core
    with pytest.raises(ValueError): w.claim({**claim(), **change})
    assert not db.rows('SELECT * FROM playback_writers')


@pytest.mark.parametrize('change', [{'position': True}, {'duration': float('nan')}, {'position': -1}, {'sequence': False},
                                    {'generation': 0}, {'position': 1500}])
def test_malformed_progress_never_mutates_fence_or_position(core, change):
    db, w, now, enabled = core
    a = claim(); w.claim(a); before = db.rows('SELECT * FROM playback_writers')
    with pytest.raises(ValueError): w.update('ordinary', {**packet(a), **change}, action='progress')
    assert db.rows('SELECT * FROM playback_writers') == before and not db.rows('SELECT * FROM web_progress')
