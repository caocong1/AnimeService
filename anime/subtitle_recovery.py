"""Bounded preparation recovery and escalation. No business writes or host control.

Attempts are immutable private evidence files. Observation never counts as an
attempt, refresh timestamps never reset deadlines, and unknown writers stay held.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import time

from .viewing_workflow import unreleased_date

DEADLINE = 6 * 3600
COOLDOWN = 30 * 60
MAX_FAILURES = 2
AUTO_RECOVERY_WINDOW = 24 * 3600
EXECUTION_STALL = 30 * 60
ACTIONS = {
    'output': ('verify_output', '核验并修复本集输出工具和本地播放器', '需要可用的本地字体、编码及播放器环境'),
    'ownership': ('reconcile_download', '由原下载流程核对完成文件、manifest和原回执', '需要确认原下载任务的完成回执与文件归属，不能重复下载'),
    'clean': ('bind_clean_candidate', '核验洁净母片来源，完成后经受控入口绑定', '需要可用的洁净母片或允许核验的新候选来源'),
    'chinese': ('find_chinese', '自动检索本集中文熟肉、内封及外挂字幕', '当前来源已检查，等待新材料或可验证的新处理方法'),
    'coverage': ('repair_coverage', '检查本地审听服务并沿原负责人补齐中文对应', '自动核对缺译条目及本地审听通道，保留中文覆盖门禁'),
}
CHINESE_ACTIONS = {
    'find_chinese': ACTIONS['chinese'][1],
    'collect_chinese_source': '自动提取并归档本集熟肉内封或外挂中文稿',
    'review_chinese_ocr': '自动逐条校正已提取的熟肉中文字幕并保留实帧证据',
    'align_chinese_source': '自动匹配已有中文稿与制作母片时轴并核对对白覆盖',
}


def read_json(path, limit=1048576):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('recovery evidence exceeds size limit')
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('recovery evidence must be an object')
    return value


def archive(db):
    return db.path.parent / 'subtitle-studio' / 'recovery'


def condition(job, assessment):
    # Changes in gates/target are new facts; updated/revision alone are not.
    blockers = sorted({str(v) for v in (job.get('payload') or {}).get('production_blockers') or []})
    raw = json.dumps([job['id'], job['input_key'], sorted(assessment.get('reasons') or []),
                      blockers,
                      (job.get('payload') or {}).get('recovery_class'),
                      (job.get('payload') or {}).get('recovery_turn')], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def chinese_conditions(job, assessment, revision, revision_aliases=()):
    """Chinese material budgets survive independent output/clean gate changes.

    Recognize the old v1 condition hashes without changing immutable receipts.
    Target, source revision and all nontechnical reasons remain identity bounds.
    """
    from itertools import combinations
    gates = ('needs_actual_clean_mother_review',
             'needs_verified_original_audio_and_continuous_player_capability',
             'needs_verified_local_finished_output_tools')
    reasons = set(assessment.get('reasons') or []) - set(gates)
    base = dict(assessment, reasons=sorted(reasons))
    def digest(value, source_revision=revision):
        return hashlib.sha256(json.dumps(
            [condition(job, value), 'autonomous-chinese-v1', source_revision],
            sort_keys=True).encode()).hexdigest()
    canonical = digest(base)
    aliases = {digest(dict(base, reasons=sorted(reasons | set(extra))), source_revision)
               for source_revision in {revision, *revision_aliases}
               for count in range(len(gates) + 1)
               for extra in combinations(gates, count)}
    return canonical, aliases


def attempts(root):
    paths = sorted((root / 'attempts').glob('*.json'))
    if len(paths) > 4000:
        raise ValueError('recovery journal needs archival; refusing to ignore old attempts')
    result = [read_json(p, 32768) for p in paths]
    for row in result:
        if (not isinstance(row.get('checked_at'), (int, float)) or isinstance(row['checked_at'], bool)
                or not math.isfinite(row['checked_at'])
                or not isinstance(row.get('condition'), str) or row.get('outcome') not in ('failed', 'progress', 'blocked')):
            raise ValueError('invalid recovery attempt; repair the journal before retrying')
    return result


def round_status(before, after, history, now=None, capacity=None):
    """Audit bounded coordinator work without changing readiness or permissions.

    A copied closure, fresh observation time or unchanged helper result does not
    close a due action. Actual failed/blocked journal receipts can close one
    bounded attempt; progress alone must also change its gate.
    """
    now = time.time() if now is None else now
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {'schema': 1, 'checked_at': now, 'complete': False,
                'status': 'invalid_or_failed_snapshot', 'pending': [], 'recorded_attempts': 0}
    started = before.get('checked_at')
    def valid_snapshot(value):
        at = value.get('checked_at')
        return (type(at) in (int, float) and math.isfinite(at) and 0 < at <= now + 30
                and now - at <= 3600 and isinstance(value.get('items'), list) and not value.get('error'))
    if (not valid_snapshot(before) or not valid_snapshot(after)
            or after['checked_at'] < started):
        return {'schema': 1, 'checked_at': now, 'complete': False,
                'status': 'invalid_or_failed_snapshot', 'pending': [], 'recorded_attempts': 0}

    def due(plan):
        result = {}
        for primary in plan.get('items', []):
            for row in (primary, *primary.get('independent_routes', [])):
                action = row.get('action')
                if row.get('status') == 'ready' and (capacity or {}).get('available', 0) > 0:
                    action = 'dispatch_ready'
                if action and (row.get('status') in ('actionable', 'overdue') or action == 'dispatch_ready'):
                    key = row['job_id'], row.get('input_key'), row['condition'], action
                    result[key] = {'job_id': row['job_id'], 'input_key': row.get('input_key'),
                                   'condition': row['condition'], 'action': action}
        official = plan.get('official_release_review') or {}
        if official.get('error'):
            raise ValueError('official release review failed')
        for row in official.get('items', []):
            if row.get('status') == 'actionable' and row.get('action'):
                identity = f"official:{row['show_id']}:{row.get('episode', 0)}"
                key = identity, None, row['condition'], row['action']
                result[key] = {'job_id': identity, 'condition': row['condition'], 'action': row['action']}
        return result

    try:
        old, current = due(before), due(after)
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'schema': 1, 'checked_at': now, 'complete': False,
                'status': 'invalid_or_failed_snapshot', 'pending': [], 'recorded_attempts': 0}
    recorded = [row for row in history if started <= row['checked_at'] <= now + 30
                and row.get('outcome') in ('failed', 'blocked')
                and (row.get('job_id'), row.get('input_key'), row.get('condition'), row.get('action')) in old]
    # Only actual non-actionable postconditions close a gate. Merely changing
    # the condition/action of a still-due job cannot manufacture progress.
    current_jobs = {key[0] for key in current}
    advanced = [key for key in old if key[0] not in current_jobs]
    complete = not current or bool(recorded or advanced)
    dispatch = after.get('dispatch') or {}
    before_dispatch = before.get('dispatch') or {}
    def identities(rows):
        return {(r['job_id'], r['input_key']) for r in rows}
    initial_ready = identities(before_dispatch.get('ready', []))
    confirmed = initial_ready & identities(dispatch.get('acknowledged', []))
    if confirmed:
        complete = True
    # Preparation or a newly freed slot can make another episode eligible inside this round.
    open_ready = dispatch.get('capacity', {}).get('available', 0) > 0 and bool(dispatch.get('ready'))
    receipt_blocked = (dispatch.get('ready') and dispatch.get('capacity',{}).get('available')==0
                       and any(not r['fenced'] for r in dispatch.get('unresolved',[])))
    if receipt_blocked or open_ready:
        complete = False
    return {'schema': 1, 'checked_at': now, 'started_at': started, 'complete': complete,
            'status': 'dispatch_receipt_blocked' if receipt_blocked else 'dispatch_slots_unfilled' if open_ready
            else 'dispatch_confirmed' if confirmed else 'waiting' if not current else 'bounded_action_recorded' if complete else 'unhandled_actionable',
            'confirmed_jobs': sorted(job for job, _ in confirmed),
            'recorded_attempts': len(recorded), 'advanced_jobs': sorted({key[0] for key in advanced}),
            'pending': list(current.values())}


def auto_recovery_usage(db, thread_id, now):
    """Actual automatic resume intents for one owner, deduplicated by native turn."""
    if not thread_id:
        return {'count': 0, 'next_check_at': None}
    rows = db.rows('''SELECT job_id,payload,recorded FROM subtitle_delivery_events
        WHERE kind='owner-auto-recovery' AND recorded>=?''', (now - AUTO_RECOVERY_WINDOW,))
    turns = {}
    for row in rows:
        payload = json.loads(row['payload'])
        if payload.get('owner_thread_id') != thread_id:
            continue
        turn = payload.get('turn_id')
        recorded = row['recorded']
        if (not isinstance(turn, str) or not turn or isinstance(recorded, bool)
                or not isinstance(recorded, (int, float)) or not math.isfinite(recorded)
                or recorded > now + 30):
            raise ValueError('invalid automatic owner recovery history')
        if turn in turns and turns[turn]['job_id'] != row['job_id']:
            raise ValueError('conflicting automatic owner recovery history')
        # Replays retain the first irreversible attempt; they never extend the window.
        turns[turn] = {'job_id': row['job_id'], 'at': min(recorded, turns.get(turn, {}).get('at', recorded))}
    times = [v['at'] for v in turns.values()]
    return {'count': len(turns), 'next_check_at': min(times) + AUTO_RECOVERY_WINDOW if len(times) >= MAX_FAILURES else None,
            'last_attempt_at': max(times, default=None)}


def _clean_download_wait(db, job, now):
    """Project one original acknowledged clean-base download; never acquire or bind.

    The sample's role and the original task manifest must agree. A receipt that
    needs reconciliation, or an ambiguous second in-flight base, is not ordinary
    media waiting. Current delivery/source protections are checked before this
    optional projection, including when the caller supplied an older job row.
    """
    from .download_receipts import pending_receipt
    from .fidelity import fidelity_blocker
    from .native_indexes import candidate_mapping, media_mode, metadata
    from .releases import select_files, validate_title
    from .subtitle_targets import exists, head
    from .viewing_workflow import lane, scope_blocker

    if job['kind'] != 'episode':
        return None
    sid, ep = job['show_id'], job['episode']
    try:
        if (not db.get('subtitle_studio_enabled', False) or lane(db, sid) != 'watching'
                or scope_blocker(db, sid, ep)):
            return {'protection': 'scope_protected'}
        if db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (sid, ep)):
            return {'protection': 'watched_protected'}
        current = db.rows('SELECT * FROM subtitle_delivery_jobs WHERE show_id=? AND episode=?', (sid, ep))
        original = next((r for r in current if r['id'] == job['id']), None)
        if not original or original['input_key'] != job['input_key']:
            return {'protection': 'preparation_changed'}
        for owner in [job, *current]:
            payload = owner.get('payload') or {}
            payload = json.loads(payload) if isinstance(payload, str) else payload
            if (owner['state'] not in ('queued', 'preparing', 'blocked')
                    or any(owner.get(k) for k in ('thread_id', 'dispatch_state', 'dispatch_token',
                                                  'lease_owner', 'lease_token', 'accepted_build_id'))
                    or (owner.get('lease_expires') or 0) > now
                    or payload.get('user_stopped') or payload.get('owner_stopped_writing')):
                return {'protection': 'owner_protected'}
        if db.get(f'subtitle_delivery_finished_{sid}_{ep}') or head(db, sid, ep):
            return {'protection': 'frozen_or_delivered'}
        if exists(db, 'subtitle_viewing_leases') and db.one(
                'SELECT 1 FROM subtitle_viewing_leases WHERE show_id=? AND episode=? AND expires>?', (sid, ep, now)):
            return {'protection': 'viewing_lease_protected'}
        if exists(db, 'subtitle_studio_jobs') and db.one(
                "SELECT 1 FROM subtitle_studio_jobs WHERE show_id=? AND episode=? AND state IN ('production','finished','accepted')",
                (sid, ep)):
            return {'protection': 'owner_protected'}
        if not exists(db, 'subtitle_samples'):
            return None
        in_flight = []
        for sample in db.rows('SELECT * FROM subtitle_samples WHERE show_id=? AND episode=?', (sid, ep)):
            evidence = json.loads(sample['evidence'])
            if evidence.get('workflow_role') != 'clean_base':
                continue
            task = db.one('SELECT * FROM tasks WHERE hash=?', (sample['hash'],))
            if not task:
                return None
            if task['status'] not in ('complete', 'linked', 'cleaned'):
                in_flight.append((sample, task))
        if len(in_flight) != 1:
            return None
        sample, task = in_flight[0]
        h = task['hash']
        if (not re.fullmatch('[a-f0-9]{40}', h or '') or task['show_id'] != sid
                or task.get('purpose') != 'subtitle_sample' or task.get('provider') not in ('qbit', 'thunder')
                or task.get('acknowledged') != 1 or task['status'] != 'downloading' or task.get('error')
                or sample['state'] != 'queued' or sample.get('error')):
            return None
        # One task must have exactly one original sample owner, even across shows.
        if (len(db.rows('SELECT candidate_id FROM subtitle_samples WHERE hash=?', (h,))) != 1
                or db.one('SELECT 1 FROM source_samples WHERE hash=?', (h,))):
            return None
        queue = db.one('SELECT state,lease,committing,error FROM thunder_queue WHERE hash=?', (h,))
        if (pending_receipt(task, queue)
                or queue and (queue['state'] != 'done' or queue.get('error'))
                or db.get('thunder_dialog_hold_' + h)
                or task['provider'] == 'thunder' and (not queue or not task.get('external_id'))):
            return None
        candidate = db.one('SELECT * FROM candidates WHERE id=?', (sample['candidate_id'],))
        source = db.one('SELECT show_id,enabled FROM sources WHERE id=?', (candidate['source_id'],)) if candidate else None
        if (not candidate or candidate['show_id'] != sid or candidate['title'] != task['title']
                or candidate.get('hash') not in (None, '', h)
                or not source or source['show_id'] != sid or source['enabled'] != 1):
            return None
        declared = metadata(candidate)
        if (media_mode(candidate) not in ('raw', 'soft') or declared.get('acquisition_declaration_conflict')
                or declared.get('expected_hash') not in (None, '', h)):
            return None
        mapping = candidate_mapping(db.mapping(sid), candidate)
        if validate_title(candidate['title'], mapping, {}, False) != [ep]:
            return None
        selection = json.loads(task['selection'])
        selected, ids = select_files(selection['files'], mapping, [ep], [ep])
        if (set(selection['selected']) != {str(ep)} or selection['selected'][str(ep)] != selected[ep]
                or selection['ids'] != ids or sample['size'] != selected[ep]['size']
                or fidelity_blocker(db, candidate['title'], selection)):
            return None
        root = Path(task['save_path']).resolve()
        file = (root / selected[ep]['name']).resolve()
        season = db.mapping(sid).get('season', 1)
        if (not task['save_path'] or not Path(task['save_path']).is_absolute() or not file.is_relative_to(root)
                or len(root.parts) < 4 or not root.parts[-4].endswith(f' [bgm-{sid}]')
                or root.parts[-3:] != (f'Season {season:02}', 'Subtitle samples', h)):
            return None
        return {'candidate_id': sample['candidate_id'], 'hash': h, 'acknowledged': 1, 'status': 'downloading'}
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        # A malformed source/receipt is never promoted to a known download wait.
        return None


def independent_chinese_routes(source, assessment):
    routes = list(dict.fromkeys(source.get('actions') or [source.get('action', 'find_chinese')]))
    refs = assessment.get('material_refs') or []
    fingerprint = next((r['target'].get('fingerprint') for r in refs if isinstance(r, dict)
                        and r.get('kind') == 'completed_media' and isinstance(r.get('target'), dict)), None)
    if not (assessment.get('material_ready') and fingerprint):
        return routes
    # The archive is checked again by chinese_preparation. A title, track count
    # or another cut's timestamps cannot close the source search.
    qualified = set(assessment.get('text_hashes') or [])
    drafts = [r for r in source.get('candidates', []) if r['kind'] == 'archived_draft']
    native = any(r.get('origin') == 'embedded' and r.get('media_fingerprint') == fingerprint
                 and r.get('text_hash') in qualified and r.get('cue_count', 0) > 0
                 and not (r.get('mixed') or r.get('estimated') or r.get('candidate_not_reliable_base'))
                 and r.get('target_matching_status') not in ('pending', 'alignment_pending') for r in drafts)
    if native:
        routes = [r for r in routes if r != 'find_chinese']
        # A correction checkpoint has independent work even with a native track.
        if not any(r.get('correction_progress') for r in drafts):
            routes = [r for r in routes if r != 'align_chinese_source']
    return routes


def recovery_plan(db, jobs, preparation, now=None, *, include_release_review=True):
    now = time.time() if now is None else now
    history = attempts(archive(db))
    pool = {r['job_id']: r for r in preparation['available']}
    waiting = {r['job_id']: r for r in preparation['awaiting_media']}
    items = []
    for job in jobs:
        payload = job.get('payload') or {}
        followup = job['kind'] == 'refinement' or bool(payload.get('refinement_pending'))
        if (job.get('finished') or job['state'] in ('finished', 'accepted')
                or (job.get('delivery_finished') and not followup)):
            continue
        assessment = (payload.get('latest_assessment') or payload).get('assessment') or payload.get('assessment') or {}
        reasons = assessment.get('reasons') or []
        row = {'job_id': job['id'], 'show_id': job['show_id'], 'episode': job['episode'],
               'input_key': job['input_key'], 'condition': condition(job, assessment), 'status': 'protected',
               'action': None, 'failed_attempts': 0, 'next_check_at': None, 'deadline_at': None,
               'last_attempt_at': None, 'user_action': None, 'domain': 'preparation', 'release_date': None}
        dispatch = job.get('dispatch_state') or ''
        future = unreleased_date(db, job['show_id'], job['episode'], now)
        if future:
            row.update(status='unreleased', reason='awaiting_release', release_date=future)
        elif job.get('scope_blocker'):
            row['reason'] = 'scope_protected'
        elif dispatch in ('committing', 'unknown'):
            row['reason'] = 'unknown_dispatch'  # needs original receipt reconciliation, never retry begin
            row['domain'] = 'dispatch_reconcile'
            row['status'] = 'reconciling'
        elif dispatch == 'acknowledged' or job.get('lease_expires', 0) > now:
            row['reason'] = 'active_owner'
        elif job['state'] in ('ready', 'late-review') and not dispatch:
            row.update(status='ready', reason='ready_for_dispatch')
        elif dispatch == 'blocked-stopped' and payload.get('owner_stopped_writing'):
            row['reason'] = 'stopped_production'
            recovery_class = payload.get('recovery_class') or ''
            if payload.get('user_stopped') or recovery_class == 'user_stopped':
                row['reason'] = 'user_stopped'
            elif recovery_class == 'source_wait':
                row.update(status='source_wait', reason='source_wait',
                           explanation='当前材料或核验条件不足；出现新证据后沿原负责人受控继续')
            elif recovery_class == 'repair_blocked':
                # An engineering blocker is work for the coordinator, not a
                # permanent exclusion from its next-action list. This action
                # repairs the dependency only; it grants no host resume or
                # change to the original stopped-owner classification.
                row.update(status='actionable', reason='repair_blocked', domain='owner_recovery',
                           action='repair_owner_dependency',
                           deadline_at=(payload.get('recovery_since') or job['updated']) + DEADLINE,
                           explanation='自动修复原负责人登记的工程依赖；实修并补证后再核同owner受控恢复')
                prior = [a for a in history if a.get('condition') == row['condition']
                         and a.get('action') == row['action'] and a['outcome'] in ('failed', 'blocked')]
                row.update(failed_attempts=len(prior),
                           last_attempt_at=max([a['checked_at'] for a in prior] + [0]) or None)
                if len(prior) >= MAX_FAILURES or any(a['outcome'] == 'blocked' for a in prior):
                    row.update(status='repair_blocked',
                               explanation='工程依赖实修受阻；保留实际失败和原owner，等待可验证的新处理条件')
                elif prior and now < row['last_attempt_at'] + COOLDOWN:
                    row.update(status='retry_wait', next_check_at=row['last_attempt_at'] + COOLDOWN)
                elif now >= row['deadline_at']:
                    row['status'] = 'overdue'
            elif recovery_class == 'system':
                row['domain'] = 'owner_recovery'
                ready = assessment.get('ready') is True
                row.update(status='actionable' if ready else 'repair_blocked',
                           action='recover_owner', deadline_at=(payload.get('recovery_since') or job['updated']) + DEADLINE,
                           explanation='按精确故障轮次恢复同一负责人' if ready
                           else '当前就绪证据尚未补齐；先补证，再按原负责人受控恢复')
                failed = [a for a in history if a.get('condition') == row['condition']
                          and a.get('action') == row['action'] and a['outcome'] in ('failed', 'blocked')]
                usage = auto_recovery_usage(db, job.get('thread_id'), now)
                row.update(failed_attempts=len(failed), auto_recoveries_24h=usage['count'],
                           last_attempt_at=max([a['checked_at'] for a in failed] + [usage.get('last_attempt_at') or 0]) or None)
                if len(failed) >= MAX_FAILURES or any(a['outcome'] == 'blocked' for a in failed):
                    row.update(status='repair_blocked', explanation=ACTIONS['coverage'][2])
                elif recovery_class == 'system' and usage['count'] >= MAX_FAILURES:
                    row.update(status='retry_wait', next_check_at=usage['next_check_at'],
                               explanation='同一负责人24小时自动恢复已达上限；窗口到期后再核新条件')
                elif failed and now < max(a['checked_at'] for a in failed) + COOLDOWN:
                    row.update(status='retry_wait', next_check_at=max(a['checked_at'] for a in failed) + COOLDOWN)
                elif now >= row['deadline_at'] and row['status'] == 'actionable':
                    row['status'] = 'overdue'
            # Legacy/ambiguous stops stay protected; prose is not recovery authorization.
        elif job.get('thread_id') or dispatch or job.get('lease_owner'):
            row['reason'] = 'owner_protected'
        elif job['id'] in waiting:
            row.update(status='waiting_media', reason='awaiting_completed_media')
        elif job['id'] in pool:
            entry = pool[job['id']]
            if any(r in reasons for r in ('needs_completed_manifest_and_ownership', 'completed_media_needs_inventory_or_reconcile', 'target_changed_or_missing')):
                kind = 'ownership'
            elif not assessment.get('capability_ready'):
                kind = 'output'
            elif not assessment.get('clean_review_ready'):
                kind = 'clean'
            else:
                kind = 'chinese'
            action, explanation, help_text = ACTIONS[kind]
            if kind == 'chinese':
                # Source discovery and OCR/alignment are different work. A failed
                # web search must not exhaust already available cooked sources.
                # Read live materials: a new archive need not change the bound
                # production input or wait for a mutating queue refresh.
                from .subtitle_chinese import chinese_preparation
                source_plan = chinese_preparation(db, job['show_id'], job['episode'],
                                                 (payload.get('latest_assessment') or payload).get('target') or payload.get('target'))
                row['source_revision'] = source_plan['revision']
                row['condition'], source_conditions = chinese_conditions(job, assessment, source_plan['revision'], source_plan.get('revision_aliases', ()))
                row['condition_aliases'] = sorted(source_conditions)
                routes = source_plan.get('actions') or [source_plan.get('action', 'find_chinese')]
                if any(route not in CHINESE_ACTIONS for route in routes):
                    raise ValueError('unknown Chinese preparation action')
                available_routes, waiting_routes = [], []
                row['source_routes'] = []
                for route in dict.fromkeys(routes):
                    prior = [a for a in history if a.get('condition') in source_conditions and a.get('action') == route]
                    failures = [a for a in prior if a.get('outcome') == 'failed']
                    exhausted = len(failures) >= MAX_FAILURES or any(a.get('outcome') == 'blocked' for a in prior)
                    latest_failure = max([a['checked_at'] for a in failures] + [0])
                    cooling = bool(latest_failure and now < latest_failure + COOLDOWN)
                    row['source_routes'].append({'action': route, 'failed_attempts': len(failures),
                        'status': 'source_wait' if exhausted else 'retry_wait' if cooling else 'actionable',
                        'next_check_at': latest_failure + COOLDOWN if cooling and not exhausted else None})
                    if exhausted:
                        continue
                    (waiting_routes if latest_failure and now < latest_failure + COOLDOWN else available_routes).append(route)
                # Exhaustion or cooldown on one route never locks all other
                # existing sources. Every route retains its own immutable history.
                action = (available_routes or waiting_routes or routes)[0]
                explanation = CHINESE_ACTIONS[action]
            same = [a for a in history if a.get('condition') in row.get('condition_aliases', [row['condition']]) and a.get('action') == action]
            failed = [a for a in same if a.get('outcome') == 'failed']
            attempted = [a for a in same if a.get('outcome') in ('failed', 'blocked')]
            blocked = any(a.get('outcome') == 'blocked' for a in same)
            observed = entry.get('material_seen_at')
            # If inventory never recorded arrival, the job creation is a stable
            # upper bound for the reminder. Using each round's now would move the
            # deadline forever; refreshed updated fields must not reset it either.
            first = max(job['created'], observed) if observed else min([a['checked_at'] for a in same] + [job['created']])
            deadline = first + DEADLINE
            last_failure = max([a['checked_at'] for a in failed] + [0])
            row.update(status='actionable', reason=kind, action=action, explanation=explanation,
                       failed_attempts=len(failed), deadline_at=deadline,
                       last_attempt_at=max([a['checked_at'] for a in attempted] + [0]) or None)
            if len(failed) >= MAX_FAILURES or blocked:
                row.update(status='source_wait' if kind == 'chinese' else 'repair_blocked',
                           explanation=help_text)
            elif last_failure and now < last_failure + COOLDOWN:
                row.update(status='retry_wait', next_check_at=last_failure + COOLDOWN)
            elif now >= deadline:
                row['status'] = 'overdue'
            if kind == 'clean':
                download = _clean_download_wait(db, job, now)
                if download and download.get('protection'):
                    row.update(status='protected', reason=download['protection'], action=None)
                elif download:
                    # Keep the same condition, deadline and failed-route/cooldown
                    # evidence. When downloading ends the original gate resumes.
                    row.update(status='waiting_media', reason='clean_base_download', action=None,
                               explanation='等待已确认的洁净母片下载完成，随后核验文件与画面并经受控入口绑定',
                               media_task=download)
            if kind in ('ownership', 'clean') and row['status'] in ('actionable', 'overdue') and entry.get('completed_research_files'):
                row['explanation'] = '本集已有研究文件，先核原清单、母片用途和实帧，再绑定合格文件'
        else:
            row['reason'] = 'no_actionable_material'
        row['notification_key'] = hashlib.sha256((row['condition'] + ':' + row['status']).encode()).hexdigest()
        row['notification_pending'] = row['status'] == 'needs_user' and not (archive(db) / 'notices' / (row['notification_key'] + '.json')).is_file()
        items.append(row)
        # Output/clean checks and Chinese source work are independent gates.
        # Expose journal-only routes after the same scope/owner/receipt checks;
        # this never changes the primary gate, readiness or dispatch capacity.
        if row.get('reason') in ('output', 'clean') and row.get('action'):
            from .subtitle_chinese import chinese_preparation
            source = chinese_preparation(db, job['show_id'], job['episode'],
                (payload.get('latest_assessment') or payload).get('target') or payload.get('target'))
            source_condition, source_conditions = chinese_conditions(job, assessment, source['revision'], source.get('revision_aliases', ()))
            row['independent_routes'] = []
            for route in independent_chinese_routes(source, assessment):
                if route not in CHINESE_ACTIONS:
                    raise ValueError('unknown Chinese preparation action')
                prior = [a for a in history if a.get('condition') in source_conditions and a.get('action') == route]
                failures = [a for a in prior if a.get('outcome') == 'failed']
                last = max([a['checked_at'] for a in failures] + [0])
                state = 'overdue' if now >= row['deadline_at'] else 'actionable'
                next_check = None
                if len(failures) >= MAX_FAILURES or any(a.get('outcome') == 'blocked' for a in prior):
                    state = 'source_wait'
                elif last and now < last + COOLDOWN:
                    state, next_check = 'retry_wait', last + COOLDOWN
                row['independent_routes'].append(dict(
                    row, independent_routes=[], condition=source_condition, action=route,
                    reason='chinese', status=state, failed_attempts=len(failures),
                    explanation=CHINESE_ACTIONS[route], notification_pending=False,
                    notification_key=hashlib.sha256((source_condition + ':' + state).encode()).hexdigest(),
                    source_revision=source['revision'], next_check_at=next_check,
                    condition_aliases=sorted(source_conditions),
                    last_attempt_at=max([a['checked_at'] for a in prior] + [0]) or None))
    # Handle overdue work first, then cheap gates. A cooling/exhausted primary
    # gate must not hide independent work from the coordinator or stall monitor.
    routes = (route for row in items for route in (row, *row.get('independent_routes', [])))
    actionable = sorted((r for r in routes if r['status'] in ('actionable', 'overdue')),
                        key=lambda r: (r['status'] != 'overdue', r['action'] != 'verify_output',
                                       r['deadline_at'] or now, r['show_id'], r['episode']))
    from .official_releases import review_plan
    release_review = review_plan(db, now) if include_release_review else None
    return {'schema': 1, 'checked_at': now, 'deadline_seconds': DEADLINE, 'max_failures': MAX_FAILURES,
            'official_release_review': release_review,
            'items': items, 'next_action': actionable[0] if actionable else None,
            'summary': {s: sum(r['status'] == s for r in items) for s in
                        ('actionable', 'overdue', 'retry_wait', 'source_wait', 'repair_blocked',
                         'reconciling', 'needs_user', 'waiting_media', 'unreleased', 'protected', 'ready')}}


def record_attempt(db, plan, job_id, action, outcome, evidence, now=None, *,
                   expected_input=None, expected_condition=None):
    """Record actual bounded repair evidence, not checks/refreshes or self-certification."""
    now = time.time() if now is None else now
    row = next((r for r in plan['items'] if r['job_id'] == job_id), None)
    if row and row.get('action') != action:
        row = next((r for r in row.get('independent_routes', []) if r['action'] == action), row)
    if expected_input is not None or expected_condition is not None:
        if (not row or row.get('action') != action
                or row.get('input_key') != expected_input
                or row.get('condition') != expected_condition):
            raise ValueError('repair input or condition changed; reconcile the evidence against the current plan')
    if outcome not in ('failed', 'progress', 'blocked') or not evidence or len(evidence) > 16:
        raise ValueError('an actual result and up to sixteen evidence files are required')
    root = (db.path.parent / 'subtitle-studio').resolve()
    snapshots = archive(db) / 'evidence'
    snapshots.mkdir(parents=True, exist_ok=True)
    refs = []
    for value in evidence:
        p = Path(value).resolve()
        if not p.is_relative_to(root) or not p.is_file() or p.stat().st_size > 1048576:
            raise ValueError('repair evidence must be a small private archive file')
        raw = p.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        snapshot = snapshots / (sha + '.json')
        try:
            with snapshot.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            if snapshot.read_bytes() != raw:
                raise ValueError('immutable recovery evidence conflict')
        refs.append({'path': str(p), 'sha256': sha, 'snapshot': str(snapshot)})
    # Content, not a mutable path or reference ordering, identifies an attempt.
    hashes = sorted({r['sha256'] for r in refs})
    folder = archive(db) / 'attempts'
    folder.mkdir(parents=True, exist_ok=True)
    if row and row['action'] == action:
        # Old gate-dependent receipts must also remain idempotent after upgrade.
        for old in attempts(archive(db)):
            if (old.get('job_id') == job_id and old.get('input_key') == row['input_key']
                    and old.get('condition') in row.get('condition_aliases', [row['condition']])
                    and old.get('action') == action
                    and sorted({r.get('sha256') for r in old.get('evidence') or []}) == hashes):
                if old.get('outcome') != outcome:
                    raise ValueError('same evidence already recorded with a different outcome')
                return old
        identity = hashlib.sha256(json.dumps([row['condition'], action, hashes], sort_keys=True).encode()).hexdigest()
        path = folder / (identity + '.json')
        if path.exists():
            old = read_json(path)
            if old.get('outcome') != outcome:
                raise ValueError('same evidence already recorded with a different outcome')
            return old
    else:
        # The planner may already have advanced to another route. A byte-identical
        # replay of the old route is still idempotent, not a new attempt there.
        replay = []
        for old in attempts(archive(db)):
            old_hashes = sorted({r.get('sha256') for r in old.get('evidence') or []})
            if (old.get('job_id') == job_id and old.get('action') == action
                    and old.get('outcome') == outcome and old_hashes == hashes):
                replay.append(old)
        if len(replay) == 1:
            return replay[0]
        raise ValueError('repair is protected, stale or for a different action')
    # A cooldown/exhaustion blocks NEW duplicate failures, not idempotent replay
    # or a genuinely new progress result. Observation alone is never an attempt.
    allowed = ('actionable', 'overdue') if outcome in ('failed', 'blocked') else (
        'actionable', 'overdue', 'retry_wait', 'source_wait', 'repair_blocked')
    if row['status'] not in allowed:
        raise ValueError('repair is protected, cooling down, exhausted or stale')
    fields = ('job_id', 'show_id', 'episode', 'input_key', 'condition', 'status', 'action',
              'reason', 'failed_attempts', 'deadline_at', 'next_check_at', 'source_revision')
    record = {k: row.get(k) for k in fields}
    record.update(checked_at=now, action=action, outcome=outcome, evidence=refs)
    encoded = json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8')
    if len(encoded) > 32768:
        raise ValueError('recovery attempt exceeds the bounded journal schema')
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(encoded.decode('utf-8'))
    except FileExistsError:
        old = read_json(path)
        if old.get('outcome') != outcome:
            raise ValueError('same evidence already recorded with a different outcome')
        return old
    return record


def acknowledge_notice(db, plan, notification_key):
    """Called after a user-visible escalation was actually emitted, never as observation."""
    if not any(r['notification_key'] == notification_key and r['status'] == 'needs_user' for r in plan['items']):
        raise ValueError('notice does not match a current intervention request')
    folder = archive(db) / 'notices'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (notification_key + '.json')
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'notification_key': notification_key, 'delivered_at': time.time()}, stream)
    except FileExistsError:
        pass
    return {'notification_key': notification_key, 'acknowledged': True}


def update_monitor(db_path):
    """Always leave a fresh monitor receipt, including a failed snapshot."""
    now = time.time()
    root = Path(db_path).resolve().parent / 'diagnostics' / 'subtitle-recovery'
    root.mkdir(parents=True, exist_ok=True)
    try:
        from .read_projection import read_projection
        from .subtitle_delivery import SubtitleDelivery
        with read_projection(db_path) as db:
            snapshot = SubtitleDelivery(db, initialize=False).snapshot()
            plan = snapshot['recovery']
        now = plan['checked_at']
        try:
            previous = read_json(root / 'status.json', 4 * 1024 * 1024)
        except (OSError, ValueError, UnicodeError):
            previous = {}
        current = plan.get('next_action')
        execution = {'status': 'idle', 'stalled_since': None}
        if current:
            old = previous.get('next_action') or {}
            same = (old.get('job_id') == current['job_id'] and old.get('input_key') == current.get('input_key')
                    and (old.get('last_attempt_at') or 0) >= (current.get('last_attempt_at') or 0))
            old_execution = previous.get('execution') or {}
            since = ((old_execution.get('stalled_since') or previous.get('checked_at') or now)
                     if same else now)
            execution = {'status': 'stalled' if now - since >= EXECUTION_STALL else 'pending',
                         'stalled_since': since,
                         'job_id': current['job_id'], 'action': current['action'],
                         'condition': current['condition']}
        plan['execution'] = execution
        error = plan.get('error')
    except Exception as exc:
        # Never leave an old healthy/actionable file behind after a new failure.
        error = f'{type(exc).__name__}: {exc}'[:500]
        execution = {'status': 'error', 'stalled_since': None}
        plan = {'schema': 1, 'checked_at': now, 'error': error, 'items': [],
                'next_action': None, 'summary': {}, 'execution': execution}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root, delete=False) as stream:
        temp = Path(stream.name)
        json.dump(plan, stream, ensure_ascii=False, indent=2)
    try:
        temp.replace(root / 'status.json')
    finally:
        temp.unlink(missing_ok=True)
    return {'checked_at': now, 'summary': plan['summary'], 'error': error,
            'execution': execution}
