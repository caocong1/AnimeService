"""One-shot handoff between a verified episode ticket and the desktop coordinator."""
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
import time

from .dispatch_caller import verified_ticket
from .subtitle_delivery import _scope_transaction, production_conflict

PREFIX = 'subtitle_host_handoff:'


def key(job, revision):
    return f'{PREFIX}{job}:{revision}'


def prepare(db, begin):
    ticket, digest = verified_ticket(begin, begin['ticket_path'])
    entry = {k: begin.get(k) for k in ('id', 'input_key', 'revision', 'ticket_path', 'dispatch_action',
                                         'responsible_thread_id', 'suggested_title', 'session_prompt')}
    entry.update(ticket_sha256=digest, state='waiting',
                 host_reply_path=str(Path(begin['ticket_path']).with_suffix('.host-reply.json')))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (ticket['job_id'],)).fetchone()
        if (not current or current['revision'] != ticket['revision'] or current['dispatch_state'] != 'committing'
                or current['input_key'] != ticket['input_key'] or current['dispatch_token'] != ticket['dispatch_token']):
            raise ValueError('Original dispatch changed')
        c.execute('INSERT INTO kv(key,value) VALUES(?,?)', (key(ticket['job_id'],ticket['revision']),json.dumps(entry)))
    return {'id': ticket['job_id'], 'job_id': ticket['job_id'], 'revision': ticket['revision'], 'state': 'waiting'}


def waiting(c):
    return [dict(row) for row in c.execute("""SELECT j.id,j.id AS job_id,j.revision FROM kv k JOIN subtitle_delivery_jobs j
        ON k.key=?||j.id||':'||j.revision WHERE j.dispatch_state='committing'
        AND json_extract(k.value,'$.state')='waiting' ORDER BY j.updated""", (PREFIX,))]


def claim(db, job, revision):
    from scripts.subtitle_delivery_queue import load_ticket
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        k = key(job, revision)
        saved = c.execute('SELECT value FROM kv WHERE key=?',(k,)).fetchone()
        record = json.loads(saved[0]) if saved else {}
        if record.get('state') != 'waiting':
            raise ValueError('No unsent handoff')
        ticket = load_ticket(record['ticket_path'])
        current = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(job,)).fetchone()
        if (hashlib.sha256(Path(record['ticket_path']).read_bytes()).hexdigest() != record['ticket_sha256']
                or not current or current['dispatch_state'] != 'committing' or current['revision'] != revision
                or current['input_key'] != ticket['input_key'] or current['dispatch_token'] != ticket['dispatch_token']
                or ticket['job_id'] != job or ticket['revision'] != revision or ticket['thread_id'] != (record['responsible_thread_id'] or '')):
            raise ValueError('Original ticket changed')
        payload = json.loads(current['payload'])
        sid, ep = current['show_id'],current['episode']
        if (_scope_transaction(c,sid,ep) or payload.get('user_stopped')
                or c.execute('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1',(sid,ep)).fetchone()):
            raise ValueError('Episode outside automatic production')
        conflict = production_conflict(c,current,time.time(),record['responsible_thread_id'])
        if conflict:
            raise ValueError(conflict)
        record['session_prompt'] += '\n\n本次制作编号 ' + uuid.uuid4().hex
        record.update(job_id=ticket['job_id'], state='claimed', claimed_at=time.time(), caller=os.environ.get('CODEX_THREAD_ID'))
        c.execute('UPDATE kv SET value=? WHERE key=?',(json.dumps(record),k))
    return record


def native_reply(entry, history):
    """Read only the sending turn's completed native tool call and its original result."""
    if not entry.get('caller') or not history.is_file():
        return None
    expected_tool = {'create': 'create_thread', 'reuse': 'send_message_to_thread',
                     'resume': 'send_message_to_thread'}.get(entry['dispatch_action'])
    if not expected_tool:
        return None
    at = entry['claimed_at']
    with sqlite3.connect(history.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5) as c:
        turns = c.execute("""SELECT turn_id FROM thread_turns WHERE thread_id=? AND started_at<=?
            AND (completed_at IS NULL OR completed_at>=?) ORDER BY started_at DESC LIMIT 3""",
            (entry['caller'],at+2,at-2)).fetchall()
        matches = []
        for (turn,) in turns:
            rows = c.execute("""SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=?
                AND item_type='mcpToolCall' AND created_at_ms>=? AND json_extract(item_json,'$.tool')=?""",
                (entry['caller'],turn,int((at-2)*1000),expected_tool))
            for (raw,) in rows:
                call = json.loads(raw)
                args = call.get('arguments') or {}
                if (call.get('server')!='codex_app' or call.get('environmentId')!='local'
                        or entry['session_prompt'] not in (args.get('prompt') or '')):
                    continue
                if expected_tool=='send_message_to_thread' and args.get('threadId')!=entry['responsible_thread_id']:
                    continue
                result = call.get('result') or {}
                answer = None
                if call.get('status')=='completed' and not call.get('error') and not result.get('isError'):
                    for block in result.get('content',[]):
                        if block.get('type')=='text':
                            value = json.loads(block['text'])
                            if isinstance(value,dict) and value.get('threadId'):
                                answer = value
                                break
                matches.append((answer, f"desktop:{turn}/{call['id']}"))
        return matches[0] if len(matches)==1 and matches[0][0] else None


def reconcile_replies(delivery, history=None):
    """Consume saved positive host replies after a coordinator exit; never initiate a host call."""
    from scripts.subtitle_delivery_queue import load_ticket
    db = delivery.db
    history = history or Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'thread_history_1.sqlite'
    with db.connect() as c:
        rows = c.execute("""SELECT h.value FROM subtitle_delivery_jobs j JOIN kv h
            ON h.key=?||j.id||':'||j.revision WHERE j.dispatch_state IN ('committing','unknown')
            AND json_extract(h.value,'$.state')='claimed'""", (PREFIX,)).fetchall()
    results = []
    for row in rows:
        entry = json.loads(row[0])
        reply = Path(entry['ticket_path']).with_suffix('.host-reply.json')
        try:
            if reply.is_file():
                if reply.resolve() != reply or reply.stat().st_size > 65536:
                    raise ValueError('Invalid reply file')
                answer, reference = json.loads(reply.read_text('utf-8-sig')), str(reply)
            else:
                original = native_reply(entry,history)
                if not original:
                    continue
                answer, reference = original
            target = answer['threadId']
            if not isinstance(target,str) or not target.strip() or len(target)>160:
                raise ValueError('Invalid host return')
            ticket_path = Path(entry['ticket_path'])
            if hashlib.sha256(ticket_path.read_bytes()).hexdigest() != entry['ticket_sha256']:
                raise ValueError('Original ticket changed')
            ticket = load_ticket(ticket_path)
            if ticket['dispatch_action'] in ('reuse', 'resume') and target != ticket['thread_id']:
                raise ValueError('Unexpected owner')
            receipt = delivery.record_dispatch_receipt(ticket,'created',thread_id=target,
                host_ref=reference, recorded_by='verified desktop host reply')
            results.append({'job':entry['id'], 'receipt':receipt['receipt_id'],
                            'accepted':receipt['receipt_accepted'], 'advanced':receipt['state_changed'],
                            'reason':receipt['reason']})
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
            results.append({'job':entry['id'],'error':type(error).__name__})
    return results
