"""Read-only diagnostic snapshot of the local Codex projections for one dispatch.

This is NOT an absence verifier: it never returns an authorization, is never
injected into SubtitleDelivery, performs no IPC and writes nothing but the report
it was asked for. It collects what the projections say about one dispatcher turn
(status, watermark, create/reuse call items, spawn edges) with whitelisted fields
only, so an owner can compare that with a dispatch ticket. Every report carries
diagnostic_only=true and absence_authorized=false; exit code 0 means only that
the declared diagnostic scope ran to completion. See docs/DISPATCH-ABSENCE-EVIDENCE.md.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.subtitle_delivery import validate_ticket  # noqa: E402

SCHEMA = 'codex-projection-diagnostic/v1'
METHOD = 'codex-projection-readonly/v1'
CREATE_TOOLS = ('create_thread', 'send_message_to_thread')
# Columns this collector needs; anything missing makes the projection unsupported.
REQUIRED = {
    'state': {'threads': {'id', 'created_at_ms', 'cwd'}, 'thread_spawn_edges': {'parent_thread_id', 'child_thread_id', 'status'}},
    'history': {'thread_turns': {'thread_id', 'turn_id', 'status', 'started_at', 'completed_at', 'rollout_end_ordinal'},
                'thread_items': {'thread_id', 'turn_id', 'item_id', 'item_type', 'item_json', 'started_at_ms', 'completed_at_ms'},
                'thread_history_projection_state': {'thread_id', 'next_rollout_ordinal'}},
    'logs': {'logs': {'thread_id', 'ts', 'id'}},
}
# Declared units per column (verified against the local schema on 2026-10-18); never guessed from magnitudes.
UNITS = {'ticket.issued_at': 'seconds', 'thread_turns.started_at': 'seconds', 'thread_turns.completed_at': 'seconds',
         'thread_items.started_at_ms': 'milliseconds', 'thread_items.completed_at_ms': 'milliseconds',
         'threads.created_at_ms': 'milliseconds', 'logs.ts': 'seconds'}
RANGES = {'seconds': (1e9, 4e9), 'milliseconds': (1e12, 4e12)}
INCOMPLETE, USAGE = 3, 2


def _load_guard_config():
    spec = importlib.util.spec_from_file_location('codex_dispatch_guard', ROOT / 'scripts/codex_dispatch_guard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest() if value not in (None, '') else ''


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def in_range(value, unit):
    low, high = RANGES[unit]
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= float(value) <= high


def parse_host_ref(ref):
    """thread:<id>[/turn:<id>[/item:<id>]]; anything else is unbound."""
    if not isinstance(ref, str) or not ref:
        return {}
    parts = {}
    for piece in ref.split('/'):
        key, sep, value = piece.partition(':')
        if not sep or key not in ('thread', 'turn', 'item') or not value or key in parts:
            return {}
        parts[key] = value
    return parts if 'thread' in parts else {}


class Budget:
    def __init__(self, seconds, max_rows, clock=time.monotonic):
        self.deadline = clock() + seconds
        self.max_rows = max_rows
        self.clock = clock
        self.exhausted = False

    def check(self):
        if self.clock() > self.deadline:
            self.exhausted = True
        return not self.exhausted


def open_ro(path, busy_ms):
    """Read-only, query-only connection in one short deferred read transaction; WAL side files are left alone."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=busy_ms / 1000)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute(f'PRAGMA busy_timeout={int(busy_ms)}')
    con.execute('BEGIN')
    return con


def schema_check(con, required):
    missing = []
    for table, columns in required.items():
        present = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        if not present:
            missing.append(table)
        else:
            missing += [f'{table}.{c}' for c in sorted(columns - present)]
    versions = [row[0] for row in con.execute("SELECT version FROM _sqlx_migrations ORDER BY version")] if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations'").fetchone() else []
    return missing, versions


