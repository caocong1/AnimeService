"""Explicit local-operator recovery of a rejected, real created-chat receipt.

No host calls, new tickets, absence claims or automatic recovery. The operator
archives actual native tool responses; this is local provenance, not a signed
remote attestation. The ordinary receipt path and its immutable ticket stay intact.
"""
import datetime
import hashlib
import json
import re
from pathlib import Path

from .subtitle_delivery import (
    RECEIPT_SCHEMA, _canonical, _digest, _json, _scope_transaction, validate_ticket,
)

RECONCILIATION_SCHEMA = 'dispatch-reconciliation/v1'
HOST_SCHEMA = 'dispatch-created-host-observation/v1'
HOST_MAX_BYTES = 2 * 1024 * 1024
HOST_MAX_AGE = 300
_JSON_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')


def _reject_authority(value, ticket):
    """Inspect keys and values, including nested JSON-string escape layers.

    Normalization is only for inspection; it never changes archived evidence.
    Excessive encoding depth is rejected rather than bypassing the check.
    """
    tokens = (ticket['dispatch_token'], ticket['lease_token'])
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current)
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif isinstance(current, str):
            for depth in range(9):
                if any(token and token in current for token in tokens):
                    raise ValueError('Reconciliation metadata must not contain authority tokens')
                if not _JSON_ESCAPE.search(current):
                    break
                if depth == 8:
                    raise ValueError('Reconciliation metadata encoding depth exceeded')
                current = _JSON_ESCAPE.sub(lambda m: json.loads('"' + m[0] + '"'), current)


def _tool_body(result):
    if not isinstance(result, dict) or result.get('isError'):
        raise ValueError('Successful native host tool response required')
    texts = [x.get('text') for x in result.get('content', []) if x.get('type') == 'text']
    if len(texts) != 1:
        raise ValueError('One raw native host JSON response required')
    try:
        body = json.loads(texts[0])
    except (TypeError, ValueError):
        raise ValueError('Native host response is not JSON') from None
    if not isinstance(body, dict):
        raise ValueError('Native host response must be an object')
    return body


def _host_observation(path, ticket, source_id, revision, thread_id, project_root):
    # Reject even nonexistent paths before an OS exception could echo a token.
    _reject_authority(str(path), ticket)
    with Path(path).open('rb') as stream:
        raw = stream.read(HOST_MAX_BYTES + 1)
    if len(raw) > HOST_MAX_BYTES:
        raise ValueError('Host observation exceeds the size limit')
    try:
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict) or value.get('schema') != HOST_SCHEMA:
            raise ValueError()
        checked = datetime.datetime.fromisoformat(value['checked_at'])
        if checked.tzinfo is None:
            raise ValueError()
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise ValueError('Timestamped host observation bundle required') from None
    _reject_authority(value, ticket)
    for key, expected in (
        ('job_id', ticket['job_id']), ('input_key', ticket['input_key']),
        ('source_receipt_id', source_id), ('expected_current_revision', revision),
        ('thread_id', thread_id), ('host_id', 'local'),
        ('dispatch_tool', 'create_thread' if ticket['dispatch_action'] == 'create' else 'send_message_to_thread'),
    ):
        if value.get(key) != expected or isinstance(value.get(key), bool):
            raise ValueError('Host observation identity mismatch: ' + key)
    for key in ('checked_by', 'authorization_ref'):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key].strip()) <= 500:
            raise ValueError('Explicit operator and user authorization reference required')
    created = _tool_body(value.get('original_dispatch_result'))
    read = _tool_body(value.get('read_thread_result'))
    waited = _tool_body(value.get('wait_threads_result'))
    _reject_authority((created, read, waited), ticket)
    thread = read.get('thread') or {}
    polls = waited.get('polls') or []
    if (created.get('threadId') != thread_id or created.get('hostId') != 'local'
            or thread.get('id') != thread_id or thread.get('hostId') != 'local'
            or thread.get('kind') != 'codex' or len(polls) != 1 or waited.get('errors')):
        raise ValueError('Exact existing local Codex thread evidence required')
    cwd = thread.get('cwd')
    if not isinstance(cwd, str) or not cwd.strip() or not Path(cwd).is_absolute():
        raise ValueError('Explicit absolute host project directory required')
    try:
        same_project = Path(cwd).resolve() == Path(project_root).resolve()
    except (OSError, ValueError):
        raise ValueError('Host project directory is invalid') from None
    if not same_project:
        raise ValueError('Host thread belongs to another project')
    preview = thread.get('preview') or ''
    # The actual production template binds both identities in its initial input.
    if f"job={ticket['job_id']}，" not in preview or f"input={ticket['input_key']}。" not in preview:
        raise ValueError('Host initial input does not bind this job and fixed input')
    poll = polls[0]
    turn = poll.get('latestTurn') or {}
    recent = read.get('turns') or []
    marker = poll.get('latestToolMarker') or {}
    if (poll.get('thread', {}).get('id') != thread_id or poll.get('thread', {}).get('hostId') != 'local'
            or thread.get('status', {}).get('type') not in ('idle', 'notLoaded')
            or poll.get('thread', {}).get('status', {}).get('type') not in ('idle', 'notLoaded')
            or not recent or not turn.get('id') or recent[0].get('id') != turn['id']
            or turn.get('status') not in ('completed', 'interrupted', 'failed')
            or recent[0].get('status') != turn['status']
            or (marker and (marker.get('turnId') != turn['id'] or marker.get('status') not in ('completed', 'failed')))):
        raise ValueError('Host thread or latest tool is still active or inconsistent')
    return {
        'file': Path(path).name, 'sha256': hashlib.sha256(raw).hexdigest(),
        'checked_at': value['checked_at'], 'checked_by': value['checked_by'],
        'authorization_ref': value['authorization_ref'], 'host_id': 'local',
        'thread_id': thread_id, 'observed_turn_id': turn['id'],
        'observed_turn_status': turn['status'], 'observed_tool_marker_id': marker.get('id'),
    }, checked.timestamp()


