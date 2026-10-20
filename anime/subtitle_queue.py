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
IDLE_SECONDS = 15 * 60  # an owner chat whose latest turn ended this long ago, with nothing finished, is waiting on someone
GUARD_STATUS = 'data/diagnostics/codex-dispatch-guard/status.json'
OWNER_GUARD_STATUS = 'data/diagnostics/codex-subtitle-owner-guard/status.json'
# The owner guard's decisions, folded to what a viewer needs to know about the responsible chat.
OWNER_STATES = {'recent_progress': 'working', 'tool_in_progress': 'working', 'terminal_or_idle': 'idle',
                'interrupt_stale_owner_wait': 'stuck', 'already_attempted': 'stuck'}
RANK = {'producing': 0, 'starting': 0, 'queued': 1, 'preparing': 2, 'downloading': 3,
        'waiting_video': 4, 'blocked': 5, 'v0_ready': 6, 'done': 7}
DOWNLOADING = ('held', 'cleaned', 'file_missing', 'error', 'complete')
_PATH = re.compile(r'[A-Za-z]:[\\/][^\s]*|(?<![\w:])/[^\s]+')
PUBLIC_REASONS = frozenset(('needs_verified_completed_media', 'completed_media_needs_inventory_or_reconcile',
    'needs_completed_manifest_and_ownership', 'target_changed_or_missing', 'needs_reliable_chinese_dialogue_base',
    'needs_reliable_japanese_dialogue_or_real_transcription', 'archived_input_changed_or_invalid',
    'needs_actual_clean_mother_review', 'needs_verified_local_finished_output_tools',
    'needs_actual_audio_picture_and_player_capability', 'readiness_proof_or_exact_input_missing',
    'refinement_baseline_or_target_changed', 'late_review_target_changed'))


def public_reasons(reasons):
    return list(dict.fromkeys(r if isinstance(r, str) and r in PUBLIC_REASONS
                              else 'preparation_check_required' for r in reasons or []))


def public_text(value, limit=200):
    """Owner-written reasons may mention local files; the page only needs the gist."""
    return _PATH.sub('[路径]', str(value or ''))[:limit]


def last_events(db):
    if not _exists(db, 'subtitle_delivery_events'):
        return {}
    return {row['job_id']: (row['kind'], row['recorded']) for row in
            db.rows("SELECT job_id,kind,MAX(recorded) recorded FROM subtitle_delivery_events WHERE kind NOT IN ('readiness','readiness-observed') GROUP BY job_id")}


def owner_guard(root, now):
    """What the owner guard last saw of the responsible chat, keyed by job; no chat or turn identifiers."""
    try:
        with (Path(root) / OWNER_GUARD_STATUS).open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return {}
        status = json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(status, dict) or status.get('error') or not isinstance(status.get('threads'), list):
        return {}
    at = status.get('at')
    if (isinstance(at, bool) or not isinstance(at, (int, float))
            or not 0 < at <= now + 30 or now - at > 1800):
        return {}  # a stale guard record says nothing about the chat now
    result, duplicates = {}, set()
    for row in status['threads']:
        if not isinstance(row, dict):
            continue
        identity, revision, latest = row.get('id'), row.get('revision'), row.get('last_log_at')
        if (not isinstance(identity, str) or not identity or not isinstance(row.get('input_key'), str)
                or not isinstance(row.get('thread_id'), str) or not row['thread_id']
                or isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0):
            continue
        if latest is not None and (isinstance(latest, bool) or not isinstance(latest, (int, float))
                                  or not 0 <= latest <= now + 30):
            continue
        if identity in result or identity in duplicates:
            duplicates.add(identity)
            result.pop(identity, None)
            continue
        decision = row.get('decision')
        result[identity] = {'state': OWNER_STATES.get(decision, 'unknown') if isinstance(decision, str) else 'unknown',
                            'checked_at': at, 'last_activity': latest or None, 'input_key': row['input_key'],
                            'revision': revision, 'thread_id': row['thread_id']}
    return result


