"""Append evidenced episode issues under a shared process lock; never overwrite an issue."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.subtitle_output_lock import output_lock


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def merge_issues(private, pending, *, job, owner, expected_input, apply=False):
    private = Path(private).resolve()
    pending = Path(pending).resolve()
    if not pending.is_relative_to(private) or pending.stat().st_size > 1048576:
        raise ValueError('Pending issues must be a small private file')
    raw = pending.read_bytes()
    batch = json.loads(raw.decode('utf-8-sig'))
    if (batch.get('role') != 'episode_pending_issue_merge' or batch.get('job') != job
            or batch.get('owner') != owner or batch.get('input') != expected_input):
        raise ValueError('Pending issues do not bind the original job, input and owner')
    _, sid, ep = job.split(':')
    if job != f'episode:{int(sid)}:{int(ep)}' or not owner or not expected_input:
        raise ValueError('Exact episode identity is required')
    issues = batch.get('issues')
    if not isinstance(issues, list) or not issues or len(issues) > 100:
        raise ValueError('A bounded list of actual unresolved issues is required')
    ids = set()
    for issue in issues:
        identity = issue.get('id')
        if (not isinstance(identity, str) or not identity.strip() or identity in ids
                or issue.get('show_id') != int(sid) or issue.get('episode') != int(ep)
                or type(issue.get('season')) is not int or issue['season'] < 1
                or issue.get('status') != 'archived_open' or issue.get('blocks_delivery') is not False
                or any(not issue.get(k) for k in ('category', 'title', 'dimensions', 'current_release_decision',
                                                 'existing_attempts', 'retry_requires', 'evidence_refs'))):
            raise ValueError('Incomplete, duplicate or mismatched unresolved issue')
        ids.add(identity)
        for ref in issue['evidence_refs']:
            evidence = Path(ref['path']).resolve()
            if (not evidence.is_relative_to(private) or not evidence.is_file()
                    or evidence.stat().st_size > 16 * 1048576
                    or digest(evidence.read_bytes()) != ref.get('sha256')):
                raise ValueError('Issue evidence missing, changed or outside private archive')
    folder = private / 'unresolved-issues'
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / 'issues.json'
    with output_lock(folder / '.write.lock', 'subtitle issue archive', timeout=10):
        before = index.read_bytes()
        current = json.loads(before.decode('utf-8-sig'))
        existing = {item['id']: item for item in current['issues']}
        if len(existing) != len(current['issues']):
            raise ValueError('Existing issue archive has duplicate IDs; preserve it for reconciliation')
        for issue in issues:
            if issue['id'] in existing and existing[issue['id']] != issue:
                raise ValueError('Existing issue differs; append-only merge will not overwrite it')
        added = [item for item in issues if item['id'] not in existing]
        if apply and added:
            backups = folder / 'merge-backups'
            backups.mkdir(exist_ok=True)
            backup = backups / (digest(before) + '.json')
            try:
                with backup.open('xb') as stream:
                    stream.write(before)
            except FileExistsError:
                if backup.read_bytes() != before:
                    raise ValueError('Immutable issue backup differs')
            current['issues'].extend(added)
            current['updated_at'] = datetime.now(timezone.utc).isoformat()
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as stream:
                temp = Path(stream.name)
                json.dump(current, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Detect legacy writers which have not yet adopted the lock.
                if index.read_bytes() != before:
                    raise ValueError('Issue archive changed outside shared lock; preserve and retry from new facts')
                temp.replace(index)
            finally:
                temp.unlink(missing_ok=True)
        return {'job': job, 'input': expected_input, 'owner': owner, 'applied': apply,
                'pending_sha256': digest(raw), 'before_sha256': digest(before),
                'after_sha256': digest(index.read_bytes()), 'added': [item['id'] for item in added],
                'already_archived': sorted(ids & set(existing))}


def merge_for_build(project, config):
    """Publish a prepared, evidenced batch from the current owner's v1 workspace."""
    job = config.get('delivery_job_id')
    if not job:
        return
    private = (project / 'data/subtitle-studio').resolve()
    review = Path(config['refinement_review']).resolve()
    review.relative_to(private)
    if config.get('pending_issues'):
        candidates = [Path(config['pending_issues']).resolve()]
    else:
        candidates = [folder / 'issues-pending.json' for folder in (review.parent, review.parent.parent)
                      if folder != private and folder.is_relative_to(private)]
    paths = [p for p in candidates if p.is_file()]
    if not paths and not config.get('pending_issues'):
        return
    if len(paths) != 1:
        raise ValueError('Expected exactly one episode issue batch')
    from scripts.subtitle_delivery_queue import ReadStore
    from anime.subtitle_delivery import _scope_transaction
    db = ReadStore(project / 'data/anime.db')
    try:
        row = db.con.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(job,)).fetchone()
        sid, ep, owner = config['show_id'], config['episode'], config.get('delivery_thread_id')
        if (not row or not owner or row['thread_id'] != owner or row['dispatch_state'] != 'acknowledged'
                or row['state'] != 'production' or (row['show_id'],row['episode']) != (sid,ep)
                or _scope_transaction(db.con,sid,ep)
                or db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1',(sid,ep))):
            raise ValueError('Current unwatched production owner required for issue merge')
        return merge_issues(private,paths[0],job=job,owner=owner,expected_input=row['input_key'],apply=True)
    finally:
        db.con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pending', type=Path)
    parser.add_argument('--job', required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--expected-input', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = merge_issues(ROOT / 'data/subtitle-studio', args.pending, job=args.job,
                          owner=args.owner, expected_input=args.expected_input, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
