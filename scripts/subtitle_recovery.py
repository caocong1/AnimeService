"""Read-only recovery plan and immutable repair evidence journal; never writes business DB."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.subtitle_delivery_queue import ReadStore
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_recovery import (record_attempt, update_monitor, acknowledge_notice,
                                    round_status, read_json, attempts, archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'data/anime.db'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    planning = sub.add_parser('plan-round', help='One live snapshot for scheduling, evidence and round-check')
    planning.add_argument('--before', required=True, type=Path)
    fence = sub.add_parser('isolate-resume', help='Fence a stopped original resume without resolving its receipt')
    fence.add_argument('job_id')
    fence.add_argument('--revision', type=int, required=True)
    fence.add_argument('--native', type=Path, required=True)
    fence.add_argument('--apply', action='store_true')
    host = sub.add_parser('claim-host', help='Claim a previously unsent desktop handoff exactly once')
    host.add_argument('job_id')
    host.add_argument('--revision', required=True, type=int)
    sub.add_parser('monitor')
    check = sub.add_parser('round-check', help='Read-only verification that a coordinator round handled due work')
    check.add_argument('--before', required=True, help='This round\'s saved recovery status JSON')
    notice = sub.add_parser('notice-delivered')
    notice.add_argument('notification_key')
    record = sub.add_parser('record')
    record.add_argument('job_id')
    record.add_argument('--action', required=True)
    record.add_argument('--outcome', choices=('failed', 'progress', 'blocked'), required=True)
    record.add_argument('--evidence', action='append', required=True)
    record.add_argument('--expected-input', help='Input key from the recovery item used for the work')
    record.add_argument('--expected-condition', help='Condition from that same recovery item')
    args = parser.parse_args()
    if args.command == 'record' and bool(args.expected_input) != bool(args.expected_condition):
        parser.error('--expected-input and --expected-condition must be supplied together')
    if args.command == 'isolate-resume':
        from scripts.subtitle_delivery_queue import ControlStore
        from scripts.subtitle_output_lock import output_lock
        from anime.subtitle_dispatch_fence import isolate
        delivery = SubtitleDelivery(ControlStore(args.db), initialize=False)
        project = Path(args.db).resolve().parent.parent
        with output_lock(project/'data/subtitle-studio/.render.process.lock','check_stopped_render',timeout=0), \
             output_lock(project/'static/finished-media/.publish.lock','check_stopped_publication',timeout=0):
            result = isolate(delivery, args.job_id, args.revision, args.native, args.native.stat().st_mtime, apply=args.apply)
    elif args.command == 'claim-host':
        from scripts.subtitle_delivery_queue import ControlStore
        from anime.subtitle_host_handoff import claim
        result = claim(ControlStore(args.db),args.job_id,args.revision)
    elif args.command == 'monitor':
        result = update_monitor(args.db)
    else:
        saved_replies = []
        if args.command == 'plan-round':
            from scripts.subtitle_delivery_queue import ControlStore
            from anime.subtitle_host_handoff import reconcile_replies
            saved_replies = reconcile_replies(SubtitleDelivery(ControlStore(args.db), initialize=False))
        db = ReadStore(args.db)
        try:
            snapshot = SubtitleDelivery(db, initialize=False).snapshot()
            plan = snapshot['recovery']
            from anime.subtitle_host_handoff import waiting
            plan['unsent_host_calls'] = waiting(db.con)
            ready_ids = {item['job_id'] for item in plan['items'] if item['status']=='ready'}
            ready = [j for j in snapshot['jobs'] if j['id'] in ready_ids]
            unresolved = [j for j in snapshot['jobs'] if j['dispatch_state'] in ('committing','unknown')]
            plan['dispatch'] = {'capacity':snapshot['capacity'],
                'ready':[{'job_id':j['id'],'input_key':j['input_key'],'revision':j['revision']} for j in ready],
                'acknowledged':[{'job_id':j['id'],'input_key':j['input_key'],'thread_id':j['thread_id']}
                    for j in snapshot['jobs'] if j['dispatch_state']=='acknowledged' and j['thread_id']],
                'unresolved':[{'job_id':j['id'],'revision':j['revision'],'thread_id':j['thread_id'],
                    'original_turn':j['payload'].get('auto_recovery_turn'),'fenced':j['slot_fenced'],
                    'watched':j['finished'],'scope_blocker':j['scope_blocker']} for j in unresolved]}
            if args.command == 'status':
                result = plan
            elif args.command == 'plan-round':
                with args.before.open('x', encoding='utf-8') as f:
                    json.dump(plan,f,ensure_ascii=False)
                result = {**plan['dispatch'],'reconciled_host_replies':saved_replies,'unsent_host_calls':plan['unsent_host_calls'],'next_preparation':plan.get('next_action'),
                          'enrollment':snapshot['enrollment_coverage'],'official':plan.get('official_release_review'),
                          'before':str(args.before)}
            elif args.command == 'round-check':
                result = round_status(read_json(args.before, 4 * 1024 * 1024), plan,
                                      attempts(archive(db)), capacity=snapshot['capacity'])
            elif args.command == 'notice-delivered':
                result = acknowledge_notice(db, plan, args.notification_key)
            else:
                result = record_attempt(db, plan, args.job_id, args.action, args.outcome, args.evidence,
                                        expected_input=args.expected_input,
                                        expected_condition=args.expected_condition)
        finally:
            db.con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if args.command == 'round-check' and not result['complete'] else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
