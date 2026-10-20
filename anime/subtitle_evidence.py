"""Controlled registration of preparation evidence into an episode's readiness record.

A report produced by a bounded check (output-tool capability, candidate cleanliness,
Japanese dialogue qualification) only counts once it is bound to the episode's exact
current target and recorded where the readiness adapter reads it. This validates that
binding and merges the reference; the adapter still decides readiness, and the dispatch,
freeze and acceptance gates are untouched. Nothing here produces or edits evidence.
"""
import hashlib
import json
import time
from pathlib import Path

from .subtitle_delivery import _exists, _finished, _scope
from .subtitle_delivery_materials import sampled_frames_valid
from .subtitle_targets import head
from .viewing_workflow import lane

KINDS = ('delivery_capability_report', 'capability_report', 'base_review', 'japanese_dialogue_report')
IN_FLIGHT = ('committing', 'unknown')


def private_root(db):
    return (db.path.parent / 'subtitle-studio').resolve()


def current_target(db, sid, ep):
    """Same precedence as the readiness adapter: frozen head, the record's provisional target, then the studio job's."""
    frozen = head(db, sid, ep)
    if frozen:
        return frozen['payload'].get('target') or {}
    record = db.get(f'subtitle_delivery_readiness_{sid}_{ep}', {}) or {}
    if record.get('target'):
        return record['target']
    if not _exists(db, 'subtitle_studio_jobs'):
        return {}
    row = db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?', (sid, ep))
    return (json.loads(row['payload']).get('target') if row else None) or {}


def load_report(root, path):
    file = Path(path).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        raise ValueError('Evidence must be a file inside the private subtitle archive')
    raw = file.read_bytes()
    try:
        report = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('Evidence must be a UTF-8 JSON object') from None
    if not isinstance(report, dict):
        raise ValueError('Evidence must be a JSON object')
    return file, raw, report


def _fresh(report):
    return type(report.get('expires_at')) in (int, float) and report['expires_at'] > time.time()


def _section(report, key):
    value = report.get(key)
    return value if isinstance(value, dict) else {}


def binding_error(root, kind, report, sid, ep, fingerprint):
    """Why the readiness adapter would ignore this report for the episode's current target, or None."""
    if not fingerprint:
        return 'the episode has no current target to bind to'
    if not report.get('reviewer') or not report.get('evidence'):
        return 'the report needs a reviewer and evidence'
    if kind == 'delivery_capability_report':
        if (report.get('role') != 'finished_v1_technical_preparation'
                or report.get('show_id') != sid or report.get('episode') != ep):
            return 'the report is not a finished_v1 technical preparation for this episode'
        if report.get('target_fingerprint') != fingerprint:
            return 'the report targets different media'
        if not all(report.get(k) is True for k in ('technical_decode_passed', 'output_tools_verified', 'local_viewer_available')):
            return 'the technical checks are not all passed'
        if not _fresh(report):
            return 'the report has expired or has no expiry'
    elif kind == 'capability_report':
        if report.get('target_fingerprint') != fingerprint:
            return 'the report targets different media'
        if not all(report.get(k) is True for k in ('actual_audio_received_and_listened', 'continuous_picture_observed',
                                                   'non_default_ass_player_verified')):
            return 'the capability checks are not all passed'
        if not _fresh(report):
            return 'the report has expired or has no expiry'
    elif kind == 'base_review':
        if report.get('target_fingerprint') != fingerprint:
            return 'the review targets different media'
        if not (_section(report, 'identity').get('status') == 'verified' and _section(report, 'fidelity').get('status') == 'verified'
                and all(_section(report, k).get('evidence') for k in ('identity', 'fidelity', 'clean'))):
            return 'identity, fidelity and clean evidence must all be verified'
        clean = _section(report, 'clean')
        whole = clean.get('status') == 'verified' and clean.get('method') in ('raw_continuous', 'soft_disabled_continuous')
        sampled = (report.get('role') == 'production_candidate' and report.get('scope') == 'sample_only'
                   and clean.get('status') == 'sampled_clear' and clean.get('method') in ('raw_sampled', 'soft_disabled_sampled')
                   and sampled_frames_valid(root, clean.get('inspected_source_frames')))
        if not (whole or sampled):
            return 'the clean review is neither a continuous verification nor a valid sampled candidate'
    elif kind == 'japanese_dialogue_report':
        if (report.get('status') != 'verified' or report.get('role') != 'dialogue'
                or report.get('coverage') != 'whole_episode_dialogue'
                or report.get('show_id') != sid or report.get('episode') != ep):
            return 'the report is not a verified whole-episode dialogue qualification for this episode'
        if not isinstance(report.get('source_raw_hashes'), list) or not report['source_raw_hashes']:
            return 'the report must name the archived source raw hashes it reviewed'
    else:
        return 'unknown evidence kind'
    return None


def register_evidence(delivery, sid, ep, kind, path, dry_run=False):
    """Bind one validated report to the episode's readiness record and re-assess through the adapter."""
    db = delivery.db
    if kind not in KINDS:
        raise ValueError('Unknown evidence kind')
    if lane(db, sid) != 'watching':
        raise ValueError('Preparation evidence is only registered for shows being watched')
    blocker = _scope(db, sid, ep)
    if blocker:
        raise ValueError(blocker)
    if _finished(db, sid, ep):
        raise ValueError('Watched episodes are not prepared')
    root = private_root(db)
    file, raw, report = load_report(root, path)
    target = current_target(db, sid, ep)
    problem = binding_error(root, kind, report, sid, ep, target.get('fingerprint'))
    if problem:
        raise ValueError(f'{kind}: {problem}')
    identity = delivery.episode_id(sid, ep)
    job = (db.one('SELECT dispatch_state FROM subtitle_delivery_jobs WHERE id=?', (identity,))
           if _exists(db, 'subtitle_delivery_jobs') else None)
    if job and job['dispatch_state'] in IN_FLIGHT:
        raise ValueError('A dispatch is in flight; reconcile it before changing this episode\'s inputs')
    reference = {'path': str(file), 'sha256': hashlib.sha256(raw).hexdigest()}
    result = {'show_id': sid, 'episode': ep, 'kind': kind, 'sha256': reference['sha256'],
              'target_fingerprint': target['fingerprint'], 'dry_run': dry_run}
    if dry_run:
        return {**result, 'would_register': True}
    key = f'subtitle_delivery_readiness_{sid}_{ep}'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        record = json.loads(row['value']) if row else {}
        record[kind] = reference
        c.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (key, json.dumps(record, ensure_ascii=False)))
        if job:
            delivery._event(c, identity, 'evidence-registered', {'kind': kind, 'sha256': reference['sha256'], 'file': file.name})
    current = delivery.record_preparation(sid, ep)
    value = current['payload'].get('latest_assessment') or current['payload']
    assessment = value.get('assessment') or {}
    return {**result, 'registered': True, 'state': current['state'],
            'assessment': {k: assessment.get(k) for k in ('ready', 'material_ready', 'clean_review_ready', 'capability_ready', 'reasons')}}