def reconcile_created(delivery, ticket, source_receipt_id, expected_revision, thread_id,
                      host_evidence_path, recorded_by=''):
    """CAS the explicit CURRENT revision while retaining all ORIGINAL authority.

    Only a rejected revision_moved created fact is eligible. The new derived
    receipt preserves the original rejection; it never creates or resumes a chat.
    """
    validate_ticket(ticket)
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision <= ticket['revision']):
        raise ValueError('Explicit current revision after the original ticket required')
    if (not isinstance(source_receipt_id, str) or len(source_receipt_id) != 64
            or any(c not in '0123456789abcdef' for c in source_receipt_id)):
        raise ValueError('Original created receipt id required')
    if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 200:
        raise ValueError('Original responsible thread required')
    if not isinstance(recorded_by, str) or len(recorded_by) > 200:
        raise ValueError('Bounded local operator identity required')
    _reject_authority(recorded_by, ticket)
    evidence, stamp = _host_observation(host_evidence_path, ticket, source_receipt_id,
                                       expected_revision, thread_id, delivery.db.path.parent.parent)
    td = _digest(_canonical(ticket))
    envelope = {
        'schema': RECONCILIATION_SCHEMA, 'job_id': ticket['job_id'],
        'source_receipt_id': source_receipt_id, 'ticket_digest': td,
        'original_revision': ticket['revision'], 'expected_revision': expected_revision,
        'dispatch_digest': _digest(ticket['dispatch_token']), 'lease_digest': _digest(ticket['lease_token']),
        'owner': ticket['owner'], 'input_key': ticket['input_key'],
        'dispatch_action': ticket['dispatch_action'], 'thread_id': thread_id,
        'outcome': 'created', 'evidence': evidence,
    }
    _reject_authority((envelope, recorded_by), ticket)
    canonical = _canonical(envelope)
    identity = _digest(canonical)
    with delivery.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        replay = c.execute('SELECT request_digest,result FROM dispatch_receipts WHERE receipt_id=?', (identity,)).fetchone()
        if replay:
            if replay['request_digest'] != identity:
                raise ValueError('Reconciliation id conflict')
            result = {**json.loads(replay['result']), 'replayed': True}
            _reject_authority(result, ticket)
            return result
        # Replay precedes freshness and current-state checks. No old successful
        # reconciliation is reapplied after a later stop, watch or dispatch.
        now = delivery.clock()
        if stamp < max(ticket['issued_at'], now - HOST_MAX_AGE) or stamp > now + 30:
            raise ValueError('Fresh native host reverification required')
        source = c.execute('SELECT * FROM dispatch_receipts WHERE receipt_id=?', (source_receipt_id,)).fetchone()
        if (not source or source['schema'] != RECEIPT_SCHEMA or source['job_id'] != ticket['job_id']
                or source['outcome'] != 'created' or source['reason'] != 'revision_moved'
                or source['receipt_accepted'] or source['state_changed']):
            raise ValueError('Original rejected revision_moved created receipt required')
        if stamp < source['recorded_at']:
            raise ValueError('Host reverification must follow the original rejected receipt')
        old = json.loads(source['envelope'])
        expected = {
            'schema': RECEIPT_SCHEMA, 'job_id': ticket['job_id'], 'ticket_digest': td,
            'expected_revision': ticket['revision'], 'input_key': ticket['input_key'],
            'owner': ticket['owner'], 'dispatch_digest': envelope['dispatch_digest'],
            'lease_digest': envelope['lease_digest'], 'dispatch_action': ticket['dispatch_action'],
            'ticket_thread_id': ticket['thread_id'], 'thread_id': thread_id, 'outcome': 'created',
        }
        if (any(old.get(k) != v for k, v in expected.items())
                or source['request_digest'] != _digest(_canonical(old))
                or (ticket['dispatch_action'] == 'reuse' and ticket['thread_id'] != thread_id)):
            raise ValueError('Original created fact does not bind the ticket and thread')
        for fact in c.execute("SELECT envelope FROM dispatch_receipts WHERE job_id=? AND schema=? AND outcome='created'",
                              (ticket['job_id'], RECEIPT_SCHEMA)):
            other = json.loads(fact['envelope'])
            if other.get('ticket_digest') == td and other.get('thread_id') != thread_id:
                raise ValueError('Conflicting original created-thread facts require separate review')
        issued = c.execute("SELECT payload FROM subtitle_delivery_events WHERE job_id=? AND kind='dispatch-ticket-issued' ORDER BY id DESC LIMIT 1",
                           (ticket['job_id'],)).fetchone()
        if not issued or json.loads(issued['payload']).get('ticket_digest') != td:
            raise ValueError('Original begin ticket is not the latest server-bound dispatch')
        row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (ticket['job_id'],)).fetchone()
        if not row:
            raise ValueError('Original job no longer exists')
        reason, accepted = '', 0
        if row['revision'] != expected_revision:
            reason = 'revision_moved'
        elif row['dispatch_token'] != ticket['dispatch_token']:
            reason = 'dispatch_mismatch'
        elif row['input_key'] != ticket['input_key']:
            reason = 'input_moved'
        elif row['lease_owner'] != ticket['owner'] or row['lease_token'] != ticket['lease_token']:
            reason = 'owner_mismatch'
        else:
            accepted = 1
            watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?', (row['show_id'], row['episode'])).fetchone()
            if _scope_transaction(c, row['show_id'], row['episode']) or (watched and watched['finished']):
                reason = 'scope_changed'
            elif (row['state'] not in ('production', 'blocked') or json.loads(row['payload']).get('owner_stopped_writing')
                  or row['dispatch_state'] not in ('committing', 'unknown')
                  or (row['thread_id'] and row['thread_id'] != thread_id)):
                reason = 'state_conflict'
            elif c.execute("SELECT id FROM subtitle_delivery_jobs WHERE id!=? AND (dispatch_state IN ('committing','unknown','acknowledged') OR lease_expires>?) LIMIT 1",
                           (ticket['job_id'], now)).fetchone():
                reason = 'other_owner_active'
        changed = 0
        if not reason:
            changed = c.execute("UPDATE subtitle_delivery_jobs SET state='production',dispatch_state='acknowledged',thread_id=?,lease_expires=0,revision=revision+1,updated=? WHERE id=? AND revision=? AND dispatch_token=? AND input_key=? AND lease_owner=? AND lease_token=?",
                                (thread_id, now, ticket['job_id'], expected_revision, ticket['dispatch_token'],
                                 ticket['input_key'], ticket['owner'], ticket['lease_token'])).rowcount
            if changed != 1:
                raise ValueError('Reconciliation CAS failed')
        after = dict(c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (ticket['job_id'],)).fetchone())
        job = {k: v for k, v in after.items() if k not in ('lease_token', 'dispatch_token')}
        job['payload'] = json.loads(job['payload'])
        result = {
            'schema': RECONCILIATION_SCHEMA, 'receipt_id': identity, 'source_receipt_id': source_receipt_id,
            'outcome': 'created', 'receipt_accepted': accepted, 'state_changed': changed, 'reason': reason,
            'resulting_revision': after['revision'], 'resulting_state': after['state'],
            'resulting_dispatch_state': after['dispatch_state'], 'job': job,
        }
        # DB-derived payload is known only inside the transaction. Reject before
        # persisting/returning it; the preceding CAS then rolls back as well.
        _reject_authority(result, ticket)
        delivery._event(c, ticket['job_id'], 'dispatch-created-reconciliation', {
            'receipt_id': identity, 'source_receipt_id': source_receipt_id, 'ticket_digest': td,
            'expected_revision': expected_revision, 'thread_id': thread_id,
            'receipt_accepted': accepted, 'state_changed': changed, 'reason': reason,
        })
        c.execute('INSERT INTO dispatch_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (identity, RECONCILIATION_SCHEMA, ticket['job_id'], expected_revision, identity, canonical,
                   'created', accepted, changed, reason, after['state'], after['dispatch_state'],
                   after['revision'], _json(result), now, recorded_by))
        return result
