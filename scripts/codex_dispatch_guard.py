"""Bounded, local no-progress guard for the two AnimeService AI dispatchers.

Reads Codex projections/logs only. Never opens the AnimeService database, starts
an AI turn, releases a lease, or interrupts an episode production thread.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
# Dispatcher thread ids, their heartbeat automation ids, the explicitly registered
# recovery turns (inert once ended), the project path and the Codex database names
# live in one file shared with codex_dispatch_control.cjs; ANIMESERVICE_DISPATCH_CONFIG
# points both scripts at another copy.
CONFIG_PATH = Path(os.environ.get('ANIMESERVICE_DISPATCH_CONFIG') or ROOT / 'scripts/codex_dispatch.json')
DEFAULTS = {'cwd': 'D:/AnimeService', 'targets': {}, 'recovery_turns': {}, 'wait_seconds': 20 * 60, 'node': None,
            'codex_home': None, 'db_files': {'state': 'state_5.sqlite', 'history': 'thread_history_1.sqlite', 'logs': 'logs_2.sqlite'},
            'run_dir': 'data/diagnostics/codex-dispatch-guard'}


def normalize_cwd(cwd):
    return str(cwd).replace('\\', '/').removeprefix('//?/').rstrip('/').lower()


def load_config(path=CONFIG_PATH):
    raw = Path(path).read_bytes()
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):raise ValueError('dispatcher config must be an object')
    config = {**DEFAULTS, **value}
    if not isinstance(config['targets'], dict) or not config['targets']:
        raise ValueError(f'{path}: targets must map dispatcher thread ids to automation ids')
    if any(not isinstance(k,str) or not k.strip() or not isinstance(v,str) or not v.strip() for k,v in config['targets'].items()):
        raise ValueError('dispatcher targets must contain nonempty strings')
    recovery=config['recovery_turns']
    if not isinstance(recovery,dict) or any(k not in config['targets'] or not isinstance(v,str) or not v for k,v in recovery.items()):
        raise ValueError('recovery_turns must map configured targets to turn ids')
    wait = config['wait_seconds']  # seconds without log progress before a scheduled turn counts as stale
    if isinstance(wait, bool) or not isinstance(wait, int) or not 60 <= wait <= 86400:
        config['wait_seconds'] = DEFAULTS['wait_seconds']  # outside 60..86400 or not an integer: the former hard-coded 20 minutes
    if not isinstance(config['cwd'], str) or not config['cwd'].strip():
        raise ValueError(f'{path}: cwd must name the dispatcher project directory')
    if not isinstance(config['db_files'],dict):raise ValueError('db_files must be an object')
    missing = {'state', 'history', 'logs'} - set(config['db_files'])
    if missing:
        raise ValueError(f'{path}: db_files is missing {sorted(missing)}')
    if any(not isinstance(v,str) or not v or '/' in v or '\\' in v or ':' in v or v in ('.','..') for v in config['db_files'].values()):
        raise ValueError('db_files must contain filenames, not paths')
    if len(set(config['db_files'].values()))!=len(config['db_files']):raise ValueError('db_files must be distinct')
    if not isinstance(config['run_dir'],str) or not config['run_dir'].strip():raise ValueError('run_dir must be a nonempty path')
    for key in ('node','codex_home'):
        if config[key] is not None and (not isinstance(config[key],str) or not config[key].strip()):raise ValueError(f'{key} must be a path or null')
    config['_config_path']=str(Path(path).resolve())
    config['_config_sha256']=hashlib.sha256(raw).hexdigest()
    return config


try:
    CONFIG = load_config(); CONFIG_ERROR = None
except (OSError,ValueError,TypeError) as error:
    CONFIG = dict(DEFAULTS); CONFIG_ERROR = type(error).__name__ + ': invalid or unreadable dispatcher configuration'
TARGETS = CONFIG['targets']
RECOVERY_TURNS = CONFIG['recovery_turns']
WAIT_SECONDS = CONFIG['wait_seconds']
RUN_DIR = ROOT / CONFIG['run_dir']


def node_path(config):
    """node from PATH first, else the configured absolute path; None when neither exists."""
    found = shutil.which('node')
    if found:
        return found
    configured = config.get('node')
    return configured if configured and Path(configured).exists() else None


def readonly(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def decide(thread_id, cwd, turn, items, last_log, now, config=None):
    config = config or CONFIG
    targets, recovery_turns = config['targets'], config['recovery_turns']
    if thread_id not in targets or normalize_cwd(cwd) != normalize_cwd(config['cwd']):
        return 'outside_scope'
    if not turn or turn['status'] != 'inProgress':
        return 'idle'
    # Only scheduled turns or the two explicitly registered recovery turns.
    user = next((x for x in items if x.get('type') == 'userMessage'), None)
    text = ''.join(x.get('text', '') for x in (user or {}).get('content', []) if x.get('type') == 'text')
    scheduled = '<heartbeat>' in text and f'<automation_id>{targets[thread_id]}</automation_id>' in text
    # App-delivered recovery prompts are functionCallOutput delegation items,
    # not userMessage items. Exact pre-registered turn IDs are the authority.
    recovery = turn.get('turn_id') == recovery_turns.get(thread_id)
    if not scheduled and not recovery:
        return 'not_scheduled_turn'
    if any(x.get('status') in ('inProgress', 'running', 'pending') for x in items):
        return 'tool_in_progress'
    progress = max(float(turn.get('started_at') or now), float(last_log or 0))
    if now - progress < config['wait_seconds']:
        return 'recent_progress'
    return 'interrupt_stale_model_wait'


def snapshot(home, now, config=None):
    config = config or CONFIG
    files = config['db_files']
    out = []
    with readonly(home / files['state']) as state, readonly(home / files['history']) as history, readonly(home / files['logs']) as logs:
        for tid in config['targets']:
            thread = state.execute('SELECT cwd FROM threads WHERE id=?', (tid,)).fetchone()
            row = history.execute('SELECT turn_id,status,started_at,rollout_ordinal FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1', (tid,)).fetchone()
            turn = dict(row) if row else None
            items = [json.loads(x[0]) for x in history.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY rollout_ordinal', (tid, turn['turn_id']))] if turn else []
            # Indexed lookup stays small; do not scan multi-GB rollout files.
            last = logs.execute('SELECT ts FROM logs WHERE thread_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
            latest = last[0] if last else 0
            reason = decide(tid, thread['cwd'] if thread else '', turn, items, latest, now, config)
            out.append({'thread_id': tid, 'turn_id': turn['turn_id'] if turn else None, 'decision': reason, 'last_log_at': latest})
    return out


def run(apply, home=None, now=None, config=None):
    """One guard pass. The status file is always written, with `error` set when the
    Codex projections could not be read; control results are recorded per thread."""
    config_error = CONFIG_ERROR if config is None else None
    config = CONFIG if config is None else config
    now = time.time() if now is None else now
    home = Path(home or config.get('codex_home') or Path.home() / '.codex')
    run_dir = Path(config['run_dir'])
    run_dir = run_dir if run_dir.is_absolute() else ROOT / run_dir
    status = {'at': now, 'apply': apply, 'threshold_seconds': config['wait_seconds'], 'threads': [], 'error': None}
    run_dir.mkdir(parents=True, exist_ok=True)
    if config_error:status['error']=config_error
    else:
        try:status['threads'] = snapshot(home, now, config)
        except Exception as e:status['error'] = f'{type(e).__name__}: {e}'
    for row in status['threads']:
        if not apply or row['decision'] != 'interrupt_stale_model_wait':
            continue
        # Native expected-turn check closes the race with completion/new work.
        control = ROOT / 'scripts/codex_dispatch_control.cjs'
        node = node_path(config)
        try:
            if not node:
                raise FileNotFoundError('node not found on PATH and the configured path does not exist')
            config_path=Path(config['_config_path'])
            if hashlib.sha256(config_path.read_bytes()).hexdigest()!=config['_config_sha256']:
                raise ValueError('dispatcher configuration changed during this pass')
            p = subprocess.run([node, str(control), row['thread_id'], row['turn_id']], capture_output=True, text=True, encoding='utf-8', timeout=25,
                               env={**os.environ, 'ANIMESERVICE_DISPATCH_CONFIG': str(config_path)}, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            row['control_exit_code'] = p.returncode
            row['control_receipt'] = p.stdout.strip()[:2000]
            row['control_error'] = p.stderr.strip()[:500]
        except Exception as e:  # the decision stays on record even when the control step could not run
            row['control_exit_code'] = None
            row['control_receipt'] = ''
            row['control_error'] = f'{type(e).__name__}: {e}'[:500]
        try:
            with (run_dir / 'actions.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps({'at': now, **row}, ensure_ascii=False) + '\n')
        except OSError as error:status['error']='action_log: '+type(error).__name__
    # Concurrent diagnostic/apply invocations must not share one temporary file.
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=run_dir,prefix='status-',suffix='.tmp',delete=False) as f:
        tmp=Path(f.name);f.write(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
    try:tmp.replace(run_dir/'status.json')
    finally:tmp.unlink(missing_ok=True)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    status = run(args.apply)
    print(json.dumps(status, ensure_ascii=False))
    sys.exit(1 if status['error'] or any(row.get('control_error') or row.get('control_exit_code') not in (None,0) for row in status['threads']) else 0)


if __name__ == '__main__':
    main()
