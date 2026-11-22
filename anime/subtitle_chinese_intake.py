"""Guarded archival of actual OCR candidates; never qualify a viewing base.

The production DB is only locked and read. Source text and its checkpoint are
appended to the private subtitle archive, with source-clock timing retained.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .db import Store
from .subtitle_chinese import _ensure_unowned, _private_file, completed_sources, digest
from .subtitle_lab import lab_core
from .subtitle_targets import exists
from .viewing_workflow import unreleased_date

SCHEMA = 'chinese-source-candidate-request/v1'
CHECKPOINT = 'chinese-source-candidate-checkpoint/v1'


class _TransactionStore(Store):
    """Use inherited read helpers against the locked, current transaction."""
    def __init__(self, db, con):
        self.path, self.con = db.path, con

    def rows(self, sql, args=()):
        return [dict(row) for row in self.con.execute(sql, args)]

    def execute(self, *args, **kwargs):
        raise ValueError('Candidate intake never writes the production database')


def _stat(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _json(raw):
    return json.loads(raw.decode('utf-8-sig'))


def _ref(root, ref, refs):
    if not isinstance(ref, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(ref.get('sha256', ''))):
        raise ValueError('Candidate evidence needs an exact path and SHA256')
    path = Path(ref.get('path', '')).resolve()
    before = _stat(path)
    _, raw = _private_file(root, ref, 64 * 1024 * 1024)
    if before != _stat(path):
        raise ValueError('Candidate evidence changed while reading')
    refs[str(path)] = {'sha256': digest(raw), 'stat': before}
    return path, raw


def review_summary(report, sid, season, ep, fingerprint, raw_hash, media_sha256=None):
    """A corrected source checkpoint advances preparation, not qualification."""
    if not isinstance(report, dict):
        raise ValueError('Partial review must be a JSON object')
    corrected = report.get('corrected_subtitle') or {}
    if (not isinstance(corrected, dict) or report.get('reviewer_kind') != 'codex'
            or (report.get('show_id'), report.get('season'), report.get('episode')) != (sid, season, ep)
            or corrected.get('sha256') != raw_hash):
        raise ValueError('Partial review must bind this actual episode and corrected subtitle')
    hardtext = report.get('source_hardtext_review') or {}
    if not isinstance(hardtext, dict):
        raise ValueError('Partial review needs its actual source correction stage')
    if report.get('schema') == 'chinese-dialogue-review/v1':
        valid = (hardtext.get('status') == 'complete'
                 and report.get('source_media_fingerprint') == fingerprint)
    elif report.get('schema') == 'private-chinese-hardtext-preparation/v1':
        valid = (hardtext.get('status') in ('complete', 'recovered_cues_corrected')
                 and (report.get('source') or {}).get('fingerprint') == fingerprint)
    else:
        valid = False
    if not valid:
        raise ValueError('Partial review has no bound corrected source checkpoint')
    declared_sha = (report.get('source') or {}).get('sha256') if report.get('schema') == 'private-chinese-hardtext-preparation/v1' else report.get('source_media_sha256')
    if media_sha256 and declared_sha and declared_sha != media_sha256:
        raise ValueError('Partial review declares a different source media SHA')
    return {'source_hardtext_review': hardtext['status'],
            'pending_reference_items': (report.get('whole_episode_review') or {}).get(
                'known_Japanese_reference_items_without_verified_Chinese_mapping'),
            'next_action': 'align_chinese_source', 'production_ready_claimed': False}


def _guard(db, sid, season, ep, expected_input):
    _ensure_unowned(db, sid, ep)
    show = db.show(sid)
    if show['state'] != 'watching' or db.mapping(sid).get('season', 1) != season:
        raise ValueError('Candidate intake requires the exact selected watching season')
    if unreleased_date(db, sid, ep, time.time()):
        raise ValueError('Unreleased episodes cannot enter candidate preparation')
    if not exists(db, 'subtitle_delivery_jobs'):
        raise ValueError('No existing episode preparation request')
    jobs = db.rows("SELECT * FROM subtitle_delivery_jobs WHERE show_id=? AND episode=? AND kind='episode'", (sid, ep))
    if len(jobs) != 1 or jobs[0]['input_key'] != expected_input:
        raise ValueError('Preparation input/job changed; inspect current status first')
    job = jobs[0]
    payload = json.loads(job['payload'])
    if (job['state'] not in ('queued', 'preparing', 'blocked', 'ready')
            or any(job.get(k) for k in ('thread_id', 'lease_owner', 'lease_token',
                                       'dispatch_token', 'dispatch_state', 'accepted_build_id'))
            or job.get('lease_expires', 0) > time.time()
            or payload.get('user_stopped') or payload.get('owner_stopped_writing')
            or db.get(f'subtitle_delivery_finished_{sid}_{ep}')):
        raise ValueError('Owner, stopped, lease, dispatch or delivered input is protected')
    if exists(db, 'subtitle_viewing_leases') and db.one(
            'SELECT 1 FROM subtitle_viewing_leases WHERE show_id=? AND episode=? AND expires>?',
            (sid, ep, time.time())):
        raise ValueError('Active viewing lease retains its source material')
    if exists(db, 'subtitle_studio_jobs') and db.one(
            "SELECT 1 FROM subtitle_studio_jobs WHERE show_id=? AND episode=? AND state IN ('production','finished','accepted')",
            (sid, ep)):
        raise ValueError('Existing studio writer or completion is protected')
    return job


def _source(db, sid, ep, wanted):
    queue = db.one('SELECT state FROM thunder_queue WHERE hash=?', (wanted['hash'],))
    if queue and queue['state'] in ('unknown', 'committing'):
        raise ValueError('Unknown original download receipt remains protected')
    matches = [s for s in completed_sources(db, sid, ep)
               if s['hash'] == wanted['hash'] and Path(s['path']).resolve() == Path(wanted['path']).resolve()
               and s['size'] == wanted['size'] and s['fingerprint'] == wanted['fingerprint']]
    if len(matches) != 1:
        raise ValueError('Exact completed source, ownership or receipt changed')
    return matches[0]


def _original_draft(root, request, refs, core):
    """An optional existing draft is checked read-only, never edited in place."""
    identity = request.get('source_draft_id')
    if not identity:
        return
    archive_path = (root / 'archive.sqlite3').resolve()
    if not archive_path.is_file():
        raise ValueError('Original candidate draft is not archived')
    with sqlite3.connect(archive_path.as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        row = c.execute('SELECT * FROM drafts WHERE id=?', (identity,)).fetchone()
    if (not row or row['episode_key'] != core.episode_key(request['show_id'], request['season'], request['episode'])
            or row['raw_hash'] != request['subtitle']['sha256'] or row['language'] not in core.ZH):
        raise ValueError('Original draft identity, episode or raw SHA mismatch')
    meta = json.loads(row['metadata'])
    if (not meta.get('candidate_not_reliable_base')
            or meta.get('source_key') != request['source']['hash']
            or meta.get('media_fingerprint') != request['source']['fingerprint']):
        raise ValueError('Original draft is not this source-bound candidate')
    _ref(root, {'path': meta['raw_file'], 'sha256': row['raw_hash']}, refs)


def register_chinese_candidate(db, sid, ep, request_path, expected_input, *, apply=False, guard=None):
    """Plan by default; apply appends a fresh, always unqualified candidate."""
    if not isinstance(expected_input, str) or not expected_input.strip() or len(expected_input) > 512:
        raise ValueError('Exact preparation input key required')
    root = (db.path.parent / 'subtitle-studio').resolve()
    request_path = Path(request_path).resolve()
    if not request_path.is_relative_to(root) or not request_path.is_file():
        raise ValueError('Candidate request must be in the private subtitle archive')
    if request_path.stat().st_size > 1024 * 1024:
        raise ValueError('Candidate request must be a bounded private JSON file')
    refs = {}
    _, request_raw = _ref(root, {'path': str(request_path), 'sha256': digest(request_path.read_bytes())}, refs)
    request = _json(request_raw)
    if (not isinstance(request, dict) or request.get('schema') != SCHEMA
            or (request.get('show_id'), request.get('episode')) != (sid, ep)
            or any(type(request.get(k)) is not int or request[k] < 1 for k in ('show_id', 'season', 'episode'))
            or request.get('expected_input') != expected_input):
        raise ValueError('Candidate request identity or expected input mismatch')
    season = request['season']
    source = request['source']
    if (not isinstance(source, dict) or not re.fullmatch(r'[a-f0-9]{40}', str(source.get('hash', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(source.get('sha256', '')))
            or type(source.get('size')) is not int or source['size'] <= 0):
        raise ValueError('Real completed source infohash, size and full SHA required')
    core = lab_core()
    with db.gate, db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = _TransactionStore(db, c)
        _guard(current, sid, season, ep, expected_input)
        item = _source(current, sid, ep, source)
        if guard:
            guard()
        c.rollback()
    subtitle, subtitle_raw = _ref(root, request['subtitle'], refs)
    if subtitle.suffix.lower() not in ('.ass', '.ssa', '.srt', '.vtt'):
        raise ValueError('Candidate needs actual timed subtitle text')
    cues = core.parse_subtitles(subtitle_raw, subtitle.suffix.lstrip('.'))
    if not cues or any(c['start_ms'] < 0 or c['end_ms'] <= c['start_ms'] or not c['text'].strip() for c in cues):
        raise ValueError('Candidate contains no valid timed source cues')
    _ref(root, request['raw_ocr'], refs)
    evidence = request.get('frame_evidence')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8192:
        raise ValueError('Actual source frame or cue/frame evidence is required')
    for ref in evidence:
        _ref(root, ref, refs)
    _original_draft(root, request, refs, core)
    review_ref, review_raw, summary = None, None, None
    if request.get('review_report'):
        review_path, review_raw = _ref(root, request['review_report'], refs)
        report = _json(review_raw)
        summary = review_summary(report, sid, season, ep, source['fingerprint'], digest(subtitle_raw), source['sha256'])
        report_path, _ = _ref(root, report['corrected_subtitle'], refs)
        if report_path != subtitle:
            raise ValueError('Partial report names a different corrected subtitle')
        if (request.get('source_draft_id')
                and report.get('source_draft_id') != request['source_draft_id']):
            raise ValueError('Partial report names a different original draft')
        if (request.get('source_draft_id') and report.get('source_raw_sha256')
                and report['source_raw_sha256'] != digest(subtitle_raw)):
            raise ValueError('Partial report names a different original draft SHA')
        review_ref = {'path': str(review_path), 'sha256': digest(review_raw)}
    media = Path(source['path']).resolve()
    media_stat, media_hash = _stat(media), hashlib.sha256()
    with media.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            media_hash.update(chunk)
    if media_stat != _stat(media) or media_hash.hexdigest() != source['sha256']:
        raise ValueError('Actual source media SHA changed or did not match')
    result = {'schema': 'chinese-source-candidate-receipt/v1', 'show_id': sid, 'season': season,
              'episode': ep, 'expected_input': expected_input, 'applied': False, 'draft_id': None,
              'source_hash': item['hash'], 'source_fingerprint': item['fingerprint'],
              'source_media_sha256': source['sha256'], 'subtitle_sha256': digest(subtitle_raw),
              'cue_count': len(cues), 'candidate_not_reliable_base': True, 'timing_is_estimated': True,
              'production_ready_claimed': False, 'target_alignment_qualified': False,
              'ready': False, 'accepted': False, 'finished': False,
              'next_action': 'align_chinese_source' if summary else 'review_chinese_ocr',
              'target_changed': False, 'owner_changed': False, 'production_db_written': False}
    # Recheck the actual files and all original facts even for a read-only plan.
    # Hashing large media stays outside this short writer reservation.
    with db.gate, db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = _TransactionStore(db, c)
        _guard(current, sid, season, ep, expected_input)
        if _source(current, sid, ep, source) != item:
            raise ValueError('Completed source changed during candidate inspection')
        if guard:
            guard()
        if media_stat != _stat(media) or any(_stat(Path(p)) != ref['stat'] for p, ref in refs.items()):
            raise ValueError('Source or candidate evidence changed before registration')
        if apply:
            from .subtitle_studio import archive_for
            archive = archive_for(db)
            directory = root / 'candidate-intake' / digest(request_raw)
            directory.mkdir(parents=True, exist_ok=True)
            def snapshot(name, raw):
                path = directory / name
                if path.exists():
                    if path.read_bytes() != raw:
                        raise ValueError('Immutable candidate intake snapshot changed')
                else:
                    with path.open('xb') as stream:
                        stream.write(raw)
                return {'path': str(path), 'sha256': digest(raw)}
            request_ref = snapshot('request.json', request_raw)
            subtitle_ref = snapshot('source' + subtitle.suffix.lower(), subtitle_raw)
            related = {}
            if summary:
                archived_review = snapshot('review.partial.json', review_raw)
                checkpoint = {'schema': CHECKPOINT, 'show_id': sid, 'season': season, 'episode': ep,
                              'source_media_fingerprint': source['fingerprint'],
                              'corrected_subtitle': subtitle_ref, 'review_report': archived_review,
                              'review_summary': summary, 'production_ready_claimed': False}
                related['candidate-checkpoint.json'] = snapshot('candidate-checkpoint.json',
                    json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8'))
            metadata = {'language': 'chi', 'source_key': item['hash'],
                        'track': 'guarded-candidate-' + digest(request_raw),
                        'release': item['title'], 'origin': 'codex-partial-reviewed-ocr' if summary else 'archived-ocr-candidate',
                        'media_fingerprint': item['fingerprint'], 'source_media_sha256': source['sha256'],
                        'candidate_not_reliable_base': True, 'timing_is_estimated': True,
                        'target_matching_status': 'source_clock_only_alignment_pending',
                        'source_draft_id': request.get('source_draft_id'), 'intake_request': request_ref,
                        'raw_ocr': request['raw_ocr'], 'source_frame_evidence': evidence,
                        'source_review_report': review_ref, 'related_outputs': related,
                        'whole_episode_review_complete': False, 'actual_listening': False,
                        'ready': False, 'accepted': False, 'finished': False}
            identity = archive.add_base(sid, season, ep, subtitle_ref['path'], metadata)
            archived = archive.draft(identity)
            if (archived['raw_hash'] != result['subtitle_sha256']
                    or not archived['metadata'].get('candidate_not_reliable_base')
                    or not archived['metadata'].get('timing_is_estimated')):
                raise ValueError('Candidate archive readback failed')
            result.update(applied=True, draft_id=identity, request=request_ref,
                          checkpoint=related.get('candidate-checkpoint.json'))
        c.rollback()
    return result
