"""Official dates advance independently of TV dates, without production grants."""
import datetime as dt
import hashlib
import json

import pytest

from anime.official_releases import (digest, register_schedule, review_plan,
                                     record_review, reconcile_released_facts)
from anime.viewing_workflow import unreleased_date
from test_subtitle_delivery import delivery  # noqa: F401


@pytest.fixture
def official(delivery):
    db, scheduler, ready, now = delivery
    now[0] = dt.datetime.fromisoformat('2026-11-01T12:00:00+08:00').timestamp()
    db.set('airing_sort_offset_42', 13)
    for ep, date in ((15, '2026-11-07'), (16, '2026-11-14')):
        db.execute('INSERT INTO airings VALUES(42,?,?,?)', (ep, date, 'TV catalog'))
    evidence = db.path.parent / 'official.html'
    evidence.write_text('Official weekly streaming; first two episodes simultaneously.')
    record = {'version': 1, 'show_id': 42, 'season': 1, 'original': db.show(42)['original'],
              'mapping_sha256': digest(db.mapping(42)), 'episode_start': 2, 'episode_end': 3,
              'interval_days': 7, 'catalog_anchor': {'episode': 2, 'source_episode': 15, 'date': '2026-11-07'},
              'stream_anchor_at': '2026-11-01T00:00:00+09:00',
              'verified_at': now[0] - 86400, 'valid_until': now[0] + 6 * 86400,
              'source_url': 'https://official.example/onair/',
              'evidence': {'path': evidence.name, 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}}
    return db, scheduler, now, record, evidence


def test_plan_apply_idempotence_and_exact_midnight_release(official):
    db, scheduler, now, record, evidence = official
    before = {t: db.rows(f'SELECT * FROM {t}') for t in ('shows', 'airings', 'episodes', 'watches', 'tasks', 'subtitle_delivery_jobs')}
    assert register_schedule(db, record, now[0])['changed']
    assert db.get('official_release_schedule_42') is None
    assert register_schedule(db, record, now[0], apply=True)['applied']
    assert not register_schedule(db, record, now[0], apply=True)['changed']
    instant = dt.datetime.fromisoformat(record['stream_anchor_at']).timestamp()
    assert unreleased_date(db, 42, 2, instant - 1) == '2026-10-31'
    assert unreleased_date(db, 42, 2, instant) == ''
    assert unreleased_date(db, 42, 3, now[0]) == '2026-11-07'
    assert all(db.rows(f'SELECT * FROM {t}') == value for t, value in before.items())
    scheduler.refresh()
    assert db.get('verified_early_release_42_2')['schedule_sha256'] == digest(record)
    row = scheduler.get('episode:42:2')
    assert row['state'] == 'ready'
    assert scheduler.reserve(row['id'], 'same-original-dispatcher', row['revision'])['lease_token']
    assert not scheduler.get(row['id'])['thread_id']


@pytest.mark.parametrize('change', ['season', 'mapping', 'offset', 'catalog', 'evidence', 'identity', 'expiry'])
def test_changed_facts_cannot_bypass_release_gate(official, change):
    db, scheduler, now, record, evidence = official
    register_schedule(db, record, now[0], apply=True)
    if change in ('season', 'mapping'):
        mapping = db.mapping(42)
        mapping['season' if change == 'season' else 'end'] = 2
        db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    elif change == 'offset':
        db.set('airing_sort_offset_42', 0)
    elif change == 'catalog':
        db.execute("UPDATE airings SET airdate='2026-11-08' WHERE show_id=42 AND episode=15")
    elif change == 'evidence':
        evidence.write_text('tampered')
    elif change == 'identity':
        db.execute("UPDATE shows SET original='another season' WHERE id=42")
    elif change == 'expiry':
        now[0] = record['valid_until']
    # Offset changes move the old raw row to E15; unrelated E2 remains unknown.
    ep = 15 if change == 'offset' else 2
    if change == 'expiry':
        assert unreleased_date(db, 42, 3, now[0]) == '2026-11-14'
    else:
        assert unreleased_date(db, 42, ep, now[0]) == ('2026-11-08' if change == 'catalog' else '2026-11-07')


