"""Two viewing editions; evidence checks never manufacture formal acceptance."""
import hashlib
import json
from pathlib import Path

SECTIONS = ('screen_text', 'op', 'ed', 'placement', 'effects', 'semantics')


def two_pass(db):
    return db.get('subtitle_delivery_policy', {}).get('editions') == ['v0', 'v1']


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_review(project, path, sid, ep, media_sha, baseline_id):
    """Require each refinement task's recorded work, or evidenced unresolved issue.

    This validates provenance and completeness of the report, not its truth or
    linguistic quality. The actual owner still inspects the bound scene/lyrics.
    """
    private = Path(project).resolve() / 'data/subtitle-studio'
    path = Path(path).resolve()
    if not path.is_relative_to(private):
        raise ValueError('Refinement evidence must remain private')
    report = json.loads(path.read_text(encoding='utf-8-sig'))
    if (report.get('role') != 'episode_v1_refinement_review'
            or report.get('show_id') != sid or report.get('episode') != ep
            or report.get('source_media_sha256') != media_sha
            or not baseline_id or report.get('baseline_release_id') != baseline_id
            or not report.get('reviewer')):
        raise ValueError('V1 report must bind the exact episode, media and playable v0')
    unresolved = []
    for section in SECTIONS:
        item = report.get('sections', {}).get(section, {})
        status = item.get('status')
        if (status not in ('done', 'unresolved', 'not_present')
                or not item.get('work_done') or not item.get('evidence_refs')
                or status == 'not_present' and section in ('placement', 'semantics')):
            raise ValueError(f'V1 task has no actual work/evidence: {section}')
        for ref in item['evidence_refs']:
            evidence = Path(ref['path']).resolve()
            if (not evidence.is_relative_to(private) or not evidence.is_file()
                    or sha(evidence) != ref.get('sha256')):
                raise ValueError(f'V1 evidence missing/changed: {section}')
        if status == 'unresolved':
            if not item.get('issue_ids'):
                raise ValueError(f'Unresolved V1 task must name archived issues: {section}')
            unresolved.extend(item['issue_ids'])
    if unresolved:
        issues = json.loads((private / 'unresolved-issues/issues.json').read_text(encoding='utf-8-sig'))
        archived = {i['id'] for i in issues['issues'] if i.get('show_id') == sid
                    and i.get('episode') == ep and i.get('blocks_delivery') is False}
        if not set(unresolved) <= archived:
            raise ValueError('V1 unresolved work was not archived for this episode')
    return {'path': str(path), 'sha256': sha(path), 'unresolved_issue_ids': sorted(set(unresolved)),
            'sections': {k: report['sections'][k]['status'] for k in SECTIONS}}


def enqueue_refinement(delivery, identity):
    """Resume a completed, unwatched basic release in its existing episode chat."""
    from .subtitle_delivery import _finished, _delivered, _scope, _scope_transaction, _json, _hash
    db = delivery.db
    original = delivery.get(identity)
    sid, ep = original['show_id'], original['episode']
    if not two_pass(db) or _scope(db, sid, ep) or _finished(db, sid, ep):
        return None
    isolated = (db.get('subtitle_delivery_policy', {}).get('stopped_owner_refinement') == 'isolated_private'
                and original['thread_id'] in db.get('subtitle_delivery_policy', {}).get('isolated_refinement_stopped_threads', [])
                and original['dispatch_state'] == 'blocked-stopped'
                and original['payload'].get('owner_stopped_writing') is True)
    if (original['kind'] != 'episode' or original['state'] == 'accepted'
            or original['dispatch_state'] != 'completed' and not isolated or not _delivered(db, sid, ep)):
        return None  # Do not wake blocked-stopped, unknown or still-writing owners.
    observation = db.get(f'subtitle_delivery_finished_{sid}_{ep}', {}) or {}
    if not observation.get('release_id') or observation.get('edition') == 'v1':
        return None
    baseline = observation['release_id']
    baseline_root = observation.get('release')
    if not baseline_root:
        # Early delivered artifacts predate the full observation schema.
        private = db.path.parent.parent / 'data/subtitle-studio/releases'
        for manifest in (private / str(sid)).glob(f'S*E{ep:03}/v*/release.json'):
            candidate = json.loads(manifest.read_text(encoding='utf-8-sig'))
            if candidate.get('id') == baseline and sha(manifest) == observation.get('manifest_sha256'):
                baseline_root = str(manifest.parent)
                break
        if not baseline_root:
            return None
    key = _hash({'baseline_release_id': baseline, 'workflow': 'v0-v1-20261015'})
    job_id = f'refinement:{sid}:{ep}:{key[:16]}'
    assessment = delivery._assessment(sid, ep, delivery._source_payload(original))
    expected_target = original['payload']['assessment'].get('target_fingerprint')
    if assessment.get('target_fingerprint') != expected_target:
        assessment['ready'] = False
        assessment['reasons'].append('refinement_baseline_or_target_changed')
    state = 'ready' if assessment['ready'] else 'blocked'
    value = {'assessment': assessment, 'baseline_release_id': baseline,
             'baseline_target_fingerprint': expected_target,
             'baseline_release': baseline_root, 'edition': 'v1',
             'isolated_owner_required': isolated,
             'work_required': list(SECTIONS), 'owner_stopped_writing': True}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?', (sid, ep)).fetchone()
        current = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
        if (_scope_transaction(c, sid, ep) or watched and watched['finished']
                or current['state'] == 'accepted' or current['dispatch_state'] != original['dispatch_state']
                or current['revision'] != original['revision']):
            return None
        existing = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (job_id,)).fetchone()
        if not existing:
            c.execute('''INSERT INTO subtitle_delivery_jobs
                (id,show_id,episode,kind,state,input_key,payload,thread_id,created,updated)
                VALUES(?,?,?,'refinement',?,?,?,?,?,?)''',
                (job_id, sid, ep, state, key, _json(value), '' if isolated else original['thread_id'], delivery.clock(), delivery.clock()))
            delivery._event(c, job_id, 'v1-refinement-queued', {'baseline_release_id': baseline})
        elif (existing['dispatch_state'] == '' and existing['lease_expires'] <= delivery.clock()
              and existing['state'] not in ('accepted','finished')
              and (existing['payload'] != _json(value) or existing['state'] != state)):
            c.execute('UPDATE subtitle_delivery_jobs SET state=?,payload=?,revision=revision+1,updated=? WHERE id=?',
                      (state, _json(value), delivery.clock(), job_id))
    return delivery.get(job_id)
