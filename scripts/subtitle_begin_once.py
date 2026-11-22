"""Archive one normal begin call, with no host bridge/call or retry.

Use a NEW private journal directory for a future, currently ready job. This is
not a recovery command for an old committing/unknown intent.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.dispatch_caller import DispatchFailure, verified_ticket
from scripts.subtitle_delivery_queue import protect_ticket_directory, public
from scripts.subtitle_dispatch_once import run_delivery_cli


def begin_once(*, job_id, owner, revision, expected_input, journal, db_path=None, auto_recovery_turn=None, queue_host=False):
    if not expected_input or not owner.strip():
        raise ValueError('An observed input and explicit owner are required')
    journal = Path(journal).absolute()
    if journal.resolve() != journal:
        raise ValueError('Journal must not be redirected')
    # Never reuse an old round, overwrite failed evidence, or apply ACL to an
    # unrelated existing directory. Preparation precedes any begin attempt.
    journal.mkdir(mode=0o700, parents=False, exist_ok=False)
    protect_ticket_directory(journal)
    verification_path = journal/'verified.json'
    with verification_path.open('x', encoding='utf-8') as verified:
        args = ['begin', job_id, '--owner', owner, '--revision', str(revision),
                '--expected-input', expected_input]
        if queue_host:
            args.append('--queue-host')
        if auto_recovery_turn:
            args.extend(['--auto-recovery-turn', auto_recovery_turn])
        result = run_delivery_cli(args, journal, 1, db_path=db_path)
        ticket, digest = verified_ticket(result, result.get('ticket_path'))
        if ticket['job_id'] != job_id or ticket['input_key'] != expected_input:
            raise DispatchFailure('Original begin identity differs; preserve intent and audit')
        if auto_recovery_turn and (ticket['dispatch_action'] != 'reuse' or
                                   not (result.get('auto_recovery') or {}).get('automatic_recovery_recorded')):
            raise DispatchFailure('Original owner recovery not verified; preserve intent and audit')
        record = {'schema': 'begin-call-verification/v1', 'job_id': job_id,
                  'input_key': expected_input, 'revision': result['revision'],
                  'ticket_path': result['ticket_path'], 'ticket_sha256': digest,
                  'dispatch_action': result['dispatch_action'], 'verified': True,
                  'host_call_started': False, 'not_created_proven': False}
        json.dump(record, verified, ensure_ascii=False, indent=2)
        verified.write('\n')
        verified.flush()
        os.fsync(verified.fileno())
    if queue_host:
        return result['host_handoff']
    if auto_recovery_turn:
        return {**record, 'responsible_thread_id': ticket['thread_id'], 'session_prompt': result['session_prompt']}
    return public(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--owner', required=True)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--expected-input', required=True)
    parser.add_argument('--auto-recovery-turn', help='Exact original turn of a newly eligible stopped owner')
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--queue-host', action='store_true', help='Persist an unsent desktop handoff')
    parser.add_argument('--db', type=Path, default=ROOT/'data/anime.db')
    args = parser.parse_args()
    try:
        result = begin_once(job_id=args.job_id, owner=args.owner, revision=args.revision,
                            expected_input=args.expected_input, journal=args.journal, db_path=args.db,
                            auto_recovery_turn=args.auto_recovery_turn, queue_host=args.queue_host)
    except Exception as error:
        # Do not echo command/authority or interpret absence of a return value.
        print(json.dumps({'error_type': type(error).__name__, 'success': False,
                          'inspect_original_journal': True, 'host_call_started': False,
                          'not_created_proven': False, 'reconcile_required': True}), file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
