"""Synthetic OCR intake fixtures; no real media, DB, download or acceptance."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from test_subtitle_delivery import delivery
from anime.subtitle_chinese import chinese_preparation
from anime.subtitle_chinese_intake import register_chinese_candidate
from anime.subtitle_lab import lab_core
from anime.subtitle_studio import archive_for


pytestmark = pytest.mark.requires_lab


def ref(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return ref(path)


def intake(delivery, review=True):
    db, scheduler, *_ = delivery
    db.set('subtitle_studio_enabled', True)
    media = db.path.parent / 'source.mkv'
    media.write_bytes(b'Original synthetic completed source, no actual video.')
    selected = {'index': 0, 'name': media.name, 'size': media.stat().st_size}
    selection = {'selected': {'2': selected}, 'ids': [0], 'files': [selected]}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose) VALUES(?,42,'[FixtureGroup] Example S01E02','complete',?,?,'subtitle_sample')",
               ('a' * 40, json.dumps(selection), str(media.parent)))
    root = db.path.parent / 'subtitle-studio'
    root.mkdir(exist_ok=True)
    subtitle = root / 'corrected.srt'
    subtitle.write_text('1\n00:00:01,000 --> 00:00:02,000\n明天再見。\n', encoding='utf-8')
    raw_ocr = write_json(root / 'ocr.json', [{'start': 1, 'end': 2, 'text': '明天再見'}])
    from test_subtitle_delivery_integration import synthetic_png
    frame = root / 'frame.png'
    frame.write_bytes(synthetic_png(40, 18))
    expected = scheduler.get(scheduler.episode_id(42, 2))['input_key']
    source = {'hash': 'a' * 40, 'path': str(media), 'size': media.stat().st_size,
              'fingerprint': lab_core().fingerprint(media), 'sha256': ref(media)['sha256']}
    request = {'schema': 'chinese-source-candidate-request/v1', 'show_id': 42, 'season': 1,
               'episode': 2, 'expected_input': expected, 'source': source,
               'subtitle': ref(subtitle), 'raw_ocr': raw_ocr, 'frame_evidence': [ref(frame)]}
    if review:
        report = {'schema': 'chinese-dialogue-review/v1', 'reviewer_kind': 'codex',
                  'show_id': 42, 'season': 1, 'episode': 2, 'corrected_subtitle': ref(subtitle),
                  'source_media_fingerprint': source['fingerprint'],
                  'source_hardtext_review': {'status': 'complete'},
                  'whole_episode_review': {'status': 'partial',
                    'known_Japanese_reference_items_without_verified_Chinese_mapping': 2},
                  'target_alignment_verified': False, 'ready': False}
        request['review_report'] = write_json(root / 'review.partial.json', report)
    path = root / 'request.json'
    write_json(path, request)
    return db, expected, path, request


def main_facts(db):
    tables = db.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return {r['name']: db.rows('SELECT * FROM ' + r['name'] + ' ORDER BY rowid') for r in tables}


def run(db, expected, path, **kwargs):
    return register_chinese_candidate(db, 42, 2, path, expected, **kwargs)


def test_plan_does_not_create_archive_or_write_production_database(delivery):
    db, expected, path, request = intake(delivery)
    before = main_facts(db)
    root = db.path.parent / 'subtitle-studio'
    result = run(db, expected, path)
    assert not result['applied'] and result['draft_id'] is None
    assert result['cue_count'] == 1 and result['next_action'] == 'align_chinese_source'
    assert not (root / 'archive.sqlite3').exists() and not (root / 'candidate-intake').exists()
    assert main_facts(db) == before
    assert Path(request['source']['path']).read_bytes().startswith(b'Original synthetic')


@pytest.mark.parametrize('review', [True, False])
def test_apply_preserves_originals_and_appends_only_an_unqualified_candidate(delivery, review):
    db, expected, path, request = intake(delivery, review=review)
    before = main_facts(db)
    raw = Path(request['subtitle']['path']).read_bytes()
    result = run(db, expected, path, apply=True)
    archived = archive_for(db).draft(result['draft_id'])
    assert archived['raw_hash'] == request['subtitle']['sha256']
    assert archived['metadata']['candidate_not_reliable_base'] and archived['metadata']['timing_is_estimated']
    assert archived['metadata']['media_fingerprint'] == request['source']['fingerprint']
    assert archived['metadata']['source_media_sha256'] == request['source']['sha256']
    assert not any(result[k] for k in ('ready', 'accepted', 'finished', 'target_alignment_qualified', 'production_db_written'))
    assert Path(request['subtitle']['path']).read_bytes() == raw and main_facts(db) == before
    plan = chinese_preparation(db, 42, 2, {'fingerprint': 'different-clean-target'})
    assert plan['reliable_text_count'] == 0 and not plan['production_ready_claimed']
    assert plan['action'] == ('align_chinese_source' if review else 'review_chinese_ocr')
    if review:
        assert plan['candidates'][0]['correction_progress']['pending_reference_items'] == 2


def test_same_receipt_is_idempotent_and_does_not_overwrite_previous_draft(delivery):
    db, expected, path, request = intake(delivery)
    original_id = archive_for(db).add_base(42, 1, 2, request['subtitle']['path'], {
        'language': 'chi', 'source_key': request['source']['hash'], 'track': 'original',
        'media_fingerprint': request['source']['fingerprint'], 'timing_is_estimated': True,
        'candidate_not_reliable_base': True})
    original = archive_for(db).draft(original_id)
    request['source_draft_id'] = original_id
    report = json.loads(Path(request['review_report']['path']).read_text(encoding='utf-8'))
    report['source_draft_id'] = original_id
    request['review_report'] = write_json(Path(request['review_report']['path']), report)
    write_json(path, request)
    first = run(db, expected, path, apply=True)
    second = run(db, expected, path, apply=True)
    assert first['draft_id'] == second['draft_id'] != original_id
    assert archive_for(db).draft(original_id) == original
    assert len(archive_for(db).bases(42, 1, 2)) == 2


@pytest.mark.parametrize('change', [
    'unauthorized', 'unselected', 'wrong_season', 'out_of_bounds', 'unwatched_scope',
    'watched', 'unreleased', 'disabled', 'policy_hold', 'wrong_input', 'owner',
    'lease', 'lease_token', 'dispatch_unknown', 'dispatch_committing', 'user_stopped',
    'owner_stopped', 'finished_job', 'accepted_build', 'finished_ledger', 'viewing_lease',
    'studio_writer', 'source_incomplete', 'source_uncertain', 'source_other_episode',
    'download_unknown', 'download_unknown_without_committing', 'dialog_unknown', 'banned_publisher'])
def test_intake_rejects_protected_scope_and_changed_receipts_before_archive(delivery, change):
    db, expected, path, request = intake(delivery)
    job = ('episode:42:2',)
    if change == 'unauthorized': db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif change == 'unselected': db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif change == 'unwatched_scope': db.execute("UPDATE shows SET state='paused' WHERE id=42")
    elif change in ('wrong_season', 'out_of_bounds'):
        mapping = db.mapping(42)
        mapping['season' if change == 'wrong_season' else 'end'] = 3 if change == 'wrong_season' else 1
        db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    elif change == 'watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    elif change == 'unreleased': db.execute("INSERT INTO airings(show_id,episode,airdate) VALUES(42,2,'2999-01-01')")
    elif change == 'disabled': db.set('subtitle_studio_enabled', False)
    elif change == 'policy_hold': db.execute('UPDATE source_policies SET config=? WHERE show_id=42', (json.dumps({'mode': 'hold'}),))
    elif change == 'wrong_input': expected = '0' * 64
    elif change == 'owner': db.execute("UPDATE subtitle_delivery_jobs SET thread_id='original-owner' WHERE id=?", job)
    elif change == 'lease': db.execute('UPDATE subtitle_delivery_jobs SET lease_expires=? WHERE id=?', (time.time() + 600, *job))
    elif change == 'lease_token': db.execute("UPDATE subtitle_delivery_jobs SET lease_token='protected-token' WHERE id=?", job)
    elif change.startswith('dispatch_'):
        db.execute('UPDATE subtitle_delivery_jobs SET dispatch_state=? WHERE id=?', (change[9:], *job))
    elif change in ('user_stopped', 'owner_stopped'):
        db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?', (json.dumps({
            'user_stopped' if change == 'user_stopped' else 'owner_stopped_writing': True}), *job))
    elif change == 'finished_job': db.execute("UPDATE subtitle_delivery_jobs SET state='finished' WHERE id=?", job)
    elif change == 'accepted_build': db.execute("UPDATE subtitle_delivery_jobs SET accepted_build_id='original-build' WHERE id=?", job)
    elif change == 'finished_ledger': db.set('subtitle_delivery_finished_42_2', {'release_id': 'old-finished'})
    elif change == 'viewing_lease':
        db.execute('CREATE TABLE subtitle_viewing_leases(show_id INTEGER,episode INTEGER,expires REAL)')
        db.execute('INSERT INTO subtitle_viewing_leases VALUES(42,2,?)', (time.time() + 600,))
    elif change == 'studio_writer': db.execute("UPDATE subtitle_studio_jobs SET state='production' WHERE show_id=42 AND episode=2")
    elif change.startswith('source_'):
        if change == 'source_other_episode':
            task = db.one('SELECT selection FROM tasks')
            selected = json.loads(task['selection'])
            selected['selected']['3'] = selected['selected'].pop('2')
            db.execute('UPDATE tasks SET selection=?', (json.dumps(selected),))
        else:
            db.execute('UPDATE tasks SET status=?', ('downloading' if change == 'source_incomplete' else 'uncertain',))
    elif change.startswith('download_unknown'):
        db.execute("INSERT INTO thunder_queue(hash,state,committing) VALUES(?,'unknown',?)",
                   (request['source']['hash'], 1 if change == 'download_unknown' else 0))
    elif change == 'dialog_unknown': db.set('thunder_dialog_hold_' + request['source']['hash'], {'unconfirmed': True})
    elif change == 'banned_publisher':
        db.set('source_publisher_blacklist', [{'publisher': 'FixtureGroup', 'aliases': ['FixtureGroup'], 'reason': 'Synthetic exclusion'}])
    before = main_facts(db)
    with pytest.raises((ValueError, OSError)):
        run(db, expected, path, apply=True)
    assert main_facts(db) == before and not (path.parent / 'archive.sqlite3').exists()


@pytest.mark.parametrize('change', ['subtitle_sha', 'raw_ocr_sha', 'frame_sha', 'source_sha',
                                    'source_path', 'source_size', 'source_fingerprint',
                                    'review_identity', 'review_fingerprint', 'review_subtitle',
                                    'review_status', 'outside_private', 'empty_cues'])
def test_intake_rejects_unbound_actual_bytes_or_report(delivery, change):
    db, expected, path, request = intake(delivery)
    if change in ('subtitle_sha', 'raw_ocr_sha'):
        request['subtitle' if change == 'subtitle_sha' else 'raw_ocr']['sha256'] = '0' * 64
    elif change == 'frame_sha': request['frame_evidence'][0]['sha256'] = '0' * 64
    elif change == 'source_sha': request['source']['sha256'] = '0' * 64
    elif change == 'source_path': request['source']['path'] = str(path)
    elif change == 'source_size': request['source']['size'] += 1
    elif change == 'source_fingerprint': request['source']['fingerprint'] = '0' * 64
    elif change.startswith('review_'):
        p = Path(request['review_report']['path'])
        report = json.loads(p.read_text(encoding='utf-8'))
        if change == 'review_identity': report['episode'] = 3
        elif change == 'review_fingerprint': report['source_media_fingerprint'] = 'wrong'
        elif change == 'review_subtitle': report['corrected_subtitle']['sha256'] = '0' * 64
        else: report['source_hardtext_review']['status'] = 'partial'
        request['review_report'] = write_json(p, report)
    elif change == 'outside_private': request['raw_ocr'] = ref(Path(request['source']['path']))
    elif change == 'empty_cues':
        p = Path(request['subtitle']['path'])
        p.write_text('', encoding='utf-8')
        request['subtitle'] = ref(p)
    write_json(path, request)
    with pytest.raises((ValueError, OSError)):
        run(db, expected, path, apply=True)
    assert not (path.parent / 'archive.sqlite3').exists()


@pytest.mark.parametrize('status', ['complete', 'recovered_cues_corrected'])
def test_actual_private_preparation_report_can_be_archived_without_claiming_full_coverage(delivery, status):
    db, expected, path, request = intake(delivery, review=False)
    report = {'schema': 'private-chinese-hardtext-preparation/v1', 'reviewer_kind': 'codex',
              'show_id': 42, 'season': 1, 'episode': 2, 'corrected_subtitle': request['subtitle'],
              'source': {'fingerprint': request['source']['fingerprint'], 'sha256': request['source']['sha256']},
              'source_hardtext_review': {'status': status, 'whole_episode_no_omissions_claim': False},
              'known_limits': ['Synthetic unreviewed Japanese/audio coverage'], 'production_ready_claimed': False}
    request['review_report'] = write_json(path.parent / 'review.private.json', report)
    write_json(path, request)
    result = run(db, expected, path, apply=True)
    plan = chinese_preparation(db, 42, 2, {'fingerprint': 'clean-target'})
    assert result['next_action'] == plan['action'] == 'align_chinese_source'
    assert plan['reliable_text_count'] == 0 and not result['target_alignment_qualified']
    assert 'review_chinese_ocr' not in plan['actions']
    assert plan['candidates'][0]['correction_progress']['source_hardtext_review'] == status
    assert not plan['production_ready_claimed']


def test_transaction_rechecks_a_new_watch_after_the_expensive_source_hash(delivery, monkeypatch):
    db, expected, path, _ = intake(delivery)
    import anime.subtitle_chinese_intake as module
    original, calls = module._source, []
    def racing_source(*args):
        result = original(*args)
        calls.append(True)
        return result
    monkeypatch.setattr(module, '_source', racing_source)
    original_ref = module._ref
    def racing_ref(*args):
        value = original_ref(*args)
        if len(calls) == 1 and Path(args[1]['path']).name == 'corrected.srt':
            db.execute('INSERT OR IGNORE INTO watches(show_id,episode,finished) VALUES(42,2,1)')
        return value
    monkeypatch.setattr(module, '_ref', racing_ref)
    with pytest.raises(ValueError, match='Watched'):
        run(db, expected, path, apply=True)
    assert not (path.parent / 'archive.sqlite3').exists()


def test_changed_checkpoint_is_not_used_for_routing(delivery):
    db, expected, path, _ = intake(delivery)
    result = run(db, expected, path, apply=True)
    Path(result['checkpoint']['path']).write_text('{}', encoding='utf-8')
    assert chinese_preparation(db, 42, 2)['action'] == 'review_chinese_ocr'


def test_stale_reader_cannot_hide_a_new_watch(delivery):
    db, expected, path, _ = intake(delivery)
    from scripts.subtitle_delivery_queue import ReadStore
    stale = ReadStore(db.path)
    try:
        assert not stale.one('SELECT 1 FROM watches WHERE show_id=42 AND episode=2')
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
        with pytest.raises(ValueError, match='Watched'):
            run(stale, expected, path, apply=True)
    finally:
        stale.con.close()
    assert not (path.parent / 'archive.sqlite3').exists()


def test_owner_claim_is_locked_out_during_archive_append(delivery, monkeypatch):
    db, expected, path, _ = intake(delivery)
    core, observations = lab_core(), []
    original = core.Archive.add_base
    def append(self, *args, **kwargs):
        with sqlite3.connect(db.path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute("UPDATE subtitle_delivery_jobs SET thread_id='competing-owner' WHERE id='episode:42:2'")
        observations.append('claim-protected')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(core.Archive, 'add_base', append)
    result = run(db, expected, path, apply=True)
    assert result['applied'] and observations == ['claim-protected']
    assert db.one("SELECT thread_id FROM subtitle_delivery_jobs WHERE id='episode:42:2'")['thread_id'] == ''
