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
import base64
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.subtitle_delivery import validate_ticket, _canonical  # noqa: E402

SCHEMA = 'codex-projection-diagnostic/v1'
METHOD = 'codex-projection-readonly/v1'
CREATE_TOOLS = ('create_thread', 'send_message_to_thread')
# Columns this collector needs; anything missing makes the projection unsupported.
REQUIRED = {
    'state': {'threads': {'id', 'created_at_ms', 'cwd'}, 'thread_spawn_edges': {'parent_thread_id', 'child_thread_id', 'status'}},
    'history': {'thread_turns': {'thread_id', 'turn_id', 'status', 'started_at', 'completed_at', 'rollout_end_ordinal', 'error_json'},
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
# Exact migration sets inspected using schema metadata only on 2026-10-18.
# New migrations require owner review even when the old columns still exist.
KNOWN_MIGRATIONS = {'state': tuple(range(1, 62)), 'history': tuple(range(1, 9)), 'logs': (1, 2)}
MAX_REPORT_BYTES = 1024 * 1024


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
        if not sep or key not in ('thread', 'turn', 'item') or not 1 <= len(value) <= 256 or key in parts:
            return {}
        parts[key] = value
    return parts if list(parts) in (['thread'], ['thread', 'turn'], ['thread', 'turn', 'item']) else {}


class Budget:
    def __init__(self, seconds, max_rows, clock=time.monotonic):
        self.deadline = clock() + seconds
        self.max_rows = max_rows
        self.clock = clock
        self.exhausted = False
        self.rows = 0

    def check(self):
        if self.clock() > self.deadline:
            self.exhausted = True
        return not self.exhausted

    def take(self, rows, coverage):
        remaining = max(0, self.max_rows - self.rows)
        if len(rows) > remaining:
            coverage['rows_truncated'] = True
        result = rows[:remaining]
        self.rows += len(result)
        return result


def open_ro(path, busy_ms, budget=None):
    """Read-only, query-only connection in one short deferred read transaction; WAL side files are left alone."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=busy_ms / 1000)
    con.row_factory = sqlite3.Row
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
    if budget:
        con.set_progress_handler(lambda: int(not budget.check()), 1000)
    con.execute('PRAGMA query_only=ON')
    con.execute(f'PRAGMA busy_timeout={int(busy_ms)}')
    con.execute('BEGIN')
    return con


def schema_check(con, required, key):
    missing = []
    for table, columns in required.items():
        present = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        if not present:
            missing.append(table)
        else:
            missing += [f'{table}.{c}' for c in sorted(columns - present)]
    versions = [row[0] for row in con.execute("SELECT version FROM _sqlx_migrations ORDER BY version LIMIT 201")] if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations'").fetchone() else []
    if tuple(versions) != KNOWN_MIGRATIONS[key]:
        missing.append('unknown_migration_set')
    return missing, versions


def normalize_cwd(value):
    return str(value).replace('\\', '/').removeprefix('//?/').rstrip('/').lower()


def validate_limits(time_limit, max_rows, busy_ms):
    if (isinstance(time_limit, bool) or not isinstance(time_limit, (int, float))
            or not math.isfinite(time_limit) or not 0.05 <= time_limit <= 30
            or isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 1000
            or isinstance(busy_ms, bool) or not isinstance(busy_ms, int) or not 0 <= busy_ms <= 2000):
        raise ValueError('invalid bounded diagnostic limits')


def collect(config, ticket, host_ref, now=None, time_limit=20.0, max_rows=200, busy_ms=2000, clock=time.monotonic):
    """Build the diagnostic report; returns (report, exit_code). Never raises for projection problems."""
    now = now if now is not None else time.time()
    validate_limits(time_limit, max_rows, busy_ms)
    ticket = validate_ticket(ticket)
    if not isinstance(config.get('cwd'), str) or not config['cwd'].strip() or not config.get('targets') or len(config['targets']) > 20:
        raise ValueError('invalid dispatcher scope configuration')
    names = [config['db_files'][key] for key in ('state', 'history', 'logs')]
    if len(set(names)) != 3 or any(not isinstance(n, str) or not n or any(x in n for x in ('/', '\\', ':')) or n in ('.', '..') for n in names):
        raise ValueError('database paths must be distinct local filenames')
    budget = Budget(time_limit, max_rows, clock)
    home = Path(config.get('codex_home') or Path.home() / '.codex')
    ref = parse_host_ref(host_ref)
    report = {'schema': SCHEMA, 'method': METHOD, 'diagnostic_only': True, 'absence_authorized': False,
              'job_digest': digest(ticket['job_id']), 'ticket_digest': digest(_canonical(ticket)),
              'dispatch_digest': digest(ticket['dispatch_token']), 'dispatch_action': ticket['dispatch_action'],
              'ticket_thread_digest': digest(ticket['thread_id']), 'host_ref_binding': 'unbound', 'host_ref_thread_digest': digest(ref.get('thread')),
              'units': UNITS, 'observations': [], 'screening': {}, 'coverage': {'truncated': False, 'cross_database_atomic': False, 'databases': {}},
              'ticket_host_binding': 'unverified',
              'gaps': ['host_operation_terminal_api_unavailable', 'marker_check_not_performed', 'dispatch_host_binding_unverified'], 'projection_files': [], 'schema_versions': {},
              'collected_from': iso(now), 'collected_until': None}
    issued = ticket['issued_at']
    if not in_range(now, 'seconds') or not in_range(issued, 'seconds') or issued > now + 300:
        report['gaps'].append('ticket_issued_at_out_of_range')
        return _finish(report, now, INCOMPLETE)
    if host_ref and not ref:
        report['gaps'].append('host_ref_invalid')
        return _finish(report, now, INCOMPLETE)
    thread = ref.get('thread')
    if thread and thread not in config['targets']:
        report['gaps'].append('thread_outside_dispatcher_scope')
        return _finish(report, now, INCOMPLETE)
    candidates = [thread] if thread else sorted(config['targets'])
    if not ref.get('turn'):
        report['gaps'].append('host_ref_unbound')
    report['host_ref_binding'] = 'unverified_locator' if ref.get('turn') else ('thread_only' if thread else 'unbound')
    cons = {}
    for key in ('state', 'history', 'logs'):
        if not budget.check():
            break
        path = home / config['db_files'][key]
        entry = {'opened': False, 'path_digest': digest(str(path)), 'started': iso(time.time()), 'rows_truncated': False}
        report['coverage']['databases'][key] = entry
        con = None
        try:
            if path.resolve().parent != home.resolve() or path.is_symlink():
                raise OSError('redirected database')
            stat = path.stat()
            report['projection_files'].append({'db': key, 'size': stat.st_size, 'mtime_ms': int(stat.st_mtime * 1000)})
            remaining_ms = max(0, int((budget.deadline-budget.clock())*1000))
            con = open_ro(path, min(busy_ms, remaining_ms, 200), budget)
            missing, versions = schema_check(con, REQUIRED[key], key)
            report['schema_versions'][key] = versions
            if missing:
                report['gaps'].append(f'unsupported_schema:{key}:' + ','.join(missing))
                continue
            cons[key] = con
            entry['opened'] = True
        except (OSError, sqlite3.Error) as e:
            report['gaps'].append(f'database_unavailable:{key}:{type(e).__name__}')
        finally:
            if key not in cons:
                entry['finished'] = iso(time.time())
                if con is not None:
                    con.close()
    try:
        scoped = []
        if 'state' in cons:
            for tid in candidates:
                if not budget.check():
                    break
                row = cons['state'].execute('SELECT cwd FROM threads WHERE id=? LIMIT 1', (tid,)).fetchone()
                if row and normalize_cwd(row['cwd']) == normalize_cwd(config['cwd']):
                    scoped.append(tid)
                else:
                    report['gaps'].append('dispatcher_cwd_unverified')
        else:
            report['gaps'].append('dispatcher_cwd_unverified')
        candidates = scoped
        if 'history' in cons and candidates:
            _history(report, cons['history'], ref, candidates, issued, now, budget)
        if 'state' in cons:
            _state(report, cons['state'], candidates, issued, budget)
        if 'logs' in cons and budget.check():
            for tid in candidates:
                if not budget.check():
                    break
                row = cons['logs'].execute('SELECT ts FROM logs WHERE thread_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
                report['observations'].append({'kind': 'last_log', 'thread_digest': digest(tid),
                                               'ts': row['ts'] if row and in_range(row['ts'], 'seconds') else None,
                                               'unit': 'seconds', 'note': 'silence proves nothing about side effects'})
                if row and not in_range(row['ts'], 'seconds'):
                    report['gaps'].append('unit_anomaly:logs.ts')
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as e:
        report['gaps'].append(f'query_failed:{type(e).__name__}')
    finally:
        for key, con in cons.items():
            report['coverage']['databases'][key]['finished'] = iso(time.time())
            try:
                con.set_progress_handler(None, 0)
                con.rollback()
            except sqlite3.Error:
                pass
            finally:
                con.close()
    budget.check()
    if budget.exhausted:
        report['gaps'].append('time_limit_exceeded')
    report['coverage']['truncated'] = budget.exhausted or any(d.get('rows_truncated') for d in report['coverage']['databases'].values())
    code = INCOMPLETE if (report['coverage']['truncated'] or any(g.startswith(('unsupported_schema', 'database_unavailable', 'query_failed', 'unit_anomaly', 'item_', 'turn_not_found', 'no_candidate_turn', 'dispatcher_cwd_unverified', 'unknown_', 'child_thread_missing')) for g in report['gaps'])) else 0
    report['coverage']['rows_examined'] = budget.rows
    return _finish(report, now, code)


def _history(report, con, ref, candidates, issued, now, budget):
    coverage = report['coverage']['databases']['history']
    turns = []
    for tid in candidates:
        if not budget.check():
            return
        if ref.get('turn') and tid == ref['thread']:
            rows = con.execute('SELECT thread_id,turn_id,status,started_at,completed_at,rollout_end_ordinal,error_json IS NOT NULL AS has_error FROM thread_turns WHERE thread_id=? AND turn_id=? LIMIT 2',
                               (tid, ref['turn'])).fetchall()
            if not rows:
                report['gaps'].append('turn_not_found')
        else:
            # Degraded: every turn overlapping [issued_at, now], unfinished ones included; a turn may have started before begin.
            rows = con.execute('''SELECT thread_id,turn_id,status,started_at,completed_at,rollout_end_ordinal,error_json IS NOT NULL AS has_error
                FROM thread_turns WHERE thread_id=? AND started_at<=? AND (completed_at IS NULL OR completed_at>=?) ORDER BY started_at LIMIT ?''',
                               (tid, now, issued, budget.max_rows + 1)).fetchall()
        turns += budget.take(rows, coverage)
    water = {}
    for tid in candidates:
        if not budget.check():
            return
        row = con.execute('SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?', (tid,)).fetchone()
        water[tid] = row['next_rollout_ordinal'] if row else None
    ended, consumed, no_thread_result = [], [], []
    for turn in turns:
        if not budget.check():
            return
        started_ok = in_range(turn['started_at'], 'seconds')
        if not started_ok:
            report['gaps'].append('unit_anomaly:thread_turns.started_at')
        completed = turn['completed_at']
        if completed is not None and (not in_range(completed, 'seconds') or (started_ok and completed < turn['started_at'])):
            report['gaps'].append('unit_anomaly:thread_turns.completed_at')
        status = turn['status'] if turn['status'] in ('completed', 'failed', 'inProgress', 'interrupted') else 'unknown'
        if status == 'unknown':
            report['gaps'].append('unknown_turn_status')
        obs = {'kind': 'turn', 'thread_digest': digest(turn['thread_id']), 'turn_digest': digest(turn['turn_id']), 'status': status,
               'has_error': bool(turn['has_error']), 'started_before_begin': (turn['started_at'] < issued) if started_ok else None,
               'rollout_end_ordinal': turn['rollout_end_ordinal'], 'projection_next_ordinal': water.get(turn['thread_id'])}
        ordinal_values = (water.get(turn['thread_id']), turn['rollout_end_ordinal'])
        ordinals_valid = all(isinstance(v, int) and v >= 0 for v in ordinal_values)
        obs['projection_next_ordinal'] = ordinal_values[0] if ordinals_valid else None
        obs['rollout_end_ordinal'] = ordinal_values[1] if ordinals_valid else None
        obs['watermark'] = ('consumed' if ordinals_valid and ordinal_values[0] > ordinal_values[1]
                            else 'lagging' if ordinals_valid else 'unknown')
        for value in (water.get(turn['thread_id']), turn['rollout_end_ordinal']):
            if value is not None and (not isinstance(value, int) or value < 0):
                report['gaps'].append('unknown_projection_watermark')
                obs['watermark'] = 'unknown'
        if obs['watermark'] == 'unknown':
            report['gaps'].append('unknown_projection_watermark')
        items = []
        items_valid = True
        try:
            cursor = con.execute('''SELECT item_id, started_at_ms, completed_at_ms,
                json_extract(item_json,'$.tool') AS tool,
                CASE WHEN json_extract(item_json,'$.status') IN ('completed','success','failed','inProgress','interrupted','pending','running')
                     THEN json_extract(item_json,'$.status') ELSE 'unknown' END AS status,
                json_extract(item_json,'$.error') IS NOT NULL AS has_error,
                json_type(item_json,'$.result.structuredContent') AS structured_type,
                (json_type(item_json,'$.result.structuredContent.threadId') IS NOT NULL OR
                 json_type(item_json,'$.result.structuredContent.thread_id') IS NOT NULL OR
                 json_type(item_json,'$.result.structuredContent.id') IS NOT NULL) AS thread_field_present,
                CASE WHEN json_type(item_json,'$.result.structuredContent.threadId')='text'
                          AND length(json_extract(item_json,'$.result.structuredContent.threadId')) BETWEEN 1 AND 256
                     THEN json_extract(item_json,'$.result.structuredContent.threadId')
                     WHEN json_type(item_json,'$.result.structuredContent.thread_id')='text'
                          AND length(json_extract(item_json,'$.result.structuredContent.thread_id')) BETWEEN 1 AND 256
                     THEN json_extract(item_json,'$.result.structuredContent.thread_id')
                     WHEN json_type(item_json,'$.result.structuredContent.id')='text'
                          AND length(json_extract(item_json,'$.result.structuredContent.id')) BETWEEN 1 AND 256
                     THEN json_extract(item_json,'$.result.structuredContent.id') END AS thread_value
                FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type='mcpToolCall'
                AND json_extract(item_json,'$.tool') IN (?,?) ORDER BY rowid LIMIT ?''',
                                 (turn['thread_id'], turn['turn_id'], *CREATE_TOOLS, budget.max_rows + 1))
            rows = cursor.fetchall()
        except sqlite3.Error as error:
            report['gaps'].append('item_json_malformed' if str(error) == 'malformed JSON' else 'query_failed:items:' + type(error).__name__)
            items_valid = False
            rows = []
        rows = budget.take(rows, coverage)
        if ref.get('item') and not any(i['item_id'] == ref['item'] for i in rows):
            report['gaps'].append('item_not_found_or_wrong_tool')
        elif ref.get('turn'):
            report['host_ref_binding'] = 'exact_locator_not_dispatch_verified'
        for item in rows:
            if not budget.check():
                return
            if item['status'] == 'unknown':
                report['gaps'].append('unknown_item_status')
            if item['thread_field_present'] and item['thread_value'] is None:
                report['gaps'].append('item_thread_result_invalid')
            for col in ('started_at_ms', 'completed_at_ms'):
                if (item[col] is None and (col == 'started_at_ms' or item['status'] in ('completed','success','failed','interrupted'))) or (item[col] is not None and not in_range(item[col], 'milliseconds')):
                    report['gaps'].append('unit_anomaly:thread_items.' + col)
            items.append({'item_digest': digest(item['item_id']), 'tool': item['tool'], 'status': item['status'], 'has_error': bool(item['has_error']),
                          'structured_type': item['structured_type'], 'thread_result': item['thread_value'] is not None,
                          'thread_result_digest': digest(item['thread_value']), 'matches_ticket_thread': bool(item['thread_value']) and digest(item['thread_value']) == report['ticket_thread_digest'],
                          'started_at_ms_in_range': in_range(item['started_at_ms'], 'milliseconds') if item['started_at_ms'] is not None else None})
        obs['create_or_reuse_calls'] = items
        obs['call_summary'] = ('unavailable' if not items_valid else 'none' if not items else 'thread_result' if any(i['thread_result'] for i in items)
                               else 'failed_only' if all(i['has_error'] or i['status'] not in ('completed', 'success') for i in items) else 'no_thread_result')
        report['observations'].append(obs)
        ended.append(status in ('completed', 'failed', 'interrupted'))
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
        rows = budget.take(rows, coverage)
        for edge in rows:
            if not budget.check():
                return
            examined += 1
            child = con.execute('SELECT created_at_ms FROM threads WHERE id=?', (edge['child_thread_id'],)).fetchone()
            edge_status = edge['status'] if edge['status'] in ('open','closed') else 'unknown'
            if edge_status == 'unknown':
                report['gaps'].append('unknown_edge_status')
            obs = {'kind': 'spawn_edge', 'parent_digest': digest(tid), 'child_digest': digest(edge['child_thread_id']), 'edge_status': edge_status,
                   'child_created_at_ms': child['created_at_ms'] if child and in_range(child['created_at_ms'], 'milliseconds') else None}
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


def validate_output_directory(path):
    """Require an already-private, unredirected parent; never repair permissions."""
    parent = Path(path).absolute().parent
    if parent.resolve() != parent or not parent.is_dir():
        raise ValueError('output parent must be an existing unredirected directory')
    if os.name != 'nt':
        if parent.stat().st_mode & 0o077:
            raise ValueError('output parent must be private')
        return
    encoded = base64.b64encode(str(parent).encode('utf-8')).decode('ascii')
    script = r"""$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PATH_BASE64'))
$user=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl=Get-Acl -LiteralPath $p
foreach($r in $acl.Access){
  if($r.AccessControlType -eq 'Allow'){
    $sid=$r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if($sid -ne $user -and $sid -ne 'S-1-5-18'){Write-Output "unexpected principal $($r.IdentityReference) ($sid) for user $user"; exit 3}
  }
}
""".replace('PATH_BASE64', encoded)
    argument = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    # The 64-bit Windows PowerShell by its full path, like the ticket-directory protection: the PATH-resolved
    # powershell.exe on some hosts (GitHub's runner) cannot load Microsoft.PowerShell.Security for Get-Acl.
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-EncodedCommand',argument],
                            capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        detail = ' '.join((result.stdout + result.stderr).decode('utf-8', 'replace').split())[:300]
        raise ValueError(f'output parent must permit only current user and SYSTEM (check exit {result.returncode}: {detail or "no output"})')


def write_report(report, out):
    """Exclusive create, canonical JSON, sidecar sha256; the hash binds the file, not the live databases."""
    data = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError('diagnostic report exceeds size limit')
    path = Path(out)
    validate_output_directory(path)
    sha = hashlib.sha256(data).hexdigest()
    sidecar = path.with_name(path.name + '.sha256')
    created = []
    try:
        # Reserve BOTH names before writing; never replace a pre-existing sidecar.
        for target in (sidecar, path):
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append((target, fd))
        os.write(created[0][1], (sha+'\n').encode('ascii'))
        with os.fdopen(created[1][1], 'wb', closefd=False) as stream:
            stream.write(data)
    except Exception:
        for target, fd in created:
            os.close(fd)
            target.unlink()
        raise
    else:
        for _, fd in created:
            os.close(fd)
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
    try:
        validate_limits(args.time_limit, args.max_rows, 2000)
        guard = _load_guard_config()
        config = guard.load_config(args.config or guard.CONFIG_PATH)
        if Path(args.ticket).stat().st_size > 16384:
            raise ValueError('ticket too large')
        ticket = validate_ticket(json.loads(Path(args.ticket).read_text(encoding='utf-8')))
        report, code = collect(config, ticket, args.host_ref, time_limit=args.time_limit, max_rows=args.max_rows)
        sha = write_report(report, args.out)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.TimeoutExpired):
        # Do not echo malformed ticket/config content or credential-bearing paths.
        parser.error('unusable diagnostic input or output; no authorization granted')
    print(json.dumps({'report': str(args.out), 'sha256': sha, 'exit_code': code, 'absence_authorized': False, 'gaps': report['gaps']}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
