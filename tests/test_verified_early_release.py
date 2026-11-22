"""Official early streaming can precede catalog broadcast without weakening gates."""
import datetime
import hashlib

import pytest

from anime.subtitle_queue import production_queue
from anime.viewing_workflow import unreleased_date
from test_subtitle_delivery import delivery  # noqa: F401


@pytest.fixture
def early(delivery):
    db, scheduler, ready, now = delivery
    now[0] = datetime.datetime.fromisoformat('2026-11-01T12:00:00+08:00').timestamp()
    db.set('airing_sort_offset_42', 13)
    db.execute("INSERT INTO airings VALUES(42,15,'2026-11-07','broadcast local E2')")
    archive = db.path.parent / 'official-onair.html'
    archive.write_text('Official provider: episode two streams a week before broadcast.', encoding='utf-8')
    proof = {'version': 1, 'show_id': 42, 'episode': 2, 'season': 1,
             'source_episode': 15, 'catalog_airdate': '2026-11-07',
             'released_at': '2026-10-31T22:00:00+09:00',
             'source_url': 'https://official.example/onair/',
             'evidence': {'path': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}}
    db.set('verified_early_release_42_2', proof)
    return db, scheduler, now, proof, archive


def test_official_early_release_flows_through_queue_refresh_and_reserve(early):
    db, scheduler, now, proof, archive = early
    before = {t: db.rows(f'SELECT * FROM {t}') for t in ('shows', 'airings', 'episodes', 'watches', 'tasks')}
    assert unreleased_date(db, 42, 2, now[0]) == ''
    scheduler.refresh()
    row = scheduler.get('episode:42:2')
    item = next(i for i in production_queue(db, now=now[0])['items'] if (i['show_id'], i['episode']) == (42, 2))
    assert item['status'] == 'queued'
    assert scheduler.reserve(row['id'], 'test-owner', row['revision'])['lease_token']
    assert all(db.rows(f'SELECT * FROM {t}') == rows for t, rows in before.items())
    assert not scheduler.get(row['id'])['thread_id']  # reserve did not dispatch a chat
    assert unreleased_date(db, 43, 2, now[0]) == ''  # other shows are unaffected


def test_no_proof_download_and_filename_do_not_override_catalog(early):
    db, scheduler, now, proof, archive = early
    db.execute("DELETE FROM kv WHERE key='verified_early_release_42_2'")
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(42,2,'formal-S01E02.mkv','complete',100)")
    assert unreleased_date(db, 42, 2, now[0]) == '2026-11-07'
    row = scheduler.get('episode:42:2')
    with pytest.raises(ValueError, match='not yet released'):
        scheduler.reserve(row['id'], 'owner', row['revision'])
    db.execute("DELETE FROM subtitle_delivery_jobs WHERE id='episode:42:2'")
    scheduler.refresh()
    assert not db.one("SELECT id FROM subtitle_delivery_jobs WHERE id='episode:42:2'")


@pytest.mark.parametrize('field,value', [
    ('show_id', 43), ('episode', 1), ('season', 2), ('source_episode', 2),
    ('catalog_airdate', '2026-11-14'), ('source_url', 'not-a-source'),
    ('released_at', '2026-10-31T22:00:00'), ('released_at', '2026-11-08T22:00:00+09:00'),
    ('evidence', {'path': 'missing.html', 'sha256': '0' * 64}),
    ('evidence', {'path': '../outside.html', 'sha256': '0' * 64}),
])
def test_unbound_or_invalid_proof_cannot_bypass_gate(early, field, value):
    db, scheduler, now, proof, archive = early
    db.set('verified_early_release_42_2', {**proof, field: value})
    assert unreleased_date(db, 42, 2, now[0]) == '2026-11-07'


def test_evidence_tampering_and_catalog_or_mapping_changes_require_reverification(early):
    db, scheduler, now, proof, archive = early
    archive.write_text('changed', encoding='utf-8')
    assert unreleased_date(db, 42, 2, now[0]) == '2026-11-07'
    db.set('verified_early_release_42_2', {**proof, 'evidence': {
        'path': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}})
    db.execute("UPDATE airings SET airdate='2026-11-14' WHERE show_id=42 AND episode=15")
    assert unreleased_date(db, 42, 2, now[0]) == '2026-11-14'


def test_official_release_still_waits_until_the_announced_instant(early):
    db, scheduler, now, proof, archive = early
    release = datetime.datetime.fromisoformat(proof['released_at']).timestamp()
    assert unreleased_date(db, 42, 2, release - 1) == '2026-10-31'
    assert unreleased_date(db, 42, 2, release) == ''
    db.execute("INSERT INTO airings VALUES(42,16,'2026-11-14','local E3')")
    assert unreleased_date(db, 42, 3, now[0]) == '2026-11-14'


def test_begin_rechecks_evidence_after_reserve(early):
    db, scheduler, now, proof, archive = early
    row = scheduler.get('episode:42:2')
    lease = scheduler.reserve(row['id'], 'owner', row['revision'])
    archive.write_text('changed after reserve', encoding='utf-8')
    with pytest.raises(ValueError, match='not yet released'):
        scheduler.begin_dispatch(row['id'], 'owner', lease['lease_token'], lease['revision'])


@pytest.mark.parametrize('gate', ['watched', 'scope'])
def test_release_fact_cannot_bypass_watch_or_scope(early, gate):
    db, scheduler, now, proof, archive = early
    if gate == 'watched':
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    else:
        db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    row = scheduler.get('episode:42:2')
    with pytest.raises(ValueError):
        scheduler.reserve(row['id'], 'owner', row['revision'])
