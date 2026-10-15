"""Versioned routing for personal wish and watching workflows.

This policy adds constraints; it never grants download or episode authorization.
"""
import json

KEY = 'viewing_workflow_policy'
POLICY = {'version': 1, 'enabled': True, 'auto_clean_bases': 2,
          'chinese_comparison_target': 3, 'runbook': 'docs/VIEWING-WORKFLOW.md'}


def enabled(db):
    value = db.get(KEY, {})
    return isinstance(value, dict) and value.get('version') == 1 and value.get('enabled') is True


def lane(db, sid):
    show = db.show(sid)
    if not show or not show['selected'] or show['cleanup_hold']:
        return None
    if show['state'] in ('wish', 'trial'):
        return 'wish'
    return 'watching' if show['state'] == 'watching' else None


def production_wanted(db, sid):
    return lane(db, sid) == 'watching' if enabled(db) else db.review_wanted(sid)


def scope_blocker(db, sid, episode=1):
    if lane(db, sid) is None:
        return '个人状态或清理保留门禁未满足'
    if not db.allowed(sid, 1) or not db.allowed(sid, episode):
        return '独立授权、映射/盘点或逐集边界未满足'
    row = db.one('SELECT config FROM source_policies WHERE show_id=?', (sid,))
    if not row or json.loads(row['config']).get('mode') not in ('sampling', 'selected'):
        return '尚无有效的个人首集/字幕研究政策'
    return ''


def clean_reservations(db, sid, episode):
    """Count unique retained/in-flight video hashes, never delete to make room."""
    from .native_indexes import media_mode
    from .fidelity import fidelity_blocker
    if episode == 1:
        samples = db.rows('''SELECT s.hash,c.title,c.source_metadata FROM source_samples s
            JOIN candidates c ON c.id=s.candidate_id WHERE s.show_id=? AND s.hash IS NOT NULL''', (sid,))
    else:
        samples = db.rows('''SELECT s.hash,c.title,c.source_metadata FROM subtitle_samples s
            JOIN candidates c ON c.id=s.candidate_id WHERE s.show_id=? AND s.episode=? AND s.hash IS NOT NULL''', (sid, episode))
    samples += db.rows('''SELECT e.hash,t.title FROM episodes e JOIN tasks t ON t.hash=e.hash
        WHERE e.show_id=? AND e.episode=?''', (sid, episode))
    return {c['hash'] for c in samples if c['hash'] and media_mode(c) in ('raw', 'soft')
            and not fidelity_blocker(db, c['title'])}


def clean_base_limit(db, sid, episode):
    # A third base requires a concrete comparison issue and recorded evidence.
    extra = db.get(f'viewing_base_exception_{sid}_{episode}', {})
    if (isinstance(extra, dict) and extra.get('max_bases') == 3
            and isinstance(extra.get('reason'), str) and extra['reason'].strip()
            and isinstance(extra.get('evidence'), str) and extra['evidence'].strip()):
        return 3
    return 2


def base_remaining(db, sid, episode):
    return max(0, clean_base_limit(db, sid, episode) - len(clean_reservations(db, sid, episode)))


def require_base_slot(db, candidate, episode):
    if not enabled(db) or lane(db, candidate['show_id']) != 'watching':
        return
    from .native_indexes import media_mode
    if media_mode(candidate) in ('raw', 'soft') and base_remaining(db, candidate['show_id'], episode) == 0:
        raise ValueError('本集已有主/对照原片样本；先实评，第三份须记录具体问题与证据')


def protected_sample(db, task):
    if task.get('acknowledged') or task.get('external_id') or task.get('status') == 'uncertain':
        return True
    row = db.one('SELECT committing,lease FROM thunder_queue WHERE hash=?', (task['hash'],))
    return bool(row and (row['committing'] or row['lease']) or
                db.get('thunder_dialog_hold_' + task['hash'], {}).get('unconfirmed'))


def reference_task(db, task):
    if not enabled(db) or task.get('purpose') != 'subtitle_sample':
        return False
    row = db.one('SELECT evidence FROM subtitle_samples WHERE hash=? AND show_id=?',
                 (task['hash'], task['show_id']))
    return bool(row and json.loads(row['evidence']).get('workflow_role') == 'subtitle_reference')


def require_reference_slot(db, sid, episode):
    if not enabled(db):
        raise ValueError('参考字幕角色须启用分流政策')
    rows = db.rows('SELECT hash,evidence FROM subtitle_samples WHERE show_id=? AND episode=?', (sid, episode))
    hashes = {r['hash'] for r in rows if r['hash'] and json.loads(r['evidence']).get('workflow_role') == 'subtitle_reference'}
    if len(hashes) >= 2:
        raise ValueError('本集已有两份额外参考视频；先提取去重和比较现有基稿')


def queue_snapshot(db):
    """Small, read-only control plane shared by both heartbeats and the API."""
    queues = {'wish': [], 'watching': []}
    for show in db.rows('SELECT id,title,state FROM shows WHERE selected=1 ORDER BY id'):
        route = lane(db, show['id'])
        if route is None:
            continue
        jobs = []
        for job in db.rows('SELECT episode,state,payload,updated FROM subtitle_studio_jobs WHERE show_id=? ORDER BY episode', (show['id'],)):
            payload = json.loads(job['payload'])
            jobs.append({'episode': job['episode'], 'state': job['state'], 'updated': job['updated'],
                         'workbook': payload.get('workbook'), 'target': payload.get('target'),
                         'scope_blocker': scope_blocker(db, show['id'], job['episode'])})
        cfg = db.one('SELECT config FROM source_policies WHERE show_id=?', (show['id'],))
        cfg = json.loads(cfg['config']) if cfg else {}
        finished = {r['episode'] for r in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1', (show['id'],))}
        queues[route].append({**show, 'scope_blocker': scope_blocker(db, show['id']),
                              'source_mode': cfg.get('mode'), 'primary': cfg.get('primary'),
                              'wait_hours': cfg.get('wait_hours', 24),
                              'next_unwatched': next((ep for ep in range(1, (db.show(show['id'])['total'] or 999)+1) if ep not in finished), None),
                              'jobs': jobs})
    return {'policy': db.get(KEY, {}), 'queues': queues,
            'summary': {key: {'personal': len(rows), 'eligible': sum(not r['scope_blocker'] for r in rows)}
                        for key, rows in queues.items()}}
