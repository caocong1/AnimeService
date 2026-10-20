"""Evidence registration binds a validated report to the exact current target; it never fabricates readiness."""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from anime.subtitle_evidence import register_evidence, binding_error, current_target
from test_subtitle_delivery import delivery, begin  # noqa: F401  (fixture and dispatch helper)

spec = importlib.util.spec_from_file_location('subtitle_delivery_queue', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
queue_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_cli)


def archive(db):
    root = db.path.parent / 'subtitle-studio'
    root.mkdir(exist_ok=True)
    return root


def write(root, name, report):
    path = root / name
    path.write_text(json.dumps(report), encoding='utf-8')
    return path


def capability(sid=42, ep=1, **extra):
    return {'role': 'finished_v1_technical_preparation', 'show_id': sid, 'episode': ep, 'reviewer': 'synthetic fixture',
            'target_fingerprint': f'synthetic-media-{sid}-{ep}', 'technical_decode_passed': True,
            'output_tools_verified': True, 'local_viewer_available': True, 'expires_at': time.time() + 3600,
            'evidence': 'synthetic decode/output/viewer check', **extra}


def png(path):
    # Signature, a 13-byte IHDR with a 2x2 size, and a placeholder CRC: the smallest frame the adapter accepts.
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + (2).to_bytes(4, 'big') + (2).to_bytes(4, 'big')
                     + b'\x08\x02\x00\x00\x00' + b'\x00\x00\x00\x00')
    import hashlib
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def test_capability_report_is_bound_to_the_current_target_and_reassessed(delivery):
    db, scheduler, ready, now = delivery
    root = archive(db)
    report = write(root, 'capability-42-1.json', capability())
    preview = register_evidence(scheduler, 42, 1, 'delivery_capability_report', report, dry_run=True)
    assert preview['would_register'] and preview['dry_run']
    assert 'delivery_capability_report' not in (db.get('subtitle_delivery_readiness_42_1') or {})  # enrollment metadata only
    result = register_evidence(scheduler, 42, 1, 'delivery_capability_report', report)
    assert result['registered'] and result['state'] == 'ready' and result['assessment']['ready'] is True
    record = db.get('subtitle_delivery_readiness_42_1')
    assert record['delivery_capability_report']['path'] == str(report.resolve())
    assert record['delivery_capability_report']['sha256'] == result['sha256']
    events = [json.loads(r['payload']) for r in db.rows("SELECT payload FROM subtitle_delivery_events WHERE job_id='episode:42:1' AND kind='evidence-registered'")]
    assert events == [{'kind': 'delivery_capability_report', 'sha256': result['sha256'], 'file': 'capability-42-1.json'}]
    # Other keys of the record survive; re-registering a newer report replaces only its own reference.
    db.set('subtitle_delivery_readiness_42_1', {**record, 'dispatch_standard': 'finished_v1'})
    newer = write(root, 'capability-42-1-b.json', capability(evidence='second synthetic check'))
    register_evidence(scheduler, 42, 1, 'delivery_capability_report', newer)
    record = db.get('subtitle_delivery_readiness_42_1')
    assert record['dispatch_standard'] == 'finished_v1' and record['delivery_capability_report']['path'] == str(newer.resolve())


@pytest.mark.parametrize('broken,message', [
    ({'target_fingerprint': 'synthetic-media-42-2'}, 'different media'),
    ({'expires_at': time.time() - 1}, 'expired'),
    ({'output_tools_verified': False}, 'not all passed'),
    ({'episode': 2}, 'this episode'),
    ({'reviewer': ''}, 'reviewer'),
])
def test_unbound_or_stale_reports_are_refused_without_writing(delivery, broken, message):
    db, scheduler, ready, now = delivery
    root = archive(db)
    report = write(root, 'bad.json', capability(**broken))
    with pytest.raises(ValueError, match=message):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report)
    assert 'delivery_capability_report' not in (db.get('subtitle_delivery_readiness_42_1') or {})


