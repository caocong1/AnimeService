"""Operator-only explicit human continuation. Planning is the default; no host IPC."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_manual_continuation import continue_manually, load_evidence
from scripts.subtitle_delivery_queue import ControlStore


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('job_id')
    p.add_argument('--thread-id', required=True)
    p.add_argument('--revision', type=int, required=True)
    p.add_argument('--expected-input', required=True)
    p.add_argument('--authorization', required=True)
    p.add_argument('--native-evidence', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--preserve-timeout-intent', action='store_true',
                   help='Separate explicit human authorization retaining the unknown original timeout')
    p.add_argument('--capability-evidence', help='Fresh real output capability report for the exact preserved input')
    args = p.parse_args()
    if os.environ.get('CODEX_THREAD_ID') != args.thread_id:
        raise ValueError('Only the actual human-requested current chat may continue this episode')
    db = ControlStore(ROOT / 'data/anime.db')
    delivery = SubtitleDelivery(db, initialize=False)
    result = continue_manually(delivery, args.job_id, args.thread_id, args.revision,
        args.expected_input, load_evidence(ROOT, args.authorization),
        load_evidence(ROOT, args.native_evidence), dry_run=not args.apply,
        preserve_timeout_intent=args.preserve_timeout_intent,
        capability_evidence=load_evidence(ROOT, args.capability_evidence) if args.capability_evidence else None)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
