"""Viewer-facing production queue: what each unwatched episode waits for, in order.

Read-only. The projection never carries responsible chat IDs, tokens, lease
owners or private paths. "Stalled" only means nothing was recorded for a while;
it prompts a check of the owner session and never proves that it stopped.
"""
import json
import re
import time
from pathlib import Path

from .subtitle_delivery import SubtitleDelivery, PROTECTED_DISPATCH, _exists
from .subtitle_delivery_view import STAGES
from .viewing_workflow import queue_snapshot

# Recorded activity is the owner's stage report, a scheduler event or a job change.
STALL_SECONDS = {'producing': 90 * 60, 'starting': 30 * 60}
GUARD_STATUS = 'data/diagnostics/codex-dispatch-guard/status.json'
RANK = {'producing': 0, 'starting': 0, 'queued': 1, 'preparing': 2, 'downloading': 3,
        'waiting_video': 4, 'blocked': 5, 'v0_ready': 6, 'done': 7}
DOWNLOADING = ('held', 'cleaned', 'file_missing', 'error', 'complete')
_PATH = re.compile(r'[A-Za-z]:[\\/][^\s]*|(?<![\w:])/[^\s]+')


def public_text(value, limit=200):
    """Owner-written reasons may mention local files; the page only needs the gist."""
    return _PATH.sub('[路径]', str(value or ''))[:limit]


def last_events(db):
    if not _exists(db, 'subtitle_delivery_events'):
        return {}
    return {row['job_id']: (row['kind'], row['recorded']) for row in
            db.rows('SELECT job_id,kind,MAX(recorded) recorded FROM subtitle_delivery_events GROUP BY job_id')}


def work(db, job, events, now, edition):
    """Status of one scheduler job from its dispatch state and recorded activity."""
    report = db.get('subtitle_delivery_progress_' + job['id'], {}) or {}
    if (report.get('input_key') != job['input_key'] or report.get('thread_id') != job['thread_id']
            or report.get('stage') not in STAGES):
        report = {}
    event = events.get(job['id'])
    activity = max(job['updated'], event[1] if event else 0, report.get('updated') or 0)
    payload = job['payload']
    assessment = (payload.get('latest_assessment') or payload).get('assessment') or payload.get('assessment') or {}
    out = {'edition': edition, 'since': job['updated'], 'last_activity': activity, 'stage': report.get('stage'),
           'progress': {'done': report['done'], 'total': report['total']} if report.get('total') else None,
           'reasons': [], 'gates': {k: assessment.get(key) is True for k, key in
                                    (('material', 'material_ready'), ('clean', 'clean_review_ready'), ('capability', 'capability_ready'))}}
    dispatch, state = job['dispatch_state'], job['state']
    if dispatch in ('committing', 'unknown'):
        status = 'starting'
    elif dispatch == 'acknowledged':
        status = 'producing'
    elif dispatch == 'blocked-stopped' or state == 'blocked':
        status = 'blocked'
        out['reasons'] = [public_text(r) for r in payload.get('production_blockers') or []] \
            or ([public_text(job['scope_blocker'])] if job['scope_blocker'] else []) \
            or list(assessment.get('reasons') or [])
    elif state in ('ready', 'late-review'):
        status = 'queued'
    else:
        status = 'preparing'
        out['reasons'] = list(assessment.get('reasons') or [])
    limit = STALL_SECONDS.get(status)
    out['stalled'] = bool(limit and now - activity > limit)
    out['stalled_seconds'] = int(now - activity) if out['stalled'] else 0
    out['status'] = status
    return out


def episode_item(db, sid, ep, season, title, jobs, episode, gap, events, now):
    item = {'show_id': sid, 'episode': ep, 'season': season, 'title': title, 'status': 'waiting_video',
            'edition': 'v0', 'stage': None, 'progress': None, 'since': None, 'last_activity': None,
            'stalled': False, 'stalled_seconds': 0, 'position': None, 'ahead': None, 'reasons': [],
            'gates': None, 'gap': None, 'download_progress': None, 'release_id': None, 'refinement': None}
    job, refinement = jobs.get('episode'), jobs.get('refinement')
    observation = db.get(f'subtitle_delivery_finished_{sid}_{ep}', {}) or {}
    delivered = job['delivery_finished'] if job else bool(observation.get('release_id'))
    if delivered:
        item['release_id'] = observation.get('release_id')
        item['since'] = job['updated'] if job else None
        if observation.get('edition') == 'v1' or (job and job['state'] == 'accepted'):
            item.update(status='done', edition='v1')
            return item
        item.update(status='v0_ready', edition='v0')
        follow = refinement or (job if job and job['payload'].get('refinement_pending') else None)
        if follow:
            item['refinement'] = work(db, follow, events, now, 'v1')
        return item
    if job:
        item.update(work(db, job, events, now, job['payload'].get('edition', 'v0')))
        return item
    if episode and episode['status'] == 'complete':
        item.update(status='preparing', reasons=['pending_inventory'])
    elif episode and episode['status'] not in DOWNLOADING:
        item.update(status='downloading', download_progress=round((episode['progress'] or 0) * 100))
    elif gap:
        item['gap'] = {'code': gap.get('code'), 'reason': public_text(gap.get('reason')),
                       'retry_at': gap.get('retry_at'), 'checked_at': gap.get('checked_at')}
    return item


