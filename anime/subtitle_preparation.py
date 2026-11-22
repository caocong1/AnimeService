"""Read-only preparation backlog; downloaded media is never readiness proof."""
import json
import time
from pathlib import Path

from .viewing_workflow import unreleased_date


def preparation_plan(db, jobs, now=None):
    now = time.time() if now is None else now
    available, waiting, protected, missed = [], [], [], []
    has_media = db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subtitle_studio_media'")
    for job in jobs:
        if job['kind'] != 'episode' or job.get('delivery_finished') or job['state'] in ('finished', 'accepted'):
            continue
        if unreleased_date(db, job['show_id'], job['episode'], now):
            continue  # Future episodes are not media-preparation failures.
        sid, ep = job['show_id'], job['episode']
        if job.get('scope_blocker'):
            protected.append({'job_id': job['id'], 'reason': job['scope_blocker']})
            continue
        # Unknown dispatches and stopped writers have their own recovery path.
        if job.get('dispatch_state') or job.get('thread_id') or job.get('lease_owner'):
            continue
        if job['state'] == 'ready' and not job.get('finished'):
            continue
        files = set()
        arrivals = []
        research_files = set()
        for task in db.rows("""SELECT t.title,t.selection,t.save_path,t.hash,t.purpose,t.acknowledged,
                                      e.first_completed FROM tasks t LEFT JOIN episodes e
                               ON e.show_id=t.show_id AND e.episode=? AND e.hash=t.hash AND e.status='complete'
                               WHERE t.show_id=? AND t.status IN ('complete','linked')""", (ep, sid)):
            try:
                from .fidelity import fidelity_blocker
                item = json.loads(task['selection']).get('selected', {}).get(str(ep))
                if not item:
                    continue
                if fidelity_blocker(db, task['title'], task['selection']):
                    continue
                root = Path(task['save_path']).resolve()
                path = (root / item['name']).resolve()
                if path.is_relative_to(root) and path.is_file() and path.stat().st_size == item['size']:
                    files.add(path)
                    if task['first_completed'] and 0 < task['first_completed'] <= now:
                        arrivals.append(task['first_completed'])
                    if task['purpose'] == 'subtitle_sample' and task['acknowledged'] == 1:
                        research_files.add(path)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        show = db.show(sid)
        row = {'job_id': job['id'], 'show_id': sid, 'episode': ep,
               'title': show['title'], 'local_media_count': len(files),
               'input_key': job['input_key'], 'created_at': job['created']}
        observed = db.one('SELECT min(updated) AS seen FROM subtitle_studio_media WHERE show_id=? AND episode=?', (sid, ep)) if has_media else None
        seen = observed['seen'] if observed else None
        if job.get('finished'):
            watched = db.one('SELECT updated FROM watches WHERE show_id=? AND episode=? AND finished=1', (sid, ep))
            # Only claim a missed opportunity when media was actually inventoried
            # while this delivery request existed, before the explicit watch.
            if watched and seen and job['created'] < watched['updated'] and seen < watched['updated']:
                missed.append({**row, 'material_seen_at': seen, 'watched_at': watched['updated'],
                               'action': 'retain_history_no_rework'})
            continue
        if job['state'] == 'ready':
            continue
        # The first verified episode completion survives later inventory runs.
        # Keep an earlier research observation when both sources exist.
        if arrivals:
            arrival = min(arrivals)
            seen = min(seen, arrival) if seen else arrival
        payload = job.get('payload') or {}
        latest = payload.get('latest_assessment') or payload
        assessment = latest.get('assessment') or payload.get('assessment') or {}
        reasons = assessment.get('reasons') or []
        actions = []
        if files:
            if any(r in reasons for r in ('completed_media_needs_inventory_or_reconcile', 'needs_completed_manifest_and_ownership', 'target_changed_or_missing')):
                actions.append('verify_media_identity_and_manifest')
            if 'needs_reliable_chinese_dialogue_base' in reasons:
                actions.append('find_or_review_chinese_dialogue')
            if 'needs_actual_clean_mother_review' in reasons:
                actions.append('inspect_candidate_frames')
            if any('capability' in r or 'output_tools' in r for r in reasons):
                actions.append('verify_episode_output_tools')
            if not actions:
                actions.append('reconcile_current_materials')
        else:
            actions.append('await_or_reconcile_media')
        # Missing inventory time cannot prove recent arrival. Job creation is a
        # stable upper bound for an attention reminder; using this round's now
        # would postpone the reminder forever on every status read.
        since = max(job['created'], seen) if seen else job['created']
        row.update(actions=actions, reasons=reasons, material_seen_at=seen,
                   completed_research_files=len(research_files),
                   waiting_since=since, waiting_seconds=max(0, now - since),
                   attention_required=bool(files and now - since >= 21600))
        (available if files else waiting).append(row)
    oldest = {}
    for row in available:
        oldest[row['show_id']] = min(oldest.get(row['show_id'], row['waiting_since']), row['waiting_since'])
    available.sort(key=lambda r: (oldest[r['show_id']], r['show_id'], r['episode']))
    return {'available': available, 'awaiting_media': waiting, 'protected': protected,
            'watched_before_delivery': missed,
            'summary': {'available': len(available), 'awaiting_media': len(waiting),
                        'attention_required': sum(r['attention_required'] for r in available),
                        'watched_before_delivery': len(missed)},
            'policy': 'Prepare one least-recently-attempted show per dispatch round, even with an active writer; record actual attempts and next evidence in closure.json. Never infer readiness from this plan.'}
