"""Watch current acknowledged subtitle writers; never release their slots.

The user's 2026-10-19 recovery authorization permits expected-turn cancellation
of a proven idle model wait, followed by same-owner recovery by automation-2.
All business and Codex databases are read-only here. Unknown dispatches, user
stops, other chats and running tools are excluded. No model call or resume.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from codex_dispatch_guard import (ROOT, claim_control, control_history, normalize_cwd,
                                  node_path, previous_control, readonly, record_outcome)

BUSY = {'inProgress', 'running', 'pending'}
MODEL_ITEMS = {'contextCompaction', 'reasoning', 'agentMessage', 'userMessage'}


def owners(db_path):
    with readonly(db_path) as db:
        rows = db.execute("""SELECT j.id,j.show_id,j.episode,j.thread_id,j.input_key,j.revision,j.payload
            FROM subtitle_delivery_jobs j JOIN shows s ON s.id=j.show_id
            LEFT JOIN watches w ON w.show_id=j.show_id AND w.episode=j.episode
            WHERE j.state IN ('production','blocked') AND j.dispatch_state='acknowledged'
              AND j.thread_id<>'' AND s.selected=1 AND s.state='watching'
              AND s.authorized=1 AND s.cleanup_hold=0
              AND coalesce(w.finished,0)=0""").fetchall()
        active = [dict(row) for row in rows
                  if not json.loads(row['payload']).get('owner_stopped_writing')]
        episodes = {(row['show_id'], row['episode']) for row in active}
        threads = {row['thread_id'] for row in active}
        if len(episodes) != len(active) or len(threads) != len(active):
            raise ValueError('Conflicting production writers; preserve all owners for reconciliation')
        return active


def decide(owner, cwd, turn, items, last_log, now, wait_seconds, project):
    if normalize_cwd(cwd) != normalize_cwd(project):
        return 'outside_scope'
    if not turn or turn['status'] != 'inProgress':
        return 'terminal_or_idle'
    # Compaction has no external writer; a stuck compaction needs the same
    # expected-turn protection as any other idle model request.
    if any(x.get('status') in BUSY and x.get('type') not in MODEL_ITEMS for x in items):
        return 'tool_in_progress'
    if now - max(float(turn.get('started_at') or now), float(last_log or 0)) < wait_seconds:
        return 'recent_progress'
    return 'interrupt_stale_owner_wait'


def snapshot(home, config, now):
    files = config['db_files']
    out = []
    with readonly(home / files['state']) as state, readonly(home / files['history']) as history, readonly(home / files['logs']) as logs:
        for owner in owners(config['owner_db']):
            tid = owner['thread_id']
            thread = state.execute('SELECT cwd FROM threads WHERE id=?', (tid,)).fetchone()
            row = history.execute('SELECT turn_id,status,started_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1', (tid,)).fetchone()
            turn = dict(row) if row else None
            items = [json.loads(x[0]) for x in history.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=?', (tid, turn['turn_id']))] if turn else []
            last = logs.execute('SELECT ts FROM logs WHERE thread_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
            latest = last[0] if last else 0
            out.append({k: owner[k] for k in ('id', 'thread_id', 'input_key', 'revision')} | {
                'turn_id': turn['turn_id'] if turn else None, 'last_log_at': latest,
                'decision': decide(owner, thread['cwd'] if thread else '', turn, items,
                                   latest, now, config['wait_seconds'], config['cwd'])})
    return out


def run(apply, home, config, now=None):
    now = time.time() if now is None else now
    run_dir = ROOT / 'data/diagnostics/codex-subtitle-owner-guard'
    run_dir.mkdir(parents=True, exist_ok=True)
    status = {'at': now, 'apply': apply, 'threads': [], 'error': None}
    actions = run_dir / 'actions.jsonl'
    try:
        status['threads'] = snapshot(home, config, now)
        previous = control_history(actions)
        for row in status['threads']:
            if not apply or row['decision'] != 'interrupt_stale_owner_wait':
                continue
            # One control attempt per exact turn, even if the host result was
            # unknown. A repeated incident is diagnosed by the dispatcher.
            if previous_control(row, previous):
                continue
            row['previous_interrupted_turns_24h'] = len({x['turn_id'] for x in previous
                if x['thread_id'] == row['thread_id'] and now - x['at'] < 86400})
            # Re-read both business ownership and latest turn immediately before
            # control; never authorize against an old observer snapshot.
            fresh = next((x for x in snapshot(home, config, time.time())
                          if all(x.get(k) == row.get(k) for k in ('id', 'thread_id', 'input_key', 'revision', 'turn_id'))), None)
            if not fresh or fresh['decision'] != 'interrupt_stale_owner_wait':
                row['decision'] = 'changed_before_control'
                continue
            # Write intent BEFORE IPC. A crash/unknown result cannot duplicate it.
            if not claim_control(run_dir, row, now):
                continue
            # The existing native control accepts only this fresh exact turn.
            scoped = {**config, 'targets': {row['thread_id']: 'subtitle-production-owner'},
                      'recovery_turns': {row['thread_id']: row['turn_id']},
                      'exact_turns_only': True}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', dir=run_dir, delete=False) as f:
                temp = Path(f.name)
                json.dump(scoped, f)
            try:
                result = subprocess.run([node_path(config), str(ROOT/'scripts/codex_dispatch_control.cjs'), row['thread_id'], row['turn_id']],
                    env={**os.environ, 'ANIMESERVICE_DISPATCH_CONFIG': str(temp)},
                    capture_output=True, text=True, encoding='utf-8', timeout=25,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                row.update(control_exit_code=result.returncode, control_receipt=result.stdout[:2000], control_error=result.stderr[:500])
                record_outcome(row)
            finally:
                temp.unlink(missing_ok=True)
            with actions.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'at': now, **row, 'control_state': 'result'}) + '\n')
        if any(row.get('reconciliation_required') for row in status['threads']):
            status['error'] = 'owner_control_requires_review'
    except Exception as error:
        status['error'] = f'{type(error).__name__}: {error}'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=run_dir, delete=False) as f:
        temp = Path(f.name)
        json.dump(status, f, ensure_ascii=False, indent=2)
    temp.replace(run_dir / 'status.json')
    return status
