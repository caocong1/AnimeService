"""Plan/apply verified official schedules through one auditable metadata entrypoint."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.db import Store
from anime.read_projection import read_projection
from anime.official_releases import register_schedule, review_plan, record_review, reconcile_released_facts
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'data/anime.db'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    for name in ('register-schedule', 'record-review'):
        cmd = commands.add_parser(name)
        cmd.add_argument('record')
        cmd.add_argument('--apply', action='store_true')
        if name == 'register-schedule':
            cmd.add_argument('--expected-sha')
    commands.add_parser('reconcile')
    args = parser.parse_args()
    if args.command == 'plan':
        with read_projection(args.db) as db:
            result = review_plan(db)
    elif args.command == 'reconcile':
        result = {'released_facts_recorded': reconcile_released_facts(Store(args.db), time.time())}
    else:
        record = json.loads(Path(args.record).read_text(encoding='utf-8-sig'))
        if args.apply:
            db = Store(args.db)
            if args.command == 'register-schedule':
                result = register_schedule(db, record, apply=True, expected_sha=args.expected_sha)
            else:
                result = record_review(db, record, apply=True)
        else:
            with read_projection(args.db) as db:
                result = (register_schedule(db, record, expected_sha=args.expected_sha)
                          if args.command == 'register-schedule' else record_review(db, record))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
