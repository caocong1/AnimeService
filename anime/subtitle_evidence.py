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
import math
import re
from pathlib import Path

from .subtitle_delivery import SubtitleDelivery, _exists, _finished, _scope, _scope_transaction, _delivered
from .read_projection import read_projection
from .db import Store
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
    try:
        if file.stat().st_size > 1048576:
            raise ValueError()
        raw = file.read_bytes()
        report = json.loads(raw.decode('utf-8-sig'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError('Evidence must be a UTF-8 JSON object') from None
    if not isinstance(report, dict):
        raise ValueError('Evidence must be a JSON object')
    return file, raw, report


def _fresh(report):
    return (type(report.get('expires_at')) in (int, float)
            and math.isfinite(report['expires_at']) and report['expires_at'] > time.time())


def _section(report, key):
    value = report.get(key)
    return value if isinstance(value, dict) else {}


def binding_error(root, kind, report, sid, ep, fingerprint):
    """Why the readiness adapter would ignore this report for the episode's current target, or None."""
    if not fingerprint:
        return 'the episode has no current target to bind to'
    if not report.get('reviewer') or (kind != 'base_review' and not report.get('evidence')):
        return 'the report needs a reviewer and evidence'
    if any(key in report and (type(report[key]) is not int or report[key] != value)
           for key, value in (('show_id', sid), ('episode', ep))):
        return 'the report is not for this episode'
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
        if sampled:
            frames = clean['inspected_source_frames']
            if len(frames) > 12 or any(Path(ref['path']).stat().st_size > 4194304 for ref in frames):
                return 'sample frame budget exceeded'
        if not (whole or sampled):
            return 'the clean review is neither a continuous verification nor a valid sampled candidate'
    elif kind == 'japanese_dialogue_report':
        if (report.get('status') != 'verified' or report.get('role') != 'dialogue'
                or report.get('coverage') != 'whole_episode_dialogue'
                or report.get('show_id') != sid or report.get('episode') != ep):
            return 'the report is not a verified whole-episode dialogue qualification for this episode'
        if (not isinstance(report.get('source_raw_hashes'), list) or not report['source_raw_hashes']
                or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
                       for v in report['source_raw_hashes'])):
            return 'the report must name the archived source raw hashes it reviewed'
    else:
        return 'unknown evidence kind'
    return None


class _Overlay(Store):
    def __init__(self, base, key, record):
        self.base, self.path, self.key, self.record = base, base.path, key, record

    def rows(self, sql, args=()):
        return self.base.rows(sql, args)

    def get(self, key, default=None):
        return self.record if key == self.key else self.base.get(key, default)

    def execute(self, *args, **kwargs):
        raise ValueError('Evidence assessment is read-only')


def _facts(db, sid, ep):
    tables = ('shows', 'source_policies', 'watches', 'subtitle_studio_jobs',
              'subtitle_target_heads', 'subtitle_targets', 'subtitle_delivery_jobs')
    facts = {}
    for table in tables:
        if not _exists(db, table):
            facts[table] = []
        elif table == 'shows':
            facts[table] = db.rows('SELECT * FROM shows WHERE id=?', (sid,))
        elif table == 'source_policies':
            facts[table] = db.rows('SELECT * FROM source_policies WHERE show_id=?', (sid,))
        else:
            facts[table] = db.rows('SELECT * FROM ' + table + ' WHERE show_id=? AND episode=?', (sid, ep))
    facts['record'] = db.get(f'subtitle_delivery_readiness_{sid}_{ep}', {}) or {}
    return facts


def _guard(db, sid, ep, job):
    if lane(db, sid) != 'watching':
        raise ValueError('Preparation evidence is only registered for shows being watched')
    if _scope(db, sid, ep):
        raise ValueError('Episode preparation scope is protected')
    if _finished(db, sid, ep):
        raise ValueError('Watched episodes are not prepared')
    if _delivered(db, sid, ep) or (job and job['state'] in ('finished', 'accepted')):
        raise ValueError('Completed episodes are protected')
    if not job or job['kind'] != 'episode':
        raise ValueError('Existing episode preparation job required')
    if (job['dispatch_state'] or job['thread_id'] or job['lease_owner']
            or job['lease_expires'] > time.time()):
        raise ValueError('A dispatch or responsible owner is in flight; inputs are protected')


def register_evidence(delivery, sid, ep, kind, path, dry_run=False):
    """Validate on a read snapshot, then atomically merge/reassess only this job.

    No broad refresh/enrollment is invoked. A concurrent scope, target or job
    change rejects the operation, and any assessment/commit failure leaves no
    proof reference, event or partial job update behind.
    """
    db = delivery.db
    if kind not in KINDS:
        raise ValueError('Unknown evidence kind')
    if type(sid) is not int or type(ep) is not int or sid < 1 or ep < 1:
        raise ValueError('Invalid episode identity')
    identity = delivery.episode_id(sid, ep)
    key = f'subtitle_delivery_readiness_{sid}_{ep}'
    with read_projection(db.path) as view:
        facts = _facts(view, sid, ep)
        job = view.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,))
        _guard(view, sid, ep, job)
        root = private_root(view)
        file, raw, report = load_report(root, path)
        target = current_target(view, sid, ep)
        problem = binding_error(root, kind, report, sid, ep, target.get('fingerprint'))
        if problem:
            raise ValueError(f'{kind}: {problem}')
        reference = {'path': str(file), 'sha256': hashlib.sha256(raw).hexdigest()}
        result = {'show_id': sid, 'episode': ep, 'kind': kind, 'sha256': reference['sha256'],
                  'target_fingerprint': target['fingerprint'], 'dry_run': dry_run}
        if dry_run:
            return {**result, 'would_register': True}
        record = {**facts['record'], kind: reference}
        overlay = _Overlay(view, key, record)
        assessor = SubtitleDelivery(overlay, readiness=delivery.readiness, clock=delivery.clock, initialize=False)
        source = view.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?', (sid, ep))
        assessment = assessor._assessment(sid, ep, json.loads(source['payload']) if source else {})
    # The adapter is a trusted read-only dependency. Rejections contain categories,
    # never file paths, report content or token-bearing names.
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        class Current(_Overlay):
            def rows(self, sql, args=()):
                return [dict(row) for row in c.execute(sql, args)]
            def get(self, key, default=None):
                row = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
                return json.loads(row['value']) if row else default
        current = Current(db, '', {})
        if _facts(current, sid, ep) != facts or _scope_transaction(c, sid, ep):
            raise ValueError('Preparation identity or scope changed; re-read before retrying')
        _guard(current, sid, ep, current.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)))
        try:
            if file.read_bytes() != raw:
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError('Evidence changed during validation') from None
        if binding_error(root, kind, report, sid, ep, target.get('fingerprint')):
            raise ValueError('Evidence no longer valid')
        unchanged = facts['record'].get(kind) == reference
        if not unchanged:
            c.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, json.dumps(record, ensure_ascii=False)))
            payload = json.loads(job['payload'])
            payload['assessment'] = assessment
            state = ('ready' if assessment['ready'] else 'preparing' if not
                     (assessment.get('material_ready') and assessment.get('clean_review_ready')) else 'blocked')
            c.execute('UPDATE subtitle_delivery_jobs SET payload=?,state=?,input_key=?,revision=revision+1,updated=? WHERE id=?',
                      (json.dumps(payload, ensure_ascii=False), state, assessment['input_signature'], delivery.clock(), identity))
            delivery._event(c, identity, 'evidence-registered', {'kind': kind, 'sha256': reference['sha256']})
        else:
            state = job['state']
    return {**result, 'registered': True, 'replayed': unchanged, 'state': state,
            'assessment': {k: assessment.get(k) for k in ('ready', 'material_ready', 'clean_review_ready', 'capability_ready', 'reasons')}}
