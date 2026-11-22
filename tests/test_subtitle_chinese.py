"""Chinese source repairs use synthetic archives and never production downloads."""
import hashlib
import json
from pathlib import Path

import pytest
from test_reliability import setup
from test_subtitle_studio import ready
from anime.subtitle_studio import archive_for, lab_core
from anime.subtitle_chinese import (archive_text_source, chinese_preparation, collect_chinese_sources,
    completed_sources, discovery_sources, same_video_sidecars, validate_chinese_review, apply_chinese_review)


def private_source(db, sid=42, ep=2, language='Chinese and Japanese story dialogue'):
    root = db.path.parent / 'subtitle-studio' / 'parallel-test' / 'subtitle-discovery' / f'{sid}-S01E{ep:02}'
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'source.ass'
    path.write_text('[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, Text\n'
                    'Dialogue: 0,0:00:01.00,0:00:02.00,Text CN,,明天再见。\n'
                    'Dialogue: 0,0:00:01.00,0:00:02.00,Text JP,,また明日。\n', encoding='utf-8')
    source = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'actual_language': language, 'url': 'https://example.invalid/original',
              'original_filename': 'Example S01E02.ass', 'target_match_status': 'alignment_pending'}
    report = root / 'RESULT.json'
    report.write_text(json.dumps({'show_id': sid, 'season': 1, 'episode': ep, 'sources': [source]}), encoding='utf-8')
    return path, report


@pytest.mark.requires_lab
def test_discovery_negative_chinese_label_is_japanese_and_not_a_chinese_candidate(setup):
    db, *_ = ready(setup)
    private_source(db, language='Japanese story dialogue and vocalizations; not Chinese')
    sources = discovery_sources(db, 42, 2)
    assert len(sources) == 1 and sources[0]['language'] == 'jpn'
    plan = chinese_preparation(db, 42, 2)
    assert plan['action'] == 'find_chinese' and not plan['candidates']


@pytest.mark.requires_lab
def test_new_discovery_bytes_reopen_preparation_and_hash_changes_are_not_trusted(setup):
    db, *_ = ready(setup)
    empty = chinese_preparation(db, 42, 2)
    path, report = private_source(db)
    found = chinese_preparation(db, 42, 2)
    assert found['action'] == 'collect_chinese_source' and found['revision'] != empty['revision']
    assert found == chinese_preparation(db, 42, 2)
    path.write_text(path.read_text(encoding='utf-8') + '\n; changed', encoding='utf-8')
    assert not discovery_sources(db, 42, 2)
    data = json.loads(report.read_text(encoding='utf-8'))
    data['sources'][0]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    report.write_text(json.dumps(data), encoding='utf-8')
    assert chinese_preparation(db, 42, 2)['revision'] != found['revision']


@pytest.mark.requires_lab
def test_bilingual_style_extraction_preserves_original_and_exact_cue_identity(setup):
    db, *_ = ready(setup)
    path, _ = private_source(db)
    before = path.read_bytes()
    archive, core = archive_for(db), lab_core()
    ids = archive_text_source(archive, core, 42, 1, 2, path, {
        'language': 'chi', 'source_key': 'fixture', 'track': 1, 'origin': 'fixture',
        'target_matching_status': 'alignment_pending'})
    assert len(ids) == 3 and path.read_bytes() == before
    raw, zh, ja = [archive.draft(i) for i in ids]
    assert raw['metadata']['mixed_language_suspected']
    assert zh['language'] == 'chi' and zh['cues'][0]['text'] == '明天再见。'
    assert zh['metadata']['source_cue_ids'] == [1] and zh['metadata']['source_raw_sha256'] == raw['raw_hash']
    assert ja['language'] == 'jpn' and ja['metadata']['source_cue_ids'] == [2]
    assert not zh['metadata']['mixed_language_suspected']


