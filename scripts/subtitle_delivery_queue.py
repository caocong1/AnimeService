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


def save_ticket(db_path, result):
    """Archive the ticket with its tokens next to the database (private); only the path is printed."""
    tickets = Path(db_path).resolve().parent / 'subtitle-delivery-tickets'
    tickets.mkdir(mode=0o700, parents=True, exist_ok=True)
    if tickets.resolve() != tickets or tickets.is_symlink():
        raise ValueError('ticket directory must not be a redirected path')
    protect_ticket_directory(tickets)
    # Hash the job identifier: even a malformed identifier cannot escape this directory.
    job = hashlib.sha256(result['id'].encode()).hexdigest()[:16]
    path = tickets / f"{job}-r{result['revision']}-{hashlib.sha256(result['ticket']['dispatch_token'].encode()).hexdigest()[:8]}.json"
    # Exclusive creation prevents overwriting another archived ticket. On Windows
    # the file inherits only the newly protected ticket directory's two ACL rules.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(result['ticket'], ensure_ascii=False, indent=2) + '\n')
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
    try:
        subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
                       check=True, capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        raise ValueError('cannot secure the ticket directory; dispatch remains protected, reconcile required') from None


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


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'data/anime.db'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('refresh')
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
    begin.add_argument('--experience', action='store_true')
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
    reading = args.command in ('status', 'receipts', 'reconcile-plan') or (args.command == 'register-evidence' and args.dry_run)
    db = ReadStore(args.db) if reading else ControlStore(args.db)
    delivery = SubtitleDelivery(db, initialize=not reading)
    try:
        if args.command == 'status':
            result = delivery.snapshot()
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
                result = register_evidence(delivery, args.show_id, args.episode, args.kind, args.report, dry_run=args.dry_run)
            except ValueError as error:
                print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
                return 2
            except (OSError, sqlite3.Error, KeyError, TypeError):
                print(json.dumps({'ok': False, 'reason': 'Evidence validation unavailable'}, ensure_ascii=False))
                return 2
        elif args.command == 'progress':
            from anime.subtitle_delivery_view import report_progress
            result = report_progress(db, args.job_id, args.thread_id, args.stage, args.done, args.total)
        elif args.command == 'refine':
            from anime.subtitle_refinement import enqueue_refinement
            result = enqueue_refinement(delivery, args.job_id) or {'status': 'preserved_or_not_eligible'}
        elif args.command == 'begin':
            # Fail before claiming a lease or committing intent when ticket ACL
            # preparation is unavailable. save_ticket verifies it again at write.
            ticket_dir = Path(args.db).resolve().parent / 'subtitle-delivery-tickets'
            ticket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if ticket_dir.resolve() != ticket_dir or ticket_dir.is_symlink():
                raise ValueError('ticket directory must not be a redirected path')
            protect_ticket_directory(ticket_dir)
            current=delivery.get(args.job_id)
            if current['dispatch_state']=='blocked-stopped':
                lease=delivery.resume(args.job_id,current['thread_id'],args.owner,args.revision,
                                      allow_experience=args.experience)
            else:
                lease = delivery.reserve(args.job_id, args.owner, args.revision, allow_experience=args.experience)
            try:
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
            sid, ep = result['show_id'], result['episode']
            initial = delivery.get(delivery.episode_id(sid, ep))
            responsible = result.get('thread_id') or (initial.get('thread_id') if result['kind'] in ('late','refinement') else '')
            if result['payload'].get('isolated_owner_required') and not result.get('thread_id'):
                responsible = ''
            result['dispatch_action'] = 'resume' if responsible else 'create'
            result['responsible_thread_id'] = responsible or None
            result['suggested_title'] = f"{db.show(sid)['title']} 第{ep}集字幕" + (' v1精校细调' if result['kind']=='refinement' else ' V2审核' if result['kind']=='late' else ' v0→v1整集制作')
            template = (ROOT / 'docs/SUBTITLE-SESSION-PROMPT.txt').read_text(encoding='utf-8')
            result['session_prompt'] = template.replace('{{JOB_ID}}', result['id']).replace('{{SHOW_ID}}', str(sid)).replace('{{EPISODE}}', str(ep)).replace('{{INPUT_KEY}}', result['input_key'])
            result['ticket_path'] = save_ticket(args.db, result)
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
            result = delivery.record_blocked(args.job_id, args.thread_id, args.reason, owner_stopped=args.stopped)
        elif args.command == 'finished':
            result = delivery.record_finished(args.job_id, args.thread_id, args.release, args.player_report)
        else:
            result = delivery.record_accepted(args.job_id, args.thread_id, args.build_id)
        if not reading:
            from anime.subtitle_delivery_view import export_view
            try:
                export_view(db, ROOT if Path(args.db).resolve() == (ROOT/'data/anime.db').resolve() else None)
            except Exception:  # the state change is already saved; a view export problem must not look like a failed command
                result['view_refresh_warning'] = 'Delivery operation saved; public progress view needs refresh'
        print(json.dumps(public(result), ensure_ascii=False, indent=2))
    finally:
        if isinstance(db, ReadStore):
            db.con.rollback()
            db.con.close()


if __name__ == '__main__':
    sys.exit(main())