def test_only_private_json_files_and_known_kinds_are_accepted(delivery, tmp_path):
    db, scheduler, ready, now = delivery
    root = archive(db)
    outside = tmp_path / 'elsewhere.json'
    outside.write_text(json.dumps(capability()), encoding='utf-8')
    with pytest.raises(ValueError, match='private subtitle archive'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', outside)
    (root / 'broken.json').write_bytes(b'\xff\xfe not json')
    with pytest.raises(ValueError, match='UTF-8 JSON'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', root / 'broken.json')
    with pytest.raises(ValueError, match='Unknown evidence kind'):
        register_evidence(scheduler, 42, 1, 'listening_certificate', write(root, 'x.json', capability()))


def test_sampled_candidate_review_needs_three_real_frames(delivery):
    db, scheduler, ready, now = delivery
    root = archive(db)
    frames = [png(root / f'frame-{i}.png') for i in range(3)]
    review = {'reviewer': 'synthetic fixture', 'evidence': 'synthetic', 'target_fingerprint': 'synthetic-media-42-1',
              'role': 'production_candidate', 'scope': 'sample_only',
              'identity': {'status': 'verified', 'evidence': 'title/season/episode checked'},
              'fidelity': {'status': 'verified', 'evidence': 'no AI upscale claims'},
              'clean': {'status': 'sampled_clear', 'method': 'raw_sampled', 'evidence': 'three frames inspected',
                        'inspected_source_frames': frames}}
    assert binding_error(root, 'base_review', review, 42, 1, 'synthetic-media-42-1') is None
    assert 'sampled candidate' in binding_error(root, 'base_review', {**review, 'clean': {**review['clean'], 'inspected_source_frames': frames[:2]}}, 42, 1, 'synthetic-media-42-1')
    assert 'verified' in binding_error(root, 'base_review', {**review, 'fidelity': {'status': 'pending', 'evidence': 'x'}}, 42, 1, 'synthetic-media-42-1')
    result = register_evidence(scheduler, 42, 1, 'base_review', write(root, 'review.json', review))
    assert result['registered'] and db.get('subtitle_delivery_readiness_42_1')['base_review']['sha256'] == result['sha256']


def test_dialogue_report_must_name_reviewed_sources(delivery):
    db, scheduler, ready, now = delivery
    root = archive(db)
    report = {'status': 'verified', 'role': 'dialogue', 'coverage': 'whole_episode_dialogue', 'show_id': 42, 'episode': 1,
              'reviewer': 'synthetic fixture', 'evidence': 'synthetic', 'source_raw_hashes': ['a' * 64]}
    assert binding_error(root, 'japanese_dialogue_report', report, 42, 1, 'synthetic-media-42-1') is None
    assert 'raw hashes' in binding_error(root, 'japanese_dialogue_report', {**report, 'source_raw_hashes': []}, 42, 1, 'synthetic-media-42-1')
    assert 'this episode' in binding_error(root, 'japanese_dialogue_report', {**report, 'coverage': 'songs_only'}, 42, 1, 'synthetic-media-42-1')


def test_protected_episodes_refuse_registration(delivery):
    db, scheduler, ready, now = delivery
    root = archive(db)
    report = write(root, 'capability.json', capability())
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    with pytest.raises(ValueError, match='Watched'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report)
    db.execute('DELETE FROM watches')
    begin(scheduler, 42, 1)  # committing: inputs must not move under a dispatch
    with pytest.raises(ValueError, match='in flight'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', report)
    db.execute("UPDATE shows SET state='paused' WHERE id=43")
    with pytest.raises(ValueError, match='being watched'):
        register_evidence(scheduler, 43, 1, 'delivery_capability_report', write(root, 'c43.json', capability(43, 1)))
    assert current_target(db, 42, 1) == {'fingerprint': 'synthetic-media-42-1'}


def test_cli_registers_and_prints_no_credentials(delivery, monkeypatch, capsys):
    db, scheduler, ready, now = delivery
    root = archive(db)
    report = write(root, 'capability.json', capability())
    monkeypatch.setattr(queue_cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(queue_cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'register-evidence', '42', '1',
                                      '--kind', 'delivery_capability_report', '--report', str(report)])
    queue_cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed['registered'] and printed['state'] == 'ready' and 'lease_token' not in json.dumps(printed)
    assert db.get('subtitle_delivery_readiness_42_1')['delivery_capability_report']['sha256'] == printed['sha256']