def scheduler_health(root, now):
    """The dispatch guard's last look at the two scheduler chats, without their IDs."""
    try:
        status = json.loads((Path(root) / GUARD_STATUS).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    threads = status.get('threads') or []
    last = max([float(t.get('last_log_at') or 0) for t in threads] + [0])
    return {'checked_at': status.get('at'), 'ok': not status.get('error'),
            'decisions': sorted({str(t.get('decision')) for t in threads if t.get('decision')}),
            'last_activity': last or None, 'stale': bool(status.get('at')) and now - float(status['at']) > 1800}


def production_queue(db, now=None, show=None):
    now = now or time.time()
    snapshot = queue_snapshot(db)
    jobs = SubtitleDelivery(db, initialize=False).snapshot()['jobs']
    events = last_events(db)
    gaps = (db.get('download_gaps', {}) or {}).get('shows', {})
    items, shows = [], []
    for row in snapshot['queues']['watching']:
        sid = row['id']
        season = db.mapping(sid).get('season', 1)
        watched = {r['episode'] for r in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1', (sid,))}
        episodes = {e['episode']: e for e in db.rows('SELECT episode,status,progress FROM episodes WHERE show_id=?', (sid,))}
        by_episode = {}
        for job in jobs:
            if job['show_id'] == sid and job['kind'] in ('episode', 'refinement'):
                by_episode.setdefault(job['episode'], {})[job['kind']] = job
        show_gaps = gaps.get(str(sid), {}) or {}
        candidates = (set(by_episode) | {j['episode'] for j in row['jobs']}
                      | {int(k) for k in show_gaps if str(k).isdigit()}
                      | {ep for ep, e in episodes.items() if e['status'] not in DOWNLOADING})
        entries = []
        for ep in sorted(ep for ep in candidates if ep > 0 and ep not in watched):
            entries.append(episode_item(db, sid, ep, season, row['title'], by_episode.get(ep, {}),
                                        episodes.get(ep), show_gaps.get(str(ep)), events, now))
        items.extend(entries)
        shows.append({'show_id': sid, 'title': row['title'], 'season': season,
                      'next_unwatched': row.get('next_unwatched'), 'items': entries})
    order = {job['id']: index for index, job in enumerate(jobs)}
    occupied = any(job['dispatch_state'] in PROTECTED_DISPATCH or job['lease_expires'] > now for job in jobs)
    queued = sorted((i for i in items if i['status'] == 'queued'),
                    key=lambda i: order.get(SubtitleDelivery.episode_id(i['show_id'], i['episode']), len(order)))
    for position, item in enumerate(queued, 1):
        item['position'] = position
        item['ahead'] = position - 1 + (1 if occupied else 0)
    items.sort(key=lambda i: (RANK[i['status']], i['position'] or 0, i['show_id'], i['episode']))
    active = [i for i in items if i['status'] in ('producing', 'starting')
              or (i['refinement'] or {}).get('status') in ('producing', 'starting')]
    slot = None
    if active:
        head = active[0]
        part = head['refinement'] if head['status'] == 'v0_ready' else head
        slot = {k: head[k] for k in ('show_id', 'episode', 'season', 'title')}
        slot.update({k: part[k] for k in ('status', 'edition', 'stage', 'since', 'last_activity', 'stalled', 'stalled_seconds')})
    summary = {status: sum(i['status'] == status for i in items) for status in RANK}
    summary['stalled'] = sum(i['stalled'] or (i['refinement'] or {}).get('stalled', False) for i in items)
    if show is not None:  # positions, the slot and the summary stay global; one show's page still sees its true place
        items = [i for i in items if i['show_id'] == show]
        shows = [s for s in shows if s['show_id'] == show]
    return {'schema': 1, 'generated_at': now, 'stall_seconds': STALL_SECONDS,
            'slot': {'occupied': occupied, 'item': slot}, 'summary': summary,
            'scheduler': scheduler_health(db.path.parent.parent, now), 'shows': shows, 'items': items}