def work(db, job, events, now, edition, guard=None):
    """Status of one scheduler job from its dispatch state and recorded activity."""
    report = db.get('subtitle_delivery_progress_' + job['id'], {}) or {}
    if (report.get('input_key') != job['input_key'] or report.get('thread_id') != job['thread_id']
            or report.get('stage') not in STAGES):
        report = {}
    event = events.get(job['id'])
    activity = max(job['updated'], event[1] if event else 0, report.get('updated') or 0)
    payload = job['payload']
    assessment = (payload.get('latest_assessment') or payload).get('assessment') or payload.get('assessment') or {}
    owner = (guard or {}).get(job['id'])
    if owner and (owner['input_key'] != job['input_key'] or owner['revision'] != job['revision']
                  or owner['thread_id'] != job['thread_id'] or job['dispatch_state'] != 'acknowledged'):
        owner = None  # the guard looked at an earlier dispatch of this job
    out = {'edition': edition, 'since': job['updated'], 'last_activity': activity, 'stage': report.get('stage'),
           'progress': {'done': report['done'], 'total': report['total']} if report.get('total') else None,
           'reasons': [], 'gates': {k: assessment.get(key) is True for k, key in
                                    (('material', 'material_ready'), ('clean', 'clean_review_ready'), ('capability', 'capability_ready'))},
           'owner_state': owner['state'] if owner else None,
           'owner_last_activity': owner['last_activity'] if owner else None}
    dispatch, state = job['dispatch_state'], job['state']
    if dispatch in ('committing', 'unknown'):
        status = 'starting'
    elif dispatch == 'acknowledged':
        status = 'producing'
    elif dispatch == 'blocked-stopped' or state == 'blocked':
        status = 'blocked'
        # Owner prose can contain credentials, account names or host identities.
        # Keep that evidence private instead of trying to redact arbitrary text.
        out['reasons'] = (['production_blocked'] if payload.get('production_blockers') else
                          ['scope_protected'] if job['scope_blocker'] else public_reasons(assessment.get('reasons')))
    elif state in ('ready', 'late-review'):
        status = 'queued'
    else:
        status = 'preparing'
        out['reasons'] = public_reasons(assessment.get('reasons'))
    limit = STALL_SECONDS.get(status)
    silent = bool(limit and now - activity > limit)
    if owner and status == 'producing':
        # The guard watched the chat itself: a working chat is not stalled however quiet the
        # ledger is; an idle or stuck one is stalled as soon as the idleness is established.
        seen = max(activity, owner['last_activity'] or 0)
        stalled = owner['state'] == 'stuck' or (owner['state'] == 'idle' and now - seen > IDLE_SECONDS) \
            or (owner['state'] not in ('working', 'idle', 'stuck') and silent)
        out['stalled'] = stalled
        out['stalled_seconds'] = int(now - seen) if stalled else 0
    else:
        out['stalled'] = silent
        out['stalled_seconds'] = int(now - activity) if silent else 0
    out['status'] = status
    return out


def episode_item(db, sid, ep, season, title, jobs, episode, gap, events, now, guard=None, order=None):
    item = {'show_id': sid, 'episode': ep, 'season': season, 'title': title, 'status': 'waiting_video',
            'edition': 'v0', 'stage': None, 'progress': None, 'since': None, 'last_activity': None,
            'stalled': False, 'stalled_seconds': 0, 'position': None, 'ahead': None, 'reasons': [],
            'gates': None, 'gap': None, 'download_progress': None, 'release_id': None, 'refinement': None,
            'video_ready_at': None, 'waiting_seconds': None, 'owner_state': None, 'owner_last_activity': None,
            'order': min([(order or {}).get(j['id'], len(order or {})) for j in jobs.values()] or [len(order or {})])}
    if episode and episode['status'] == 'complete' and episode['first_completed']:
        # How long a playable file has waited for its own subtitles; the first completion is recorded once.
        item['video_ready_at'] = episode['first_completed']
        item['waiting_seconds'] = max(0, int(now - episode['first_completed']))
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
            item['refinement'] = work(db, follow, events, now, 'v1', guard)
            item['refinement']['position'] = item['refinement']['ahead'] = None
        return item
    if job:
        item.update(work(db, job, events, now, job['payload'].get('edition', 'v0'), guard))
        return item
    if episode and episode['status'] == 'complete':
        item.update(status='preparing', reasons=['pending_inventory'])
    elif episode and episode['status'] not in DOWNLOADING:
        item.update(status='downloading', download_progress=round((episode['progress'] or 0) * 100))
    elif gap:
        item['gap'] = {'code': gap.get('code'), 'reason': None,
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
    if not hasattr(db, 'con'):
        from .read_projection import read_projection
        with read_projection(db.path) as snapshot_db:
            return production_queue(snapshot_db, now=now, show=show)
    now = now or time.time()
    snapshot = queue_snapshot(db)
    jobs = SubtitleDelivery(db, initialize=False).snapshot()['jobs']
    events = last_events(db)
    gaps = (db.get('download_gaps', {}) or {}).get('shows', {})
    items, shows = [], []
    order = {job['id']: index for index, job in enumerate(jobs)}  # the scheduler's own claim order
    guard = owner_guard(db.path.parent.parent, now)
    for row in snapshot['queues']['watching']:
        sid = row['id']
        season = db.mapping(sid).get('season', 1)
        watched = {r['episode'] for r in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1', (sid,))}
        episodes = {e['episode']: e for e in db.rows('SELECT episode,status,progress,first_completed FROM episodes WHERE show_id=?', (sid,))}
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
                                        episodes.get(ep), show_gaps.get(str(ep)), events, now, guard, order))
        items.extend(entries)
        shows.append({'show_id': sid, 'title': row['title'], 'season': season,
                      'next_unwatched': row.get('next_unwatched'), 'items': entries})
    occupied = any(job['dispatch_state'] in PROTECTED_DISPATCH or job['lease_expires'] > now for job in jobs)
    # A delivered v0 whose v1 refinement is ready waits in the same line as first versions.
    queued = sorted((i for i in items if i['status'] == 'queued' or (i['refinement'] or {}).get('status') == 'queued'),
                    key=lambda i: i['order'])
    for position, item in enumerate(queued, 1):
        target = item['refinement'] if item['status'] == 'v0_ready' else item
        target['position'] = position
        target['ahead'] = position - 1 + (1 if occupied else 0)
    items.sort(key=lambda i: (RANK[i['status']], i['position'] or 0, i['show_id'], i['episode']))
    for item in items:
        item.pop('order')
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