def test_season_pack_sidecars_are_strictly_for_the_same_video(tmp_path):
    files = []
    for i, name in enumerate(('Example.S01E02.mkv', 'Example.S01E03.mkv',
                              'Example.S01E02.chs.ass', 'Example.S01E03.chs.ass')):
        path = tmp_path / name
        path.write_bytes(b'fixture')
        files.append({'index': i, 'name': name, 'size': 7})
    task = {'save_path': str(tmp_path), 'selection': json.dumps({
        'selected': {'2': files[0], '3': files[1]}, 'files': files, 'ids': [0, 1, 2, 3]})}
    assert [Path(r['path']).name for r in same_video_sidecars(task, 2)] == ['Example.S01E02.chs.ass']
    assert [Path(r['path']).name for r in same_video_sidecars(task, 3)] == ['Example.S01E03.chs.ass']


@pytest.mark.requires_lab
def test_existing_ocr_remains_an_actionable_uncorrected_source(setup):
    db, *_ = ready(setup)
    archive = archive_for(db)
    path = archive.root / 'fixture-ocr.json'
    path.write_text('[{"start":1,"end":2,"text":"明天再見"}]', encoding='utf-8')
    draft = archive.add_base(42, 1, 2, path, {'source_key': 'fixture', 'language': 'chi',
        'origin': 'windows-chinese-ocr', 'timing_is_estimated': True, 'candidate_not_reliable_base': True})
    plan = chinese_preparation(db, 42, 2)
    assert plan['action'] == 'review_chinese_ocr'
    assert plan['actions'] == ['review_chinese_ocr', 'find_chinese']
    assert plan['candidates'][0]['draft_id'] == draft and plan['reliable_text_count'] == 0
    assert not plan['production_ready_claimed']


@pytest.mark.requires_lab
def test_collect_imports_discovery_without_target_owner_or_watch_mutation(setup):
    db, q, *_ = ready(setup)
    path, _ = private_source(db)
    before = (db.rows('SELECT * FROM episodes'), db.rows('SELECT * FROM watches'), path.read_bytes())
    calls = []
    result = collect_chinese_sources(db, 42, 2, guard=lambda: calls.append(True))
    assert len(result['draft_ids']) == 3 and len(calls) >= 2
    assert result['after']['action'] == 'align_chinese_source'
    assert not result['target_changed'] and not result['owner_changed']
    assert before == (db.rows('SELECT * FROM episodes'), db.rows('SELECT * FROM watches'), path.read_bytes())
    assert q.adds == 0


@pytest.mark.requires_lab
def test_collect_rechecks_watch_before_archive_write(setup):
    db, *_ = ready(setup)
    private_source(db)
    calls = []
    def guard():
        calls.append(True)
        if len(calls) > 1:
            raise ValueError('new watch observed by fresh guard')
    with pytest.raises(ValueError, match='new watch'):
        collect_chinese_sources(db, 42, 2, guard=guard)
    assert not archive_for(db).bases(42, 1, 2)


@pytest.mark.requires_lab
def test_no_text_inspection_is_reused_until_real_media_or_sidecar_changes(setup, monkeypatch):
    db, *_ = ready(setup)
    path = db.path.parent / 'Example.S01E02.mkv'
    path.write_bytes(b'fixture')
    selected = {'index': 0, 'name': path.name, 'size': 7}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose) VALUES(?,42,'Example S01E02','complete',?,?,'subtitle_sample')",
               ('a' * 40, json.dumps({'selected': {'2': selected}, 'files': [selected], 'ids': [0]}), str(path.parent)))
    from anime.subtitles import Subtitles
    monkeypatch.setattr(Subtitles, 'tracks', lambda self, item: [])
    before = chinese_preparation(db, 42, 2)
    assert before['action'] == 'collect_chinese_source'
    result = collect_chinese_sources(db, 42, 2)
    assert result['after']['action'] == 'find_chinese'
    assert result['after']['revision'] == before['revision']
    assert before['revision'] in result['after']['revision_aliases']
    assert completed_sources(db, 42, 2)[0]['state'] == 'no_chinese_text_track'
    assert result['after'] == chinese_preparation(db, 42, 2)


