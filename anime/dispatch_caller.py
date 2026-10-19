"""One bounded dispatch operation with an injected, preflighted host bridge.

No host IPC is guessed here. A bridge must expose preflight and create/resume;
absence is never inferred and a failed/unknown side effect retains committing.
"""
from pathlib import Path
import hashlib
import json

from .subtitle_delivery import validate_ticket


class DispatchFailure(RuntimeError):
    pass


def verified_ticket(begin, ticket_path):
    """Read original authority once; compare only fields shared by public output.

    The CLI intentionally strips credential fields recursively. Equality against
    that redacted dictionary is therefore invalid even for a correct archive.
    """
    raw = Path(ticket_path).read_bytes()
    ticket = validate_ticket(json.loads(raw.decode('utf-8-sig')))
    inline = begin.get('ticket')
    if not isinstance(inline, dict):
        raise DispatchFailure('missing inline ticket binding')
    public_fields = ('schema', 'job_id', 'revision', 'owner', 'input_key', 'dispatch_action', 'thread_id', 'issued_at')
    if any(k not in inline or inline[k] != ticket[k] for k in public_fields):
        raise DispatchFailure('original ticket binding mismatch')
    for k in ('dispatch_token', 'lease_token'):
        if k in inline and inline[k] != ticket[k]:
            raise DispatchFailure('original ticket authority mismatch')
    expected_action = 'reuse' if begin.get('dispatch_action') == 'resume' else 'create'
    if (ticket['job_id'] != begin.get('id') or ticket['input_key'] != begin.get('input_key')
            or ticket['revision'] != begin.get('revision') or ticket['dispatch_action'] != expected_action
            or (ticket['thread_id'] or None) != (begin.get('responsible_thread_id') or None)):
        raise DispatchFailure('begin result binding mismatch')
    return ticket, hashlib.sha256(raw).hexdigest()


def dispatch_once(*, host, archive_preflight, begin, confirm, interrupted, journal):
    """Preflight -> persisted begin -> original archive -> one host call -> confirm.

    Callbacks carry the original ticket path, never live credentials. The host
    bridge is trusted deployment configuration, not a packet or prompt field.
    """
    capability = host.preflight()
    if not isinstance(capability, dict) or not all(capability.get(k) is True for k in ('create', 'resume')):
        raise DispatchFailure('host bridge unavailable; no begin performed')
    archive_preflight()
    result = begin()
    path = result.get('ticket_path')
    # A broken archive cannot supply a callback identity: record locally only.
    try:
        ticket, digest = verified_ticket(result, path)
    except Exception:
        journal({'phase': 'archive_invalid', 'reconcile_required': True, 'external_call_started': False})
        raise DispatchFailure('archive invalid; committing preserved') from None
    journal({'phase': 'ticket_verified', 'ticket_sha256': digest, 'job_id': ticket['job_id']})
    try:
        answer = host.dispatch(action=result['dispatch_action'], thread_id=result.get('responsible_thread_id'),
                               prompt=result['session_prompt'], title=result['suggested_title'],
                               job_id=ticket['job_id'], ticket_sha256=digest)
    except Exception:
        interrupted(path, 'host result unavailable')
        journal({'phase': 'interrupted', 'reconcile_required': True})
        raise DispatchFailure('host result unavailable; committing preserved') from None
    if not isinstance(answer, dict) or not isinstance(answer.get('thread_id'), str) or not answer['thread_id'].strip():
        interrupted(path, 'host result missing responsible identity')
        raise DispatchFailure('host result invalid; committing preserved')
    if ticket['dispatch_action'] == 'reuse' and answer['thread_id'] != ticket['thread_id']:
        interrupted(path, 'host returned a different responsible identity')
        raise DispatchFailure('host identity mismatch; committing preserved')
    # host_ref is passed only when actually supplied by the bridge.
    receipt = confirm(path, answer['thread_id'], answer.get('host_ref'))
    journal({'phase': 'confirmed' if receipt.get('receipt_accepted') else 'reconcile_required',
             'receipt_accepted': bool(receipt.get('receipt_accepted')), 'reason': receipt.get('reason')})
    return receipt
