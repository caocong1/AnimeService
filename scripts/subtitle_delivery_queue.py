"""Local delivery control plane; never instantiate Engine or call a downloader.

Use status for read-only snapshots. Mutations are explicit subcommands. Lease and
dispatch tokens stay in the local DB/process or private begin-ticket archive;
they are never printed or included in public receipt exports.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.db import Store
from anime.subtitle_delivery import SubtitleDelivery, validate_ticket


class ControlStore(Store):
    def __init__(self, path):
        self.path = Path(path)
        self.gate = threading.RLock()
        if not self.path.is_file():
            raise ValueError('Existing AnimeService database required')


class ReadStore(ControlStore):
    def __init__(self, path):
        super().__init__(path)
        self.con = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA query_only=ON')
        self.con.execute('BEGIN')

    def rows(self, sql, args=()):
        return [dict(row) for row in self.con.execute(sql, args)]

    def execute(self, *args, **kwargs):
        raise ValueError('Status is read-only')


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k not in ('lease_token', 'dispatch_token')}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def load_ticket(path):
    """The begin ticket archived by `begin`; a v1 receipt is bound to it, never to the live job's current tokens."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError('dispatch ticket must be a readable UTF-8 JSON file') from None
    return validate_ticket(data)


ACL_TIMEOUT_SECONDS = 45


class TicketArchiveError(ValueError):
    """Credential-free diagnostics; never include subprocess argv or ticket bytes."""
    def __init__(self, message, diagnostics):
        self.diagnostics = diagnostics
        super().__init__(message + ': ' + json.dumps(diagnostics, ensure_ascii=False))


def ticket_directory(db_path):
    tickets = Path(db_path).resolve().parent / 'subtitle-delivery-tickets'
    tickets.mkdir(mode=0o700, parents=True, exist_ok=True)
    if tickets.resolve() != tickets or tickets.is_symlink():
        raise ValueError('ticket directory must not be a redirected path')
    return tickets


def _write_verified_archive(path, raw):
    """One exclusive write, flush/fsync and closed-file readback. Never remove failures."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        written = os.fstat(stream.fileno())
    with path.open('rb') as stream:
        reopened = os.fstat(stream.fileno())
        actual = stream.read()
    if (path.is_symlink() or path.resolve() != path or
            (written.st_dev, written.st_ino) != (reopened.st_dev, reopened.st_ino) or actual != raw):
        raise TicketArchiveError('ticket archive readback failed', {'phase': 'archive_readback'})


def preflight_ticket_archive(db_path):
    """Exercise the real archive IO before any lease, using only a unique nonsensitive probe."""
    tickets = ticket_directory(db_path)
    protect_ticket_directory(tickets)
    probe = tickets / ('.archive-probe-' + uuid.uuid4().hex + '.tmp')
    raw = b'AnimeService archive IO preflight; no dispatch authority\n'
    _write_verified_archive(probe, raw)
    # Only a successfully created and verified probe can reach cleanup. A failed
    # write stays as evidence; an exclusive-open collision never deletes a file.
    probe.unlink()
    return tickets


def save_ticket(db_path, result):
    """Archive the original ticket privately; return a path only after durable readback."""
    tickets = ticket_directory(db_path)
    protect_ticket_directory(tickets)
    # Hash the job identifier: even a malformed identifier cannot escape this directory.
    job = hashlib.sha256(result['id'].encode()).hexdigest()[:16]
    path = tickets / f"{job}-r{result['revision']}-{hashlib.sha256(result['ticket']['dispatch_token'].encode()).hexdigest()[:8]}.json"
    # Exclusive creation prevents overwriting another archived ticket. On Windows
    # the file inherits only the newly protected ticket directory's two ACL rules.
    validate_ticket(result['ticket'])
    raw = (json.dumps(result['ticket'], ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    _write_verified_archive(path, raw)
    if load_ticket(path) != result['ticket']:
        raise TicketArchiveError('ticket archive identity mismatch', {'phase': 'archive_validation'})
    return str(path)


def protect_ticket_directory(path):
    """Restrict only the ticket directory, never the DB/parent ACL. Fail before writing secrets.

    https://learn.microsoft.com/dotnet/api/system.io.directory.setaccesscontrol
    https://learn.microsoft.com/dotnet/api/system.security.accesscontrol.objectsecurity.setaccessruleprotection
    Paths are encoded as data; no shell interpolation of paths or credentials.
    """
    if os.name != 'nt':
        os.chmod(path, 0o700)
        return
    encoded_path = base64.b64encode(str(path).encode('utf-8')).decode('ascii')
    script = r"""$ErrorActionPreference='Stop'