def collect(config, ticket, host_ref, now=None, time_limit=20.0, max_rows=200, busy_ms=2000, clock=time.monotonic):
    """Build the diagnostic report; returns (report, exit_code). Never raises for projection problems."""
    now = now if now is not None else time.time()
    budget = Budget(time_limit, max_rows, clock)
    home = Path(config.get('codex_home') or Path.home() / '.codex')
    ref = parse_host_ref(host_ref)
    report = {'schema': SCHEMA, 'method': METHOD, 'diagnostic_only': True, 'absence_authorized': False,
              'job_digest': digest(ticket['job_id']), 'ticket_digest': digest(json.dumps(ticket, sort_keys=True, ensure_ascii=False)),
              'dispatch_digest': digest(ticket['dispatch_token']), 'dispatch_action': ticket['dispatch_action'],
              'ticket_thread_digest': digest(ticket['thread_id']), 'host_ref_binding': 'unbound', 'host_ref_thread_digest': digest(ref.get('thread')),
              'units': UNITS, 'observations': [], 'screening': {}, 'coverage': {'truncated': False, 'cross_database_atomic': False, 'databases': {}},
              'gaps': ['host_operation_terminal_api_unavailable', 'marker_check_not_performed'], 'projection_files': [], 'schema_versions': {},
              'collected_from': iso(now), 'collected_until': None}
    issued = ticket['issued_at']
    if not in_range(issued, 'seconds'):
        report['gaps'].append('ticket_issued_at_out_of_range')
        return _finish(report, now, INCOMPLETE)
    thread = ref.get('thread')
    if thread and thread not in config['targets']:
        report['gaps'].append('thread_outside_dispatcher_scope')
        return _finish(report, now, INCOMPLETE)
    candidates = [thread] if thread else sorted(config['targets'])
    if not thread:
        report['gaps'].append('host_ref_unbound')
    report['host_ref_binding'] = 'exact' if ref.get('turn') else ('thread_only' if thread else 'unbound')
    cons = {}
    for key in ('state', 'history', 'logs'):
        path = home / config['db_files'][key]
        entry = {'opened': False, 'path_digest': digest(str(path)), 'started': iso(time.time()), 'rows_truncated': False}
        report['coverage']['databases'][key] = entry
        try:
            stat = path.stat()
            report['projection_files'].append({'db': key, 'size': stat.st_size, 'mtime_ms': int(stat.st_mtime * 1000)})
            con = open_ro(path, busy_ms)
            missing, versions = schema_check(con, REQUIRED[key])
            report['schema_versions'][key] = versions
            if missing:
                report['gaps'].append(f'unsupported_schema:{key}:' + ','.join(missing))
                con.close()
                continue
            cons[key] = con
            entry['opened'] = True
        except (OSError, sqlite3.Error) as e:
            report['gaps'].append(f'database_unavailable:{key}:{type(e).__name__}')
    try:
        if 'history' in cons:
            _history(report, cons['history'], ref, candidates, issued, now, budget)
        if 'state' in cons:
            _state(report, cons['state'], candidates, issued, budget)
        if 'logs' in cons and budget.check():
            for tid in candidates:
                row = cons['logs'].execute('SELECT ts FROM logs WHERE thread_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
                report['observations'].append({'kind': 'last_log', 'thread_digest': digest(tid),
                                               'ts': row['ts'] if row and in_range(row['ts'], 'seconds') else None,
                                               'unit': 'seconds', 'note': 'silence proves nothing about side effects'})
    except sqlite3.Error as e:
        report['gaps'].append(f'query_failed:{type(e).__name__}')
    finally:
        for key, con in cons.items():
            report['coverage']['databases'][key]['finished'] = iso(time.time())
            try:
                con.rollback(); con.close()
            except sqlite3.Error:
                pass
    if budget.exhausted:
        report['gaps'].append('time_limit_exceeded')
    report['coverage']['truncated'] = budget.exhausted or any(d.get('rows_truncated') for d in report['coverage']['databases'].values())
    code = INCOMPLETE if (report['coverage']['truncated'] or any(g.startswith(('unsupported_schema', 'database_unavailable', 'query_failed')) for g in report['gaps'])) else 0
    return _finish(report, now, code)


def _history(report, con, ref, candidates, issued, now, budget):
    coverage = report['coverage']['databases']['history']
    turns = []
    for tid in candidates:
        if not budget.check():
            return
        if ref.get('turn') and tid == ref['thread']:
            rows = con.execute('SELECT thread_id,turn_id,status,started_at,completed_at,rollout_end_ordinal,error_json IS NOT NULL AS has_error FROM thread_turns WHERE thread_id=? AND turn_id=?',
                               (tid, ref['turn'])).fetchall()
            if not rows:
                report['gaps'].append('turn_not_found')
        else:
            # Degraded: every turn overlapping [issued_at, now], unfinished ones included; a turn may have started before begin.
            rows = con.execute('''SELECT thread_id,turn_id,status,started_at,completed_at,rollout_end_ordinal,error_json IS NOT NULL AS has_error
                FROM thread_turns WHERE thread_id=? AND started_at<=? AND (completed_at IS NULL OR completed_at>=?) ORDER BY started_at LIMIT ?''',
                               (tid, now, issued, budget.max_rows + 1)).fetchall()
            if len(rows) > budget.max_rows:
                coverage['rows_truncated'] = True
                rows = rows[:budget.max_rows]
        turns += rows
    water = {}
    for tid in candidates:
        row = con.execute('SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?', (tid,)).fetchone()
        water[tid] = row['next_rollout_ordinal'] if row else None
    ended, consumed, no_thread_result = [], [], []
    for turn in turns:
        if not budget.check():
            return
        started_ok = in_range(turn['started_at'], 'seconds')
        if not started_ok:
            report['gaps'].append('unit_anomaly:thread_turns.started_at')
        obs = {'kind': 'turn', 'thread_digest': digest(turn['thread_id']), 'turn_digest': digest(turn['turn_id']), 'status': turn['status'],
               'has_error': bool(turn['has_error']), 'started_before_begin': (turn['started_at'] < issued) if started_ok else None,
               'rollout_end_ordinal': turn['rollout_end_ordinal'], 'projection_next_ordinal': water.get(turn['thread_id'])}
        obs['watermark'] = ('consumed' if water.get(turn['thread_id']) is not None and turn['rollout_end_ordinal'] is not None
                            and water[turn['thread_id']] > turn['rollout_end_ordinal'] else
                            'lagging' if water.get(turn['thread_id']) is not None and turn['rollout_end_ordinal'] is not None else 'unknown')
        items = []
        try:
            cursor = con.execute('''SELECT item_id, started_at_ms, completed_at_ms,
                json_extract(item_json,'$.tool') AS tool, json_extract(item_json,'$.status') AS status,
                json_extract(item_json,'$.error') IS NOT NULL AS has_error,
                json_type(item_json,'$.result.structuredContent') AS structured_type,
                COALESCE(json_extract(item_json,'$.result.structuredContent.threadId'), json_extract(item_json,'$.result.structuredContent.thread_id'),
                         json_extract(item_json,'$.result.structuredContent.id')) AS thread_value
                FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type='mcpToolCall'
                AND json_extract(item_json,'$.tool') IN (?,?) ORDER BY rowid LIMIT ?''',
                                 (turn['thread_id'], turn['turn_id'], *CREATE_TOOLS, budget.max_rows + 1))
            rows = cursor.fetchall()
        except sqlite3.Error:
            report['gaps'].append('item_json_malformed')
            rows = []
        if len(rows) > budget.max_rows:
            coverage['rows_truncated'] = True
            rows = rows[:budget.max_rows]
        for item in rows:
            items.append({'item_digest': digest(item['item_id']), 'tool': item['tool'], 'status': item['status'], 'has_error': bool(item['has_error']),
                          'structured_type': item['structured_type'], 'thread_result': item['thread_value'] is not None,
                          'thread_result_digest': digest(item['thread_value']), 'matches_ticket_thread': bool(item['thread_value']) and digest(item['thread_value']) == report['ticket_thread_digest'],
                          'started_at_ms_in_range': in_range(item['started_at_ms'], 'milliseconds') if item['started_at_ms'] is not None else None})
        obs['create_or_reuse_calls'] = items
        obs['call_summary'] = ('none' if not items else 'thread_result' if any(i['thread_result'] for i in items)
                               else 'failed_only' if all(i['has_error'] or i['status'] not in ('completed', 'success') for i in items) else 'no_thread_result')
        report['observations'].append(obs)
        ended.append(turn['status'] in ('completed', 'failed', 'interrupted'))
        consumed.append(obs['watermark'] == 'consumed')
        no_thread_result.append(obs['call_summary'] in ('none', 'failed_only', 'no_thread_result'))
    report['screening'].update({'turn_ended': all(ended) if turns else None, 'watermark_consumed': all(consumed) if turns else None,
                                'calls_without_thread_result': all(no_thread_result) if turns else None, 'marker_check': 'not_performed',
                                'turns_examined': len(turns)})
    if not turns:
        report['gaps'].append('no_candidate_turn')


def _state(report, con, candidates, issued, budget):
    coverage = report['coverage']['databases']['state']
    children_after = 0; examined = 0
    for tid in candidates:
        if not budget.check():
            return
        rows = con.execute('SELECT child_thread_id,status FROM thread_spawn_edges WHERE parent_thread_id=? LIMIT ?', (tid, budget.max_rows + 1)).fetchall()
        if len(rows) > budget.max_rows:
            coverage['rows_truncated'] = True
            rows = rows[:budget.max_rows]
        for edge in rows:
            examined += 1
            child = con.execute('SELECT created_at_ms FROM threads WHERE id=?', (edge['child_thread_id'],)).fetchone()
            obs = {'kind': 'spawn_edge', 'parent_digest': digest(tid), 'child_digest': digest(edge['child_thread_id']), 'edge_status': edge['status'],
                   'child_created_at_ms': child['created_at_ms'] if child else None}
            if not child:
                obs['note'] = 'edge without thread row (cross-database or lag)'
                report['gaps'].append('child_thread_missing')
            elif not in_range(child['created_at_ms'], 'milliseconds'):
                report['gaps'].append('unit_anomaly:threads.created_at_ms')
            elif child['created_at_ms'] >= issued * 1000:
                children_after += 1
            report['observations'].append(obs)
    report['screening'].update({'spawn_children_after_issued': children_after, 'spawn_edges_examined': examined})


def _finish(report, now, code):
    report['collected_until'] = iso(time.time())
    report['gaps'] = sorted(set(report['gaps']))
    report['screening'].setdefault('absence_authorized', False)
    report['exit_code'] = code
    return report, code


def write_report(report, out):
    """Exclusive create, canonical JSON, sidecar sha256; the hash binds the file, not the live databases."""
    data = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    path = Path(out)
    with open(path, 'xb') as f:
        f.write(data)
    sha = hashlib.sha256(data).hexdigest()
    (path.with_name(path.name + '.sha256')).write_text(sha + '\n', encoding='utf-8')
    return sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='required: the only mode; the report is diagnostic and authorizes nothing')
    parser.add_argument('--ticket', required=True, help='archived begin ticket (dispatch-ticket/v1); only digests are reported')
    parser.add_argument('--host-ref', default='', help='thread:<id>[/turn:<id>[/item:<id>]]; missing turn degrades to window candidates')
    parser.add_argument('--out', required=True, help='report path; must not exist')
    parser.add_argument('--config', default=None, help='codex_dispatch.json (default: the guard configuration)')
    parser.add_argument('--time-limit', type=float, default=20.0)
    parser.add_argument('--max-rows', type=int, default=200)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error('--dry-run is required: this collector never authorizes anything')
    if Path(args.out).exists():
        parser.error('--out must not exist (reports are immutable)')
    guard = _load_guard_config()
    config = guard.load_config(args.config) if args.config else guard.CONFIG
    try:
        ticket = validate_ticket(json.loads(Path(args.ticket).read_text(encoding='utf-8')))
    except (OSError, ValueError) as e:
        parser.error(f'ticket unusable: {e}')
    report, code = collect(config, ticket, args.host_ref, time_limit=args.time_limit, max_rows=max(1, args.max_rows))
    sha = write_report(report, args.out)
    print(json.dumps({'report': str(args.out), 'sha256': sha, 'exit_code': code, 'absence_authorized': False, 'gaps': report['gaps']}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