def test_reverification_requires_expected_sha_and_archives_old_schedule(official):
    db, scheduler, now, record, evidence = official
    register_schedule(db, record, now[0], apply=True)
    new = {**record, 'verified_at': now[0] + 1, 'valid_until': now[0] + 7 * 86400 + 1}
    with pytest.raises(ValueError, match='expected SHA'):
        register_schedule(db, new, now[0] + 1, apply=True)
    register_schedule(db, new, now[0] + 1, apply=True, expected_sha=digest(record))
    assert db.get('official_release_schedule_42_history_' + digest(record)) == record


def test_refresh_retains_elapsed_facts_but_not_future_or_watched_episodes(official):
    db, scheduler, now, record, evidence = official
    register_schedule(db, record, now[0], apply=True)
    before = {t: db.rows(f'SELECT * FROM {t}') for t in ('shows', 'airings', 'episodes', 'watches', 'tasks', 'subtitle_delivery_jobs')}
    assert reconcile_released_facts(db, now[0]) == 1
    assert reconcile_released_facts(db, now[0]) == 0
    assert db.get('verified_early_release_42_3') is None
    assert unreleased_date(db, 42, 2, record['valid_until']) == ''
    assert all(db.rows(f'SELECT * FROM {t}') == value for t, value in before.items())
    db.execute('DELETE FROM kv WHERE key=?', ('verified_early_release_42_2',))
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    assert reconcile_released_facts(db, now[0]) == 0


def add_media(db):
    path = db.path.parent / 'S01E02.mkv'
    path.write_bytes(b'actual complete media')
    db.execute('INSERT OR REPLACE INTO episodes(show_id,episode,path,status,size) VALUES(42,2,?,\'complete\',?)', (str(path), path.stat().st_size))
    return path


def test_conflict_without_job_in_trial_is_reviewed_without_enrolling_production(official):
    db, scheduler, now, record, evidence = official
    path = add_media(db)
    db.execute('DELETE FROM subtitle_delivery_jobs WHERE show_id=42')
    db.execute('DELETE FROM subtitle_studio_jobs WHERE show_id=42')
    db.execute("UPDATE shows SET state='trial' WHERE id=42")
    row = review_plan(db, now[0])['next_action']
    assert (row['show_id'], row['episode'], row['action']) == (42, 2, 'verify_official_release')
    assert row['production_scope_blocker']
    register_schedule(db, record, now[0], apply=True)
    assert review_plan(db, now[0])['next_action'] is None
    assert db.show(42)['state'] == 'trial'
    assert not db.rows('SELECT * FROM subtitle_delivery_jobs WHERE show_id=42')


def test_future_official_time_waits_and_expiry_requests_reverification_without_media(official):
    db, scheduler, now, record, evidence = official
    record['stream_anchor_at'] = '2026-11-01T22:00:00+09:00'
    add_media(db)
    register_schedule(db, record, now[0], apply=True)
    plan = review_plan(db, now[0])
    assert plan['summary']['awaiting_official_release'] == 1
    assert plan['next_action'] is None
    row = review_plan(db, record['valid_until'] - 86400)['next_action']
    assert row['action'] == 'revalidate_official_schedule'