$target=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PATH_BASE64'))
$current=[Security.Principal.WindowsIdentity]::GetCurrent().User
$system=[Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$acl=[Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true,$false)
foreach($sid in @($current,$system)) {
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
[IO.Directory]::SetAccessControl($target,$acl)
$actual=[IO.Directory]::GetAccessControl($target)
$rules=@($actual.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
$expected=@($current.Value)
if($system.Value -notin $expected) {$expected+=$system.Value}
if(-not $actual.AreAccessRulesProtected -or $rules.Count -ne $expected.Count) {throw 'ticket ACL protection failed'}
foreach($rule in $rules) {
  if($rule.IdentityReference.Value -notin $expected -or $rule.AccessControlType -ne 'Allow' -or $rule.FileSystemRights -ne 'FullControl') {throw 'unexpected ticket ACL'}
}
""".replace('PATH_BASE64', encoded_path)
    command = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    started = time.monotonic()
    try:
        subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
                       check=True, capture_output=True, timeout=ACL_TIMEOUT_SECONDS,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as error:
        stderr = getattr(error, 'stderr', None) or b''
        if isinstance(stderr, str):
            stderr = stderr.encode('utf-8', errors='replace')
        diagnostics = {
            'phase': 'ticket_acl', 'exception_type': type(error).__name__,
            'kind': ('timeout' if isinstance(error, subprocess.TimeoutExpired) else
                     'process_exit' if isinstance(error, subprocess.CalledProcessError) else 'launch'),
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'timeout_seconds': ACL_TIMEOUT_SECONDS,
            'returncode': getattr(error, 'returncode', None), 'errno': getattr(error, 'errno', None),
            'stderr': stderr[:4096].decode('utf-8', errors='replace'),
            'stderr_bytes': len(stderr), 'stderr_truncated': len(stderr) > 4096,
        }
        raise TicketArchiveError('cannot secure the ticket directory', diagnostics) from None


def evidence_file(path):
    """v1: the file's content plus the tool-computed name and sha256; the service validates and binds it."""
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('absence evidence must be a UTF-8 JSON object') from None
    if not isinstance(data, dict):
        raise ValueError('absence evidence must be a JSON object')
    return {**data, 'file': Path(path).name, 'sha256': hashlib.sha256(raw).hexdigest()}


def absence_evidence(path,record=None):
    """Legacy path: the verification record itself, bound by hash: who checked which chat list when, and how."""
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('absence evidence must be a UTF-8 JSON object') from None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip() for k in ('checked_at', 'method', 'checked_by')):
        raise ValueError('absence evidence needs checked_at, method and checked_by')
    if record is not None:
        import datetime
        if data.get('job_id')!=record['id'] or data.get('dispatch_token')!=record['dispatch_token']:
            raise ValueError('absence evidence must identify this job_id and dispatch_token')
        try:
            checked=datetime.datetime.fromisoformat(data['checked_at'])
            if checked.tzinfo is None or checked.timestamp()<record['updated']:raise ValueError()
        except (TypeError,ValueError):raise ValueError('absence evidence checked_at must be timezone-aware and after the current dispatch state') from None
    # Evidence must not supply its own trusted file/hash fields.
    return {**data,'file': Path(path).name, 'sha256': hashlib.sha256(raw).hexdigest()}


def begin_phase(phase, job_id, revision):
    """Best-effort, credential-free markers for the caller's preopened stderr."""
    try:
        print(json.dumps({'schema': 'dispatch-begin-phase/v1', 'phase': phase,
                          'job_id': job_id, 'revision': revision, 'pid': os.getpid(), 'ppid': os.getppid(),
                          'at': time.time(), 'monotonic': time.monotonic()}, ensure_ascii=False),
              file=sys.stderr, flush=True)
    except OSError:
        # Diagnostics cannot replace or undo the real ticket/state operation.
        pass


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'data/anime.db'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('refresh')
    capacity = sub.add_parser('capacity', help='set bounded concurrent episode production without releasing existing owners')
    capacity.add_argument('--limit', type=int, choices=range(1, 4), required=True)
    progress = sub.add_parser('progress')
    progress.add_argument('job_id')
    progress.add_argument('--thread-id', required=True)
    from anime.subtitle_delivery_view import STAGES
    progress.add_argument('--stage', required=True, choices=STAGES)
    refine = sub.add_parser('refine')
    refine.add_argument('job_id')
    progress.add_argument('--done', type=int)
    progress.add_argument('--total', type=int)
    begin = sub.add_parser('begin')
    begin.add_argument('job_id')
    begin.add_argument('--owner', required=True)
    begin.add_argument('--revision', required=True, type=int)
    begin.add_argument('--expected-input', help='require this exact observed input before claiming a lease')
    begin.add_argument('--experience', action='store_true')
    begin.add_argument('--auto-recovery-turn', help='exact stopped origin turn for standing automatic same-owner recovery')
    begin.add_argument('--queue-host', action='store_true', help='Persist a recoverable, unsent handoff before returning')
    begin.add_argument('--manual-recovery', action='store_true', help='new explicit user request to resume a previously user-stopped owner')
    release = sub.add_parser('release-lease', help='give back an undispatched lease (never one whose dispatch started)')
    release.add_argument('job_id')
    release.add_argument('--owner', required=True)
    release.add_argument('--revision', required=True, type=int)
    absent = sub.add_parser('not-created', help='record independently verified absence of a chat for an unknown dispatch')
    absent.add_argument('job_id')
    absent.add_argument('--evidence', required=True, help='JSON file describing how the absence was verified')
    absent.add_argument('--ticket', help='begin ticket file: records a dispatch-receipt/v1 instead of the legacy path')
    absent.add_argument('--host-ref', help='host operation/request reference bound into a v1 receipt')
    for name in ('failed', 'interrupted'):
        p = sub.add_parser(name, help=f'diagnostic dispatch-receipt/v1 ({name}); changes no job state')
        p.add_argument('job_id')
        p.add_argument('--ticket', required=True, help='begin ticket file')
        p.add_argument('--host-ref', help='host operation/request reference bound into the receipt')
        p.add_argument('--note', default='', help='short bounded note kept as evidence')
    receipts = sub.add_parser('receipts', help='read-only receipt ledger of one job')
    receipts.add_argument('job_id')
    from anime.subtitle_evidence import KINDS
    evidence = sub.add_parser('register-evidence', help='bind one validated preparation report to an episode\'s readiness record and re-assess')
    evidence.add_argument('show_id', type=int)
    evidence.add_argument('episode', type=int)
    evidence.add_argument('--kind', required=True, choices=KINDS)
    evidence.add_argument('--report', required=True, help='JSON report inside the private subtitle archive')
    evidence.add_argument('--dry-run', action='store_true', help='validate the binding without recording it')
    evidence.add_argument('--thread-id', help='authorized stopped original owner continuing v1')
    evidence.add_argument('--expected-input', help='exact fixed input for stopped-owner recovery')
    target = sub.add_parser('bind-target', help='record a reconciled completed file as an unfrozen episode\'s provisional production target')
    target.add_argument('show_id', type=int)
    target.add_argument('episode', type=int)
    target.add_argument('--path', required=True, help='the selected completed media file of a reconciled task for this episode')
    target.add_argument('--dry-run', action='store_true', help='validate the binding without recording it')
    target.add_argument('--thread-id', help='explicitly authorized correction by the current acknowledged production owner')
    target.add_argument('--expected-input', help='exact current input key; required together with --thread-id')
    plan = sub.add_parser('reconcile-plan', help='read-only owner review; does not reconcile or issue a new ticket')
    plan.add_argument('job_id')
    plan.add_argument('--ticket', required=True)
    plan.add_argument('--receipt-id', required=True)
    for name in ('confirm', 'unknown', 'blocked', 'accepted', 'finished'):
        p = sub.add_parser(name)
        p.add_argument('job_id')
        if name != 'unknown':
            p.add_argument('--thread-id', required=True)
        if name in ('confirm', 'unknown'):
            p.add_argument('--ticket', help='begin ticket file: records a dispatch-receipt/v1 instead of the legacy path')
            p.add_argument('--host-ref', help='host operation/request reference; requires --ticket')
        if name == 'confirm':
            p.add_argument('--reconcile-receipt', help='explicit local recovery of this original rejected created receipt')
            p.add_argument('--expected-revision', type=int, help='explicit CURRENT revision for created-receipt reconciliation')
            p.add_argument('--host-evidence', help='fresh raw native tool observation bundle; requires the two reconciliation flags')
        if name == 'blocked':
            p.add_argument('--reason', action='append', required=True)
            p.add_argument('--stopped', action='store_true')
            p.add_argument('--recovery-class', choices=('system', 'source_wait', 'repair_blocked'))
            p.add_argument('--recovery-turn', help='exact stopped origin turn; required for system recovery')
            p.add_argument('--user-stopped', action='store_true', help='explicit user stop; never automatic recovery')
        if name == 'accepted':
            p.add_argument('--build-id', required=True)
        if name == 'finished':
            p.add_argument('--release', required=True)
            p.add_argument('--player-report', required=True)
    args = parser.parse_args()
    if getattr(args, 'host_ref', None) and not getattr(args, 'ticket', None):
        raise ValueError('--host-ref requires the original --ticket')
    reconciliation = [getattr(args, k, None) for k in ('reconcile_receipt', 'expected_revision', 'host_evidence')]
    if any(v is not None for v in reconciliation):
        if not all(v is not None for v in reconciliation) or not args.ticket or args.host_ref:
            raise ValueError('Reconciliation requires original --ticket, --reconcile-receipt, --expected-revision and --host-evidence; no replacement host-ref')
    reading = args.command in ('status', 'receipts', 'reconcile-plan') or (args.command in ('register-evidence', 'bind-target') and args.dry_run)
    if args.command == 'begin':
        begin_phase('cli_started', args.job_id, args.revision)
    db = ReadStore(args.db) if reading else ControlStore(args.db)
    delivery = SubtitleDelivery(db, initialize=not reading)
    try:
        if args.command == 'status':
            result = delivery.snapshot()
        elif args.command == 'capacity':
            result = delivery.set_production_limit(args.limit)
        elif args.command == 'receipts':
            result = delivery.receipts(args.job_id)
        elif args.command == 'reconcile-plan':
            ticket = load_ticket(args.ticket)
            if ticket['job_id'] != args.job_id:
                raise ValueError('ticket belongs to another job')
            result = delivery.reconciliation_plan(ticket, args.receipt_id)
        elif args.command == 'refresh':
            result = delivery.refresh()
        elif args.command == 'register-evidence':
            from anime.subtitle_evidence import register_evidence
            try:
                result = register_evidence(delivery, args.show_id, args.episode, args.kind, args.report, dry_run=args.dry_run,
                                           thread_id=args.thread_id, expected_input=args.expected_input)
            except ValueError as error:
                print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
                return 2
            except (OSError, sqlite3.Error, KeyError, TypeError):
                print(json.dumps({'ok': False, 'reason': 'Evidence validation unavailable'}, ensure_ascii=False))
                return 2
        elif args.command == 'bind-target':
            from anime.subtitle_evidence import bind_target
            try:
                result = bind_target(delivery, args.show_id, args.episode, args.path, dry_run=args.dry_run,
                                     thread_id=args.thread_id, expected_input=args.expected_input)
            except ValueError as error:
                print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
                return 2
            except (OSError, sqlite3.Error, KeyError, TypeError):
                print(json.dumps({'ok': False, 'reason': 'Target validation unavailable'}, ensure_ascii=False))
                return 2
        elif args.command == 'progress':
            from anime.subtitle_delivery_view import report_progress
            result = report_progress(db, args.job_id, args.thread_id, args.stage, args.done, args.total)
        elif args.command == 'refine':
            from anime.subtitle_refinement import enqueue_refinement
            result = enqueue_refinement(delivery, args.job_id) or {'status': 'preserved_or_not_eligible'}
        elif args.command == 'begin':
            # Exercise the same filesystem before claiming a lease. This cannot
            # guarantee later IO, so every post-intent failure stays protected.
            begin_phase('archive_preflight_started', args.job_id, args.revision)
            preflight_ticket_archive(args.db)
            begin_phase('archive_preflight_passed', args.job_id, args.revision)
            current=delivery.get(args.job_id)
            if args.expected_input is not None and current['input_key'] != args.expected_input:
                raise ValueError('Expected delivery input changed; no lease claimed')
            template = (ROOT / 'docs/SUBTITLE-SESSION-PROMPT.txt').read_text(encoding='utf-8')
            title = db.show(current['show_id'])['title']
            begin_phase('reserve_started', args.job_id, args.revision)
            if current['dispatch_state']=='blocked-stopped':
                lease=delivery.resume(args.job_id,current['thread_id'],args.owner,args.revision,
                                      allow_experience=args.experience,auto_recovery_turn=args.auto_recovery_turn,
                                      manual_recovery=args.manual_recovery)
            else:
                if args.auto_recovery_turn or args.manual_recovery:
                    raise ValueError('Recovery options require a blocked-stopped original owner')
                lease = delivery.reserve(args.job_id, args.owner, args.revision, allow_experience=args.experience)
            begin_phase('lease_reserved', args.job_id, lease['revision'])
            try:
                begin_phase('intent_started', args.job_id, lease['revision'])
                result = delivery.begin_dispatch(args.job_id, args.owner, lease['lease_token'], lease['revision'])
            except Exception as error:
                # The lease was ours and nothing was dispatched: hand it back so the single slot is not held hostage.
                try:
                    released = delivery.release(args.job_id, args.owner, lease['revision'], lease['lease_token'], reason='begin failed: ' + str(error)[:200])
                except Exception:
                    raise SystemExit(json.dumps({'error': str(error), 'lease_released': False,
                        'reconcile_required': True}, ensure_ascii=False)) from None
                raise SystemExit(json.dumps({'error': str(error), 'lease_released': True, 'state': released['state'],
                                             'revision': released['revision']}, ensure_ascii=False))
            begin_phase('intent_committed', args.job_id, result['revision'])
            sid, ep = result['show_id'], result['episode']
            initial = delivery.get(delivery.episode_id(sid, ep))
            responsible = result.get('thread_id') or (initial.get('thread_id') if result['kind'] in ('late','refinement') else '')
            if result['payload'].get('isolated_owner_required') and not result.get('thread_id'):
                responsible = ''
            result['dispatch_action'] = 'resume' if responsible else 'create'
            result['responsible_thread_id'] = responsible or None
            result['suggested_title'] = f"{title} 第{ep}集字幕" + (' v1精校细调' if result['kind']=='refinement' else ' V2审核' if result['kind']=='late' else ' v0→v1整集制作')
            result['session_prompt'] = template.replace('{{JOB_ID}}', result['id']).replace('{{SHOW_ID}}', str(sid)).replace('{{EPISODE}}', str(ep)).replace('{{INPUT_KEY}}', result['input_key'])
            if args.auto_recovery_turn and responsible:
                result['session_prompt'] = (
                    f"继续原任务 {result['id']}，固定输入 {result['input_key']}，恢复票据 r{result['revision']}。"
                    "读本集最新 RESULT／交接和 docs/SUBTITLE-SESSION-PROMPT.txt，按其中完整制作与交付规则续作。"
                    "开工前核实原负责人、派发确认、未看和当前输入，复用已有稿件与编码。"
                )
            try:
                begin_phase('ticket_archive_started', args.job_id, result['revision'])
                result['ticket_path'] = save_ticket(args.db, result)
                from anime.dispatch_caller import verified_ticket
                verified_ticket(result, result['ticket_path'])
                begin_phase('ticket_verified', args.job_id, result['revision'])
            except Exception as error:
                # No retry, release or fabricated replacement ticket. The
                # caller needs a phase-specific diagnostic even when stdout is
                # empty. Only fixed identity and credential-free error fields.
                print(json.dumps({
                    'phase': 'after_intent_archive', 'job_id': result['id'],
                    'input_key': result['input_key'], 'revision': result['revision'],
                    'external_host_call_started': False, 'reconcile_required': True,
                    'error_type': type(error).__name__,
                    'diagnostics': getattr(error, 'diagnostics', {}),
                }, ensure_ascii=False), file=sys.stderr, flush=True)
                raise
            if args.auto_recovery_turn:
                # Stop before returning control to any host caller if this cannot
                # be persisted. Verify the original archive before consuming the
                # attempt; unknown host results still consume it afterwards.
                result['auto_recovery'] = delivery.mark_auto_recovery(
                    args.job_id, args.owner, result['revision'], args.auto_recovery_turn, result['ticket'])
            if args.queue_host:
                from anime.subtitle_host_handoff import prepare
                result['host_handoff'] = prepare(db,result)
        elif args.command == 'release-lease':
            result = delivery.release(args.job_id, args.owner, args.revision, reason='operator release')
        elif args.command == 'not-created' and args.ticket:
            ticket = load_ticket(args.ticket)
            if ticket['job_id'] != args.job_id:
                raise ValueError('ticket belongs to another job')
            result = delivery.record_dispatch_receipt(ticket, 'not_created', evidence=evidence_file(args.evidence),
                                                      host_ref=args.host_ref,
                                                      recorded_by='subtitle_delivery_queue not-created')
        elif args.command == 'not-created':
            record=delivery.get(args.job_id)
            result = delivery.reconcile_dispatch(args.job_id, record['dispatch_token'],
                                                 definitely_not_created=True, absence_evidence=absence_evidence(args.evidence,record))
            result['receipt'] = 'legacy'
        elif args.command == 'unknown' and args.ticket:
            ticket = load_ticket(args.ticket)
            if ticket['job_id'] != args.job_id:
                raise ValueError('ticket belongs to another job')
            result = delivery.record_dispatch_receipt(ticket, 'interrupted', evidence={'note': 'dispatch response unknown'},
                                                      host_ref=args.host_ref,
                                                      recorded_by='subtitle_delivery_queue unknown --ticket')
        elif args.command == 'confirm' and args.reconcile_receipt:
            from anime.dispatch_reconciliation import reconcile_created
            ticket = load_ticket(args.ticket)
            if ticket['job_id'] != args.job_id:
                raise ValueError('ticket belongs to another job')
            result = reconcile_created(delivery, ticket, args.reconcile_receipt, args.expected_revision,
                                       args.thread_id, args.host_evidence,
                                       recorded_by='subtitle_delivery_queue confirm --reconcile-receipt')
        elif args.command == 'confirm' and args.ticket:
            ticket = load_ticket(args.ticket)
            if ticket['job_id'] != args.job_id:
                raise ValueError('ticket belongs to another job')
            result = delivery.record_dispatch_receipt(ticket, 'created', thread_id=args.thread_id,
                                                      host_ref=args.host_ref,
                                                      recorded_by='subtitle_delivery_queue confirm')
        elif args.command in ('failed', 'interrupted'):
            ticket = load_ticket(args.ticket)
            if ticket['job_id'] != args.job_id:
                raise ValueError('ticket belongs to another job')
            result = delivery.record_dispatch_receipt(ticket, args.command, evidence={'note': args.note[:500]} if args.note else None,
                                                      host_ref=args.host_ref,
                                                      recorded_by=f'subtitle_delivery_queue {args.command}')
        elif args.command in ('confirm', 'unknown'):
            record = delivery.get(args.job_id)
            result = delivery.reconcile_dispatch(args.job_id, record['dispatch_token'],
                                                 thread_id=args.thread_id if args.command == 'confirm' else None)
            result['receipt'] = 'legacy'
        elif args.command == 'blocked':
            result = delivery.record_blocked(args.job_id, args.thread_id, args.reason, owner_stopped=args.stopped,
                                             recovery_class=args.recovery_class, recovery_turn=args.recovery_turn,
                                             user_stopped=args.user_stopped)
        elif args.command == 'finished':
            result = delivery.record_finished(args.job_id, args.thread_id, args.release, args.player_report)
        else:
            result = delivery.record_accepted(args.job_id, args.thread_id, args.build_id)
        # Begin's critical result is the verified original ticket. A whole-library
        # legacy export must not delay returning it; live queue/progress APIs read
        # current facts independently. Other mutation exports remain unchanged.
        if not reading and args.command != 'begin':
            from anime.subtitle_delivery_view import export_view
            try:
                export_view(db, ROOT if Path(args.db).resolve() == (ROOT/'data/anime.db').resolve() else None)
            except Exception:  # the state change is already saved; a view export problem must not look like a failed command
                result['view_refresh_warning'] = 'Delivery operation saved; public progress view needs refresh'
        if args.command == 'begin':
            begin_phase('result_ready', args.job_id, result['revision'])
        print(json.dumps(public(result), ensure_ascii=False, indent=2), flush=True)
        if args.command == 'begin':
            begin_phase('output_flushed', args.job_id, result['revision'])
    finally:
        if isinstance(db, ReadStore):
            db.con.rollback()
            db.con.close()


if __name__ == '__main__':
    sys.exit(main())
