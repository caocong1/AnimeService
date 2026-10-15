"""Bounded, local no-progress guard for the two AnimeService AI dispatchers.

Reads Codex projections/logs only. Never opens the AnimeService database, starts
an AI turn, releases a lease, or interrupts an episode production thread.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    '01a10c1a-eea3-7070-bd80-5f8cee6426a1': 'automation-3',
    '01a125c3-0885-7191-9acd-0789c73f6ebb': 'automation-2',
}
# Explicit recovery turns dispatched by this repair; all other manual work is
# excluded. These immutable IDs become inert once their turns have ended.
RECOVERY_TURNS = {
    '01a10c1a-eea3-7070-bd80-5f8cee6426a1': '01a13f9c-66e7-7ad0-bd73-3997b6cfc773',
    '01a125c3-0885-7191-9acd-0789c73f6ebb': '01a13f9c-6b91-74b0-b187-b05b71fee152',
}
WAIT_SECONDS = 20 * 60
RUN_DIR = ROOT / 'data/diagnostics/codex-dispatch-guard'


def readonly(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def decide(thread_id, cwd, turn, items, last_log, now):
    normalized = str(cwd).replace('\\', '/').removeprefix('//?/').rstrip('/').lower()
    if thread_id not in TARGETS or normalized != 'd:/animeservice':
        return 'outside_scope'
    if not turn or turn['status'] != 'inProgress':
        return 'idle'
    # Only scheduled turns or the two explicitly registered recovery turns.
    user = next((x for x in items if x.get('type') == 'userMessage'), None)
    text = ''.join(x.get('text', '') for x in (user or {}).get('content', []) if x.get('type') == 'text')
    scheduled = '<heartbeat>' in text and f'<automation_id>{TARGETS[thread_id]}</automation_id>' in text
    # App-delivered recovery prompts are functionCallOutput delegation items,
    # not userMessage items. Exact pre-registered turn IDs are the authority.
    recovery = turn.get('turn_id') == RECOVERY_TURNS.get(thread_id)
    if not scheduled and not recovery:
        return 'not_scheduled_turn'
    if any(x.get('status') in ('inProgress', 'running', 'pending') for x in items):
        return 'tool_in_progress'
    progress = max(float(turn.get('started_at') or now), float(last_log or 0))
    if now - progress < WAIT_SECONDS:
        return 'recent_progress'
    return 'interrupt_stale_model_wait'


def snapshot(home, now):
    out = []
    with readonly(home / 'state_5.sqlite') as state, readonly(home / 'thread_history_1.sqlite') as history, readonly(home / 'logs_2.sqlite') as logs:
        for tid in TARGETS:
            thread = state.execute('SELECT cwd FROM threads WHERE id=?', (tid,)).fetchone()
            row = history.execute('SELECT turn_id,status,started_at,rollout_ordinal FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1', (tid,)).fetchone()
            turn = dict(row) if row else None
            items = [json.loads(x[0]) for x in history.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY rollout_ordinal', (tid, turn['turn_id']))] if turn else []
            # Indexed lookup stays small; do not scan multi-GB rollout files.
            last = logs.execute('SELECT ts FROM logs WHERE thread_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
            latest = last[0] if last else 0
            reason = decide(tid, thread['cwd'] if thread else '', turn, items, latest, now)
            out.append({'thread_id': tid, 'turn_id': turn['turn_id'] if turn else None, 'decision': reason, 'last_log_at': latest})
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    now = time.time()
    rows = snapshot(Path.home() / '.codex', now)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if not args.apply or row['decision'] != 'interrupt_stale_model_wait':
            continue
        # Native expected-turn check closes the race with completion/new work.
        control = ROOT / 'scripts/codex_dispatch_control.cjs'
        p = subprocess.run(['C:/Program Files/nodejs/node.exe', str(control), row['thread_id'], row['turn_id']], capture_output=True, text=True, encoding='utf-8', timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        row['control_exit_code'] = p.returncode
        row['control_receipt'] = p.stdout.strip()[:2000]
        row['control_error'] = p.stderr.strip()[:500]
        with (RUN_DIR / 'actions.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'at': now, **row}, ensure_ascii=False) + '\n')
    status = {'at': now, 'apply': args.apply, 'threshold_seconds': WAIT_SECONDS, 'threads': rows}
    tmp = RUN_DIR / 'status.tmp'
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(RUN_DIR / 'status.json')
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__':
    main()
