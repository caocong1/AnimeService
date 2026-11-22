"""Small public delivery projection: no private paths, claims or download calls."""
import json
import os
import time
import uuid
from pathlib import Path

from .fidelity import fidelity_blocker
from .native_indexes import media_mode
from .subtitle_delivery import SubtitleDelivery, _scope

STAGES = ('writing', 'layout', 'rendering', 'checking', 'publishing', 'screen_text', 'lyrics', 'placement', 'effects', 'refining')


def report_progress(db, identity, thread_id, stage, done=None, total=None):
    if stage not in STAGES:
        raise ValueError('Unknown production stage')
    if (done is None) != (total is None) or (done is not None and
            (type(done) is not int or type(total) is not int or not 0 <= done <= total or total < 1)):
        raise ValueError('Invalid actual progress counts')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
        if (not row or not thread_id or row['thread_id'] != thread_id
                or row['dispatch_state'] != 'acknowledged' or row['state'] != 'production'
                or _scope(db, row['show_id'], row['episode'])):
            raise ValueError('Only the confirmed active owner may report its actual stage')
        from .subtitle_refinement import two_pass
        if two_pass(db) and db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (row['show_id'], row['episode'])):
            raise ValueError('Watched episode refinement is frozen')
        value = {'job_id': identity, 'thread_id': thread_id, 'input_key': row['input_key'],
                 'stage': stage, 'done': done, 'total': total, 'updated': time.time()}
        c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                  ('subtitle_delivery_progress_' + identity, json.dumps(value)))
    return value


def resource_catalogue(db, identities):
    """Read each show's manifests once, checking current files for this request."""
    identities = set(identities)
    result = {key: {k: {'complete': 0, 'downloading': 0, 'pending': 0, 'missing': 0}
                    for k in ('original', 'reference', 'unknown')} for key in identities}
    shows = sorted({sid for sid, _ in identities})
    seen = set()
    for start in range(0, len(shows), 400):
        batch = shows[start:start + 400]
        tasks = db.rows('SELECT show_id,hash,title,status,selection,save_path FROM tasks WHERE show_id IN ('
                        + ','.join('?' for _ in batch) + ')', batch)
        for task in tasks:
            try:
                selected = json.loads(task['selection']).get('selected', {})
                if not isinstance(selected, dict) or fidelity_blocker(db, task['title'], task['selection']):
                    continue
                base = Path(task['save_path']).resolve()
                mode = media_mode(task)
                kind = 'original' if mode in ('raw', 'soft') else 'reference' if mode == 'hard' else 'unknown'
            except (ValueError, KeyError, TypeError, OSError):
                continue
            for episode, item in selected.items():
                try:
                    identity = task['show_id'], int(episode)
                    key = (*identity, task['hash'])
                    if identity not in result or key in seen:
                        continue
                    path = (base / item['name']).resolve()
                    if not path.is_relative_to(base):
                        continue
                    seen.add(key)
                    if task['status'] in ('complete', 'linked'):
                        state = 'complete' if path.is_file() and path.stat().st_size == item['size'] else 'missing'
                    else:
                        state = 'downloading' if task['status'] == 'downloading' else 'pending'
                    result[identity][kind][state] += 1
                except (ValueError, KeyError, TypeError, OSError):
                    continue
    return result


def resources(db, sid, ep):
    return resource_catalogue(db, {(sid, ep)})[(sid, ep)]