@pytest.mark.requires_lab
def test_completed_receipt_is_rechecked_after_extraction_before_archival(setup, monkeypatch):
    from test_subtitle_targets import fixture
    from anime.subtitles import Subtitles
    db, _, _, archive, _, _, _, _ = fixture(setup, monkeypatch)
    before = {d['id'] for d in archive.bases(42, 1, 2)}
    path = archive.root / 'new-track.vtt'
    path.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n实际提取的新稿。\n', encoding='utf-8')
    monkeypatch.setattr(Subtitles, 'tracks', lambda self, item: [
        {'index': 1, 'codec': 'subrip', 'language': 'chi', 'supported': True}])
    def extract(self, item, index, fmt):
        db.execute("UPDATE tasks SET status='uncertain' WHERE hash=?", (item['hash'],))
        return path
    monkeypatch.setattr(Subtitles, 'extract', extract)
    result = collect_chinese_sources(db, 42, 2)
    assert any('ownership, receipt or sidecar changed' in r['error'] for r in result['errors'])
    assert before == {d['id'] for d in archive.bases(42, 1, 2)}


def review_fixture(setup, monkeypatch):
    from test_subtitle_targets import fixture
    db, _, _, archive, _, _, _, target = fixture(setup, monkeypatch)
    db.set('subtitle_delivery_readiness_42_2', {'target': target})
    source = archive.root / 'ocr.json'
    source.write_text('[{"start":1,"end":2,"text":"明天再見"}]', encoding='utf-8')
    source_id = archive.add_base(42, 1, 2, source, {'source_key': 'fixture', 'language': 'chi',
        'origin': 'windows-chinese-ocr', 'timing_is_estimated': True,
        'media_fingerprint': target['fingerprint']})
    corrected = archive.root / 'corrected.vtt'
    corrected.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n明天再见。\n', encoding='utf-8')
    from test_subtitle_delivery_integration import synthetic_png
    frame = archive.root / 'source-frame.png'
    frame.write_bytes(synthetic_png(40, 18))
    ref = lambda p: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    report = {'schema': 'chinese-dialogue-review/v1', 'reviewer_kind': 'codex',
        'show_id': 42, 'season': 1, 'episode': 2, 'source_draft_id': source_id,
        'source_raw_sha256': archive.draft(source_id)['raw_hash'], 'source_media_fingerprint': target['fingerprint'],
        'target_fingerprint': target['fingerprint'], 'corrected_subtitle': ref(corrected),
        'source_frames': [{'id': 1, **ref(frame), 'seconds': 1.5}],
        'coverage': [{'source_cue_id': 1, 'status': 'corrected_dialogue', 'corrected_cue_ids': [1],
                      'source_frame_ids': [1], 'evidence': 'Synthetic correction fixture only'}],
        'alignment': [{'corrected_cue_id': 1, 'target_start_ms': 1000, 'target_end_ms': 2000,
                       'evidence': 'Synthetic timing fixture only'}],
        'whole_episode_review': {'status': 'complete', 'sampling_gaps_checked': True,
                                'missing_chinese_dialogue_count': 0, 'evidence': 'Synthetic no-gap fixture only'}}
    return db, archive, source_id, report


@pytest.mark.requires_lab
@pytest.mark.parametrize('failure', ['coverage', 'timing', 'frame_time', 'source_hash', 'human', 'sampling_gaps'])
def test_ocr_qualification_rejects_unbound_incomplete_or_human_review(setup, monkeypatch, failure):
    db, archive, _, report = review_fixture(setup, monkeypatch)
    if failure == 'coverage': report['coverage'] = []
    elif failure == 'timing': report['alignment'][0]['target_end_ms'] = 3000
    elif failure == 'frame_time': report['source_frames'][0]['seconds'] = 10
    elif failure == 'source_hash': report['source_raw_sha256'] = 'wrong'
    elif failure == 'human': report['reviewer_kind'] = 'human'
    else: report['whole_episode_review']['sampling_gaps_checked'] = False
    path = archive.root / 'review.json'
    path.write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(ValueError):
        validate_chinese_review(db, 42, 2, path)


