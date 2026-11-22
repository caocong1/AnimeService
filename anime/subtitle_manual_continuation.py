"""Explicit human assignment after a ticket-archive failure, never automatic retry.

This is a separate provenance from created/not-created dispatch receipts. The
unresolved original row is retained verbatim in a private, append-only DB table.
No credential is reconstructed, cleared, returned, or exported.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from .subtitle_delivery import (_scope, _scope_transaction, _json, _hash,
                                production_conflict)
from .viewing_workflow import unreleased_date

SCHEMA = 'subtitle-human-continuation/v1'
TIMEOUT_SCHEMA = 'subtitle-human-timeout-continuation/v1'
TABLE = 'subtitle_delivery_manual_continuations'


def load_evidence(root, path):
    path = Path(path).resolve()
    path.relative_to((Path(root) / 'data/subtitle-studio').resolve())
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Manual continuation evidence exceeds limit')
    return json.loads(raw.decode('utf-8-sig')), {
        'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def _timeout_original(auth, native, identity, root):
    """Validate observed timeout provenance, without asserting host absence."""
    if (auth.get('preserve_original_unknown_intent') is not True
            or auth.get('production_dispatch_messages_reviewed_absent') is not True):
        raise ValueError('Explicit authorization must preserve the unknown original outcome')
    row = native.get('turn_row') or {}
    if (row.get('turn_id') != auth.get('original_turn_id')
            or native.get('thread_id') != row.get('thread_id')
            or row.get('status') != 'completed' or not row.get('completed_at')):
        raise ValueError('Complete persisted original native turn required')
    matches = [x for x in native.get('items', []) if x.get('id') == auth.get('original_failed_command_id')]
    if len(matches) != 1:
        raise ValueError('Exact original timeout operation required')
    command = matches[0]
    output = command.get('aggregatedOutput', '')
    wrapper = auth.get('original_wrapper') or {}
    path = Path(wrapper.get('path', '')).resolve()
    path.relative_to((Path(root) / 'data/subtitle-studio').resolve())
    raw = path.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != wrapper.get('sha256'):
        raise ValueError('Original timeout wrapper changed')
    text = raw.decode('utf-8-sig')
    if (command.get('type') != 'commandExecution' or command.get('status') != 'failed'
            or command.get('exitCode') != 1 or command.get('output') is not None
            or 'subprocess.TimeoutExpired' not in output
            or not re.search(r"['\"]" + re.escape(identity) + r"['\"]", output)
            or 'timed out after 120 seconds' not in output
            or path.name not in command.get('command', '')
            or "'scripts/subtitle_delivery_queue.py'" not in text
            or not re.search(r"'begin'\s*,\s*'" + re.escape(identity) + "'", text)
            or 'timeout=120' not in text):
        raise ValueError('Original completed turn must bind the exact failed timeout wrapper')
    allowed = auth.get('allowed_maintenance_messages') or []
    observed = []
    for item in native.get('items', []):
        if item.get('type') != 'mcpToolCall':
            continue
        if item.get('tool') in ('create_thread', 'handoff_thread'):
            raise ValueError('Original host dispatch requires original-owner reconciliation')
        if item.get('tool') == 'send_message_to_thread':
            args = item.get('arguments') or {}
            observed.append({'id': item.get('id'), 'thread_id': args.get('threadId'),
                'prompt_sha256': hashlib.sha256(args.get('prompt', '').encode()).hexdigest(),
                'engineering_only_no_production_dispatch': True})
    if observed != allowed:
        raise ValueError('Every original cross-chat message must match the reviewed maintenance evidence')
    return {'id': row['turn_id'], 'status': row['status'],
            'startedAt': row['started_at'], 'completedAt': row['completed_at'],
            'items': native['items']}, wrapper


def _observe_related_processes(identity, wrapper, target):
    """A fresh current observation only; never a historical not-created receipt."""
    import psutil
    _, sid, ep = identity.split(':')
    markers = [identity, f'{sid}-s01e{int(ep):02}', f'/{sid}/s01e{int(ep):03}',
               str(wrapper['path']), Path(target['path']).name]
    markers = [m.replace('\\', '/').casefold() for m in markers]
    current = psutil.Process()
    own = {current.pid, *[p.pid for p in current.parents()]}
    matches = []
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.pid in own or not re.match(r'^(python(?:w)?|ffmpeg|ffprobe)(?:\.exe)?$', proc.info['name'] or '', re.I):
            continue
        try:
            command = ' '.join(proc.cmdline()).replace('\\', '/').casefold()
            if any(m in command for m in markers):
                matches.append({'pid': proc.pid, 'name': proc.info['name'], 'created': proc.create_time()})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ValueError('Cannot inspect a possible subtitle writer') from exc
    return {'observed_at': time.time(), 'status': 'observed', 'related_processes': matches,
            'historical_host_absence_claimed': False}


def continue_manually(delivery, identity, thread_id, expected_revision,
                      expected_input, authorization, native, dry_run=True,
                      preserve_timeout_intent=False, capability_evidence=None):
    """Assign an existing human-requested chat once; no host call or absence claim.

    authorization and native are (parsed data, actual file reference) pairs from
    load_evidence. Only a new, explicit human answer authorizes this exception.
    A timeout requires the separate, explicitly authorized preserved-intent route.
    Neither route accepts an existing/stopped owner or claims a dispatch outcome.
    """
    auth, auth_ref = authorization
    turn, native_ref = native
    schema = TIMEOUT_SCHEMA if preserve_timeout_intent else SCHEMA
    if (auth.get('schema') != schema or auth.get('job_id') != identity
            or auth.get('input_key') != expected_input
            or auth.get('expected_revision') != expected_revision
            or auth.get('responsible_thread_id') != thread_id
            or auth.get('explicit_human_approval') is not True
            or not isinstance(auth.get('human_answer'), str) or not auth['human_answer'].strip()
            or not isinstance(thread_id, str) or not thread_id.strip()):
        raise ValueError('Explicit episode-bound human continuation authorization required')
    wrapper = None
    if preserve_timeout_intent:
        turn, wrapper = _timeout_original(auth, turn, identity, delivery.db.path.parent.parent)
    if (turn.get('id') != auth.get('original_turn_id') or turn.get('status') != 'completed'
            or not turn.get('completedAt')):
        raise ValueError('Original native turn must have completed')
    items = turn.get('items', [])
    matches = [x for x in items if x.get('id') == auth.get('original_failed_command_id')]
    if len(matches) != 1:
        raise ValueError('Exact original failed begin operation required')
    command = matches[0]
    output = command.get('output', {})
    command_text = command.get('command', '')
    if not preserve_timeout_intent and (command.get('type') != 'commandExecution' or command.get('status') != 'failed'
            or command.get('exitCode') != 1 or output.get('truncated') is not False
            or not re.search(r'\bsubtitle_delivery_queue\.py\s+begin\s+'
                             + re.escape(identity) + r'(?=\s|$)', command_text)
            or 'save_ticket' not in output.get('text', '')
            or 'protect_ticket_directory' not in output.get('text', '')
            or 'cannot secure the ticket directory' not in output.get('text', '')):
        raise ValueError('This exception requires native proof of the pre-file archive failure')
    if not preserve_timeout_intent and any(x.get('type') == 'mcpToolCall' and x.get('tool') in
           ('create_thread', 'send_message_to_thread', 'handoff_thread') for x in items):
        raise ValueError('A host-writing operation in the original turn requires original dispatch reconciliation')
    evidence = {'authorization': auth_ref, 'native_original_turn': native_ref}
    if wrapper:
        evidence['original_timeout_wrapper'] = wrapper
    capability_record = capability_key = capability_before = None
    if capability_evidence:
        if not preserve_timeout_intent:
            raise ValueError('Fresh capability overlay belongs to the explicit timeout continuation only')
        evidence['fresh_delivery_capability'] = capability_evidence[1]
    continuation_id = _hash({'schema': schema, 'job_id': identity,
        'expected_revision': expected_revision, 'input_key': expected_input,
        'thread_id': thread_id, 'evidence': evidence})
    # An identical already-applied request remains a read-only replay even after
    # this owner produces a release or changes progress. Never replay assignment.
    exists = delivery.db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,))
    previous = delivery.db.one(f'SELECT result FROM {TABLE} WHERE id=?', (continuation_id,)) if exists else None
    if previous:
        return {**json.loads(previous['result']), 'duplicate': True}
    job = delivery.get(identity)
    if _scope(delivery.db, job['show_id'], job['episode']):
        raise ValueError('Manual continuation scope is protected')
    if unreleased_date(delivery.db, job['show_id'], job['episode'], delivery.clock()):
        raise ValueError('Manual continuation episode is not released')
    assessor = delivery
    if capability_evidence:
        from .subtitle_evidence import _Overlay, binding_error, private_root
        from .subtitle_delivery import SubtitleDelivery
        report, reference = capability_evidence
        root = private_root(delivery.db)
        file = Path(reference['path']).resolve()
        file.relative_to(root)
        if hashlib.sha256(file.read_bytes()).hexdigest() != reference['sha256']:
            raise ValueError('Fresh capability report changed')
        if (report.get('job_id') != identity or report.get('input_key') != expected_input
                or binding_error(root, 'delivery_capability_report', report,
                    job['show_id'], job['episode'], job['payload']['assessment'].get('target_fingerprint'))):
            raise ValueError('Fresh capability must bind the same episode and fixed input')
        capability_key = f"subtitle_delivery_readiness_{job['show_id']}_{job['episode']}"
        capability_before = delivery.db.get(capability_key, {}) or {}
        capability_record = {**capability_before, 'delivery_capability_report': reference}
        evidence['previous_delivery_capability'] = capability_before.get('delivery_capability_report')
        assessor = SubtitleDelivery(_Overlay(delivery.db, capability_key, capability_record),
            readiness=delivery.readiness, clock=delivery.clock, initialize=False)
    assessment = assessor._dispatch_assessment(job)
    # Human authorization assigns this exact fixed bundle. Reuse the existing
    # signature reconstruction to admit only additional immutable archive refs,
    # without changing the original input or claiming an earlier created owner.
    # This is a read-only proposed owner view; the transaction below still checks
    # that the actual original row has no owner and no accepted created receipt.
    assessment = delivery._fixed_episode_assessment({**job, 'thread_id': thread_id}, assessment)
    if not assessment['ready'] or assessment['input_signature'] != expected_input:
        raise ValueError('Manual continuation requires current verified ready inputs')
    process_observation = None
    if wrapper:
        targets = [r['target'] for r in job['payload']['assessment']['material_refs'] if r.get('kind') == 'completed_media']
        if len(targets) != 1:
            raise ValueError('Exact fixed target required for current writer inspection')
        process_observation = _observe_related_processes(identity, wrapper, targets[0])
        if (process_observation.get('status') != 'observed'
                or process_observation.get('related_processes')
                or abs(time.time() - process_observation.get('observed_at', 0)) > 30):
            raise ValueError('Current related writer observation is not safely clear')

    def check(c):
        row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
        if (not row or row['revision'] != expected_revision or row['input_key'] != expected_input
                or row['thread_id'] or row['kind'] != 'episode'
                or row['state'] not in ('production', 'blocked')
                or row['dispatch_state'] not in ('committing', 'unknown')
                or not row['dispatch_token'] or row['lease_expires'] > delivery.clock()
                or row['accepted_build_id']):
            raise ValueError('Original unowned, expired-lease dispatch changed; preserve it')
        value = json.loads(row['payload'])
        if any(value.get(k) for k in ('finished', 'refinement_pending', 'user_stopped',
                                     'owner_stopped_writing', 'scope_blocker', 'manual_continuation')):
            raise ValueError('Existing delivery or stopped-owner protection must remain')
        if _scope_transaction(c, row['show_id'], row['episode']):
            raise ValueError('Manual continuation scope changed')
        watch = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?',
                          (row['show_id'], row['episode'])).fetchone()
        if watch and watch['finished']:
            raise ValueError('Watched episodes cannot be continued')
        conflict = production_conflict(c, row, delivery.clock(), thread_id, check_capacity=False)
        if conflict:
            raise ValueError('Production writer conflict: ' + conflict)
        started = c.execute("SELECT recorded FROM subtitle_delivery_events WHERE job_id=? AND kind='dispatch-started' ORDER BY id DESC LIMIT 1", (identity,)).fetchone()
        if (not started or not turn.get('startedAt', 0) <= started['recorded'] <= turn['completedAt']):
            raise ValueError('Native original turn does not bind the latest dispatch intent')
        receipts = c.execute("SELECT 1 FROM dispatch_receipts WHERE job_id=? AND receipt_accepted=1 AND outcome='created' LIMIT 1", (identity,)).fetchone()
        if receipts:
            raise ValueError('A created fact already exists; preserve its original owner')
        if capability_evidence:
            old = c.execute('SELECT value FROM kv WHERE key=?', (capability_key,)).fetchone()
            if (json.loads(old['value']) if old else {}) != capability_before:
                raise ValueError('Current readiness record changed')
            report, reference = capability_evidence
            if (hashlib.sha256(Path(reference['path']).read_bytes()).hexdigest() != reference['sha256']
                    or binding_error(root, 'delivery_capability_report', report,
                        row['show_id'], row['episode'], value['assessment'].get('target_fingerprint'))):
                raise ValueError('Fresh capability changed or expired before assignment')
        return dict(row), value

    if dry_run:
        with delivery.db.connect() as c:
            row, _ = check(c)
        return {'schema': schema, 'status': 'planned', 'job_id': identity,
                'continuation_id': continuation_id, 'expected_revision': row['revision'],
                'thread_id': thread_id, 'evidence': evidence, 'state_changed': False,
                'current_process_observation': process_observation}
    with delivery.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row, value = check(c)
        c.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, original_row TEXT NOT NULL,
            evidence TEXT NOT NULL, result TEXT NOT NULL, recorded REAL NOT NULL)''')
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_immutable_update BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT, 'manual continuation history is immutable'); END")
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_immutable_delete BEFORE DELETE ON {TABLE} BEGIN SELECT RAISE(ABORT, 'manual continuation history is immutable'); END")
        value['manual_continuation'] = {'schema': schema, 'id': continuation_id,
            'original_revision': row['revision'], 'original_dispatch_state': row['dispatch_state'],
            'verified_input_signature': assessment['input_signature'],
            'observed_input_signature': assessment.get('observed_input_signature', assessment['input_signature']),
            'evidence': evidence, 'authority': 'explicit_human_request',
            'original_outcome': 'unknown_preserved' if wrapper else 'pre_file_archive_failure',
            'current_process_observation': process_observation}
        result = {'schema': schema, 'status': 'assigned', 'job_id': identity,
            'continuation_id': continuation_id, 'thread_id': thread_id,
            'original_revision': row['revision'], 'revision': row['revision'] + 1,
            'state_changed': True, 'original_intent_preserved': True,
            'dispatch_created_claimed': False, 'dispatch_absence_claimed': False,
            'finished': False, 'accepted': False}
        c.execute(f'INSERT INTO {TABLE} VALUES(?,?,?,?,?,?)',
                  (continuation_id, identity, _json(row), _json(evidence), _json(result), delivery.clock()))
        if capability_record:
            c.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (capability_key, _json(capability_record)))
        # Retain original tokens, lease metadata, fixed input and all old payload.
        c.execute("UPDATE subtitle_delivery_jobs SET state='production',dispatch_state='acknowledged',thread_id=?,payload=?,revision=revision+1,updated=? WHERE id=?",
                  (thread_id, _json(value), delivery.clock(), identity))
        delivery._event(c, identity, 'human-manual-continuation', {
            **result, 'evidence': evidence,
            'original_row_sha256': hashlib.sha256(_json(row).encode()).hexdigest()})
    return result