def catalogue(db):
    if not hasattr(db, 'con'):
        from .read_projection import read_projection
        with read_projection(db.path) as snapshot_db:
            return catalogue(snapshot_db)
    from .subtitle_queue import (last_events, owner_guard, work, attach_recovery, scheduler_health,
                                 episode_item, queued_work_positions)
    from .viewing_workflow import lane, unreleased_date
    now = time.time()
    # The full media/date review is coordinator work, not a page dependency.
    queue = SubtitleDelivery(db, clock=lambda: now, initialize=False).snapshot(include_release_review=False)
    preparation = {r['job_id']: r for r in queue.get('preparation', {}).get('available', [])}
    recovery = {r['job_id']: r for r in queue.get('recovery', {}).get('items', [])}
    events, guard = last_events(db), owner_guard(db.path.parent.parent, now)
    materials = resource_catalogue(db, {(j['show_id'], j['episode']) for j in queue['jobs']})
    rows = []
    for job in queue['jobs']:
        show = db.show(job['show_id']); payload = job['payload']
        latest = payload.get('latest_assessment') or payload
        assessment = latest.get('assessment') or payload.get('assessment') or {}
        report = db.get('subtitle_delivery_progress_' + job['id'], {}) or {}
        if (report.get('input_key') != job['input_key'] or report.get('thread_id') != job['thread_id']
                or report.get('stage') not in STAGES):
            report = {}
        # Never expose responsible IDs or exact input/source paths to the page.
        progress = {k: report[k] for k in ('stage', 'done', 'total', 'updated') if k in report}
        refs = assessment.get('material_refs') or []
        chinese = [r for r in refs if isinstance(r, dict) and r.get('kind') == 'archived_draft'
                   and r.get('language') in ('chi', 'zho', 'zh', 'zh-CN', 'zh-TW', 'chs', 'cht')]
        edition = payload.get('edition', 'v1' if job['kind'] == 'refinement' else 'v0')
        current = work(db, job, events, now, edition, guard)
        future = unreleased_date(db, job['show_id'], job['episode'], now) if not job['delivery_finished'] else ''
        if future:
            current.update(status='unreleased', release_date=future)
        attach_recovery(current, recovery.get(job['id']))
        rows.append({'job_id': job['id'], 'show_id': job['show_id'], 'episode': job['episode'],
                     'season': db.mapping(job['show_id']).get('season', 1), 'title': show['title'],
                     'kind': job['kind'], 'state': job['state'], 'dispatch_state': job['dispatch_state'],
                     'edition': edition,
                     'refinement_pending': payload.get('refinement_pending') is True,
                     'watched': job['finished'], 'scope_valid': not bool(job['scope_blocker']),
                     'delivery_finished': job['delivery_finished'], 'progress': progress,
                     'resources': materials[(job['show_id'], job['episode'])],
                     'chinese_bases': len(chinese), 'chinese_texts': len({r.get('text_hash') for r in chinese}),
                     'ready': assessment.get('ready') is True,
                     'material_ready': assessment.get('material_ready') is True,
                     'clean_review_ready': assessment.get('clean_review_ready') is True,
                     'capability_ready': assessment.get('capability_ready') is True,
                     'reasons': current['reasons'], 'updated': job['updated'],
                     'work': current})
        pending = preparation.get(job['id'])
        if pending:
            rows[-1]['preparation_wait'] = {k: pending[k] for k in
                ('waiting_since', 'waiting_seconds', 'attention_required', 'actions')}
    # Build only job-backed episode facts, within this same read snapshot. Reuse
    # the queue's v0/v1 selection and ordering without loading its media inventory.
    order = {job['id']: index for index, job in enumerate(queue['jobs'])}
    grouped = {}
    for job in queue['jobs']:
        if job['kind'] in ('episode', 'refinement') and job['episode'] > 0 and not job['finished']:
            grouped.setdefault((job['show_id'], job['episode']), {})[job['kind']] = job
    waiting = [episode_item(db, sid, ep, None, None, jobs, None, None, events, now, guard, order)
               for (sid, ep), jobs in grouped.items() if lane(db, sid) == 'watching']
    positions = queued_work_positions(waiting, queue['capacity'])
    for row in rows:
        row['work'].update(positions.get(row['job_id'], {}))
    return {'schema': 1, 'generated_at': now, 'live': True, 'jobs': rows,
            'capacity': queue['capacity'], 'scheduler': scheduler_health(db.path.parent.parent, now),
            'recovery_health': {'error': queue.get('recovery', {}).get('error')}}


def export_view(db, project=None):
    project = Path(project or db.path.parent.parent)
    path = project / 'static/subtitle-delivery-status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = catalogue(db)
    value['live'] = False
    temporary = path.with_name('subtitle-delivery-status-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return value
