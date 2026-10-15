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


def resources(db, sid, ep):
    counts = {k: {'complete': 0, 'downloading': 0, 'pending': 0, 'missing': 0}
              for k in ('original', 'reference', 'unknown')}
    seen = set()
    for task in db.rows('SELECT hash,title,status,selection,save_path FROM tasks WHERE show_id=?', (sid,)):
        try:
            selection = json.loads(task['selection']); item = selection.get('selected', {}).get(str(ep))
            if not item or task['hash'] in seen or fidelity_blocker(db, task['title'], task['selection']):
                continue
            base = Path(task['save_path']).resolve(); path = (base / item['name']).resolve()
            if not path.is_relative_to(base):
                continue
            seen.add(task['hash'])
            mode = media_mode(task)
            kind = 'original' if mode in ('raw', 'soft') else 'reference' if mode == 'hard' else 'unknown'
            state = task['status']
            if state in ('complete', 'linked'):
                key = 'complete' if path.is_file() and path.stat().st_size == item['size'] else 'missing'
            elif state == 'downloading':
                key = 'downloading'
            else:
                key = 'pending'
            counts[kind][key] += 1
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return counts


def catalogue(db):
    queue = SubtitleDelivery(db, initialize=False).snapshot()
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
        chinese = [r for r in refs if r.get('kind') == 'archived_draft'
                   and r.get('language') in ('chi', 'zho', 'zh', 'zh-CN', 'zh-TW', 'chs', 'cht')]
        rows.append({'job_id': job['id'], 'show_id': job['show_id'], 'episode': job['episode'],
                     'season': db.mapping(job['show_id']).get('season', 1), 'title': show['title'],
                     'kind': job['kind'], 'state': job['state'], 'dispatch_state': job['dispatch_state'],
                     'edition': payload.get('edition', 'v1' if job['kind'] == 'refinement' else 'v0'),
                     'refinement_pending': payload.get('refinement_pending') is True,
                     'watched': job['finished'], 'scope_valid': not bool(job['scope_blocker']),
                     'delivery_finished': job['delivery_finished'], 'progress': progress,
                     'resources': resources(db, job['show_id'], job['episode']),
                     'chinese_bases': len(chinese), 'chinese_texts': len({r.get('text_hash') for r in chinese}),
                     'ready': assessment.get('ready') is True,
                     'material_ready': assessment.get('material_ready') is True,
                     'clean_review_ready': assessment.get('clean_review_ready') is True,
                     'capability_ready': assessment.get('capability_ready') is True,
                     'reasons': assessment.get('reasons') or [], 'updated': job['updated']})
    return {'schema': 1, 'generated_at': time.time(), 'jobs': rows}


def export_view(db, project=None):
    project = Path(project or db.path.parent.parent)
    path = project / 'static/subtitle-delivery-status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = catalogue(db)
    temporary = path.with_name('subtitle-delivery-status-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return value
