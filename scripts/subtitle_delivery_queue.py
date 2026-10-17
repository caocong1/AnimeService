"""Local delivery control plane; never instantiate Engine or call a downloader.

Use status for read-only snapshots. Mutations are explicit subcommands. Lease and
dispatch tokens stay in the local DB/process and are never printed or exported.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.db import Store
from anime.subtitle_delivery import SubtitleDelivery


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


def absence_evidence(path):
    """The verification record itself, bound by hash: who checked which chat list when, and how."""
    import hashlib
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('absence evidence must be a UTF-8 JSON object') from None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip() for k in ('checked_at', 'method', 'checked_by')):
        raise ValueError('absence evidence needs checked_at, method and checked_by')
    return {'file': Path(path).name, 'sha256': hashlib.sha256(raw).hexdigest(), **data}


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
    for name in ('confirm', 'unknown', 'blocked', 'accepted', 'finished'):
        p = sub.add_parser(name)
        p.add_argument('job_id')
        if name != 'unknown':
            p.add_argument('--thread-id', required=True)
        if name == 'blocked':
            p.add_argument('--reason', action='append', required=True)
            p.add_argument('--stopped', action='store_true')
        if name == 'accepted':
            p.add_argument('--build-id', required=True)
        if name == 'finished':
            p.add_argument('--release', required=True)
            p.add_argument('--player-report', required=True)
    args = parser.parse_args()
    db = ReadStore(args.db) if args.command == 'status' else ControlStore(args.db)
    delivery = SubtitleDelivery(db, initialize=args.command != 'status')
    try:
        if args.command == 'status':
            result = delivery.snapshot()
        elif args.command == 'refresh':
            result = delivery.refresh()
        elif args.command == 'progress':
            from anime.subtitle_delivery_view import report_progress
            result = report_progress(db, args.job_id, args.thread_id, args.stage, args.done, args.total)
        elif args.command == 'refine':
            from anime.subtitle_refinement import enqueue_refinement
            result = enqueue_refinement(delivery, args.job_id) or {'status': 'preserved_or_not_eligible'}
        elif args.command == 'begin':
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
                released = delivery.release(args.job_id, args.owner, lease['revision'], lease['lease_token'], reason='begin failed: ' + str(error)[:200])
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
        elif args.command == 'release-lease':
            result = delivery.release(args.job_id, args.owner, args.revision, reason='operator release')
        elif args.command == 'not-created':
            result = delivery.reconcile_dispatch(args.job_id, delivery.get(args.job_id)['dispatch_token'],
                                                 definitely_not_created=True, absence_evidence=absence_evidence(args.evidence))
        elif args.command in ('confirm', 'unknown'):
            record = delivery.get(args.job_id)
            result = delivery.reconcile_dispatch(args.job_id, record['dispatch_token'],
                                                 thread_id=args.thread_id if args.command == 'confirm' else None)
        elif args.command == 'blocked':
            result = delivery.record_blocked(args.job_id, args.thread_id, args.reason, owner_stopped=args.stopped)
        elif args.command == 'finished':
            result = delivery.record_finished(args.job_id, args.thread_id, args.release, args.player_report)
        else:
            result = delivery.record_accepted(args.job_id, args.thread_id, args.build_id)
        if args.command != 'status':
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
    main()