@pytest.mark.requires_lab
def test_ocr_qualification_creates_new_bound_draft_and_retains_original_estimate(setup, monkeypatch):
    db, archive, source_id, report = review_fixture(setup, monkeypatch)
    source_before = archive.draft(source_id)
    path = archive.root / 'review.json'
    path.write_text(json.dumps(report), encoding='utf-8')
    result = apply_chinese_review(db, 42, 2, path)
    corrected = archive.draft(result['draft_id'])
    assert corrected['id'] != source_id and not corrected['metadata']['timing_is_estimated']
    assert corrected['metadata']['source_raw_sha256'] == source_before['raw_hash']
    assert corrected['metadata']['origin'] == 'codex-reviewed-ocr'
    assert archive.draft(source_id) == source_before and not db.rows('SELECT * FROM watches')


@pytest.mark.requires_lab
def test_ocr_review_uses_existing_studio_target_without_rebinding(setup, monkeypatch):
    db, archive, _, report = review_fixture(setup, monkeypatch)
    target = db.get('subtitle_delivery_readiness_42_2')['target']
    db.set('subtitle_delivery_readiness_42_2', {})
    db.execute("INSERT OR REPLACE INTO subtitle_studio_jobs VALUES(42,2,'preparing',?,'',0)",
               (json.dumps({'target': target}),))
    before = db.one('SELECT * FROM subtitle_studio_jobs WHERE show_id=42 AND episode=2')
    path = archive.root / 'studio-target-review.json'
    path.write_text(json.dumps(report), encoding='utf-8')
    assert validate_chinese_review(db, 42, 2, path)['target'] == target
    assert db.one('SELECT * FROM subtitle_studio_jobs WHERE show_id=42 AND episode=2') == before
    assert db.get('subtitle_delivery_readiness_42_2') == {}
    # A higher-precedence target must still reject a report for the studio target.
    db.set('subtitle_delivery_readiness_42_2', {'target': {**target, 'fingerprint': 'different'}})
    with pytest.raises(ValueError, match='current production target'):
        validate_chinese_review(db, 42, 2, path)


@pytest.mark.requires_lab
def test_partial_corrected_checkpoint_advances_routing_but_never_qualifies(setup, monkeypatch):
    db, archive, source_id, report = review_fixture(setup, monkeypatch)
    report['source_hardtext_review'] = {'status': 'complete'}
    report['whole_episode_review'] = {'status': 'partial', 'sampling_gaps_checked': True,
        'missing_chinese_dialogue_count': None,
        'known_Japanese_reference_items_without_verified_Chinese_mapping': 2}
    path = archive.root / 'review.partial.json'
    path.write_text(json.dumps(report), encoding='utf-8')
    candidate = archive.add_base(42, 1, 2, report['corrected_subtitle']['path'], {
        'language': 'chi', 'origin': 'codex_reviewed_burned_chinese_partial',
        'source_key': 'checkpoint', 'candidate_not_reliable_base': True,
        'media_fingerprint': report['target_fingerprint'],
        'related_outputs': {'review.partial.json': {'path': str(path),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}})
    import anime.subtitle_chinese as chinese
    original_sources = chinese.archived_chinese_sources
    monkeypatch.setattr(chinese, 'archived_chinese_sources', lambda *args: [
        d for d in original_sources(*args) if d['draft_id'] in (source_id, candidate)])
    plan = chinese_preparation(db, 42, 2, {'fingerprint': report['target_fingerprint']})
    assert plan['action'] == 'align_chinese_source'
    assert 'review_chinese_ocr' not in plan['actions']
    assert plan['reliable_text_count'] == 0 and not plan['production_ready_claimed']
    assert archive.draft(candidate)['metadata']['candidate_not_reliable_base']
    assert archive.draft(source_id)['metadata']['timing_is_estimated']
    with pytest.raises(ValueError, match='whole-episode'):
        validate_chinese_review(db, 42, 2, path)
    assert chinese_preparation(db, 42, 2, {'fingerprint': 'new-target'})['action'] == 'align_chinese_source'
    path.write_text('{}', encoding='utf-8')
    assert chinese_preparation(db, 42, 2)['action'] == 'review_chinese_ocr'