def test_review_receipt_cooldown_idempotence_and_new_media_condition(official):
    db, scheduler, now, record, evidence = official
    media = add_media(db)
    row = review_plan(db, now[0])['next_action']
    report = {'show_id': 42, 'episode': 2, 'condition': row['condition'],
              'outcome': 'source_unavailable', 'evidence': record['evidence']}
    assert not record_review(db, report, now[0])['applied']
    result = record_review(db, report, now[0], apply=True)
    assert result['review']['attempts'] == 1
    assert record_review(db, report, now[0] + 1, apply=True)['idempotent']
    assert review_plan(db, now[0] + 1)['summary']['retry_wait'] == 1
    with pytest.raises(ValueError, match='cooling down'):
        record_review(db, {**report, 'outcome': 'not_formal'}, now[0] + 1, apply=True)
    media.write_bytes(b'new complete media')
    db.execute('UPDATE episodes SET size=? WHERE show_id=42 AND episode=2', (media.stat().st_size,))
    changed = review_plan(db, now[0] + 2)['next_action']
    assert changed['condition'] != row['condition']
    assert db.get('official_release_review_42_2_history_' + digest(report)) == result['review']


def test_watched_and_missing_files_do_not_create_conflicts(official):
    db, scheduler, now, record, evidence = official
    media = add_media(db)
    media.unlink()
    assert review_plan(db, now[0])['next_action'] is None
    add_media(db)
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    assert review_plan(db, now[0])['next_action'] is None


def test_broken_source_url_or_naive_time_rejected(official):
    db, scheduler, now, record, evidence = official
    for field, value in [('source_url', 'http://unverified/'), ('stream_anchor_at', '2026-11-01T00:00:00')]:
        with pytest.raises(ValueError):
            register_schedule(db, {**record, field: value}, now[0], apply=True)


def test_mapping_changed_between_validation_and_apply_is_rejected(official, monkeypatch):
    import anime.official_releases as module
    db, scheduler, now, record, evidence = official
    original = module.validate_schedule
    def changed(db, record, stamp, **kwargs):
        result = original(db, record, stamp, **kwargs)
        db.set('airing_sort_offset_42', 0)
        return result
    monkeypatch.setattr(module, 'validate_schedule', changed)
    with pytest.raises(ValueError, match='changed before apply'):
        register_schedule(db, record, now[0], apply=True)
    assert db.get('official_release_schedule_42') is None


def test_schedule_for_another_show_cannot_change_this_episode(official):
    db, scheduler, now, record, evidence = official
    db.set('official_release_schedule_43', record)
    db.set('airing_sort_offset_43', 13)
    db.execute("INSERT INTO airings VALUES(43,15,'2026-11-07','same TV date, other show')")
    assert unreleased_date(db, 43, 2, now[0]) == '2026-11-07'


def test_schedule_revalidation_failure_cools_down_and_catalog_change_is_new_condition(official):
    db, scheduler, now, record, evidence = official
    register_schedule(db, record, now[0], apply=True)
    stamp = record['valid_until'] - 86400
    row = review_plan(db, stamp)['next_action']
    report = {'show_id': 42, 'episode': None, 'condition': row['condition'],
              'outcome': 'source_unavailable', 'evidence': record['evidence']}
    record_review(db, report, stamp, apply=True)
    assert review_plan(db, stamp + 1)['summary']['retry_wait'] == 1
    assert review_plan(db, stamp + 1)['next_action'] is None
    db.execute("UPDATE airings SET airdate='2026-11-15' WHERE show_id=42 AND episode=16")
    assert review_plan(db, stamp + 2)['next_action']['condition'] != row['condition']


def test_viewer_queue_keeps_date_gate_without_running_full_media_audit(official, monkeypatch):
    from anime.subtitle_queue import production_queue
    import anime.official_releases as module
    db, scheduler, now, record, evidence = official
    add_media(db)
    calls = []
    def audit(*args, **kwargs):
        calls.append(True)
        return {'items': [], 'next_action': None, 'summary': {}}
    monkeypatch.setattr(module, 'review_plan', audit)
    queue = production_queue(db, now=now[0])
    assert not calls
    row = next(i for i in queue['items'] if (i['show_id'], i['episode']) == (42, 2))
    assert row['status'] == 'unreleased'
    assert scheduler.snapshot()['recovery']['official_release_review']['items'] == []
    assert calls == [True]
