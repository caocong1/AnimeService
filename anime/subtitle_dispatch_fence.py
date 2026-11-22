"""Quarantine a stopped, uncertain *resume*. The original receipt stays pending."""
import hashlib
import json
import time
from pathlib import Path


PREFIX = 'subtitle_dispatch_fence:'


def fence_keys(connection, *, capacity_only=False):
    if not capacity_only:
        return {row[0] for row in connection.execute("SELECT key FROM kv WHERE key GLOB ?", (PREFIX+'*',))}
    from .subtitle_host_handoff import PREFIX as HANDOFF
    return {row[0] for row in connection.execute("""SELECT f.key FROM kv f WHERE f.key GLOB ?
        AND NOT EXISTS (SELECT 1 FROM kv h WHERE h.key=?||substr(f.key,?) AND json_extract(h.value,'$.state')='claimed')""",
        (PREFIX+'*', HANDOFF, len(PREFIX)+1))}


def fenced(row, keys):
    return row['dispatch_state'] in ('unknown', 'committing') and PREFIX+row['id']+':'+str(row['revision']) in keys


def check_output(db, sid, episode):
    row = db.one("""SELECT 1 FROM subtitle_delivery_jobs j JOIN kv k
        ON k.key=?||j.id||':'||j.revision WHERE j.show_id=? AND j.episode=?
        AND j.dispatch_state IN ('committing','unknown')""", (PREFIX, sid, episode))
    if row:
        raise ValueError('原恢复回执待核，此集已暂停写入与发布')


def isolate(delivery, identity, revision, observation, observed_at, *, apply=False):
    from .subtitle_manual_continuation import _observe_related_processes

    db = delivery.db
    project = db.path.parent.parent.resolve()
    path = Path(observation).resolve()
    path.relative_to((project/'data').resolve())
    raw = path.read_bytes()
    host = json.loads(raw.decode('utf-8-sig'))
    thread = host['thread']
    turns = host['turns']
    job = delivery.get(identity)
    value = job['payload']
    target = [r['target'] for r in value['assessment']['material_refs'] if r.get('kind')=='completed_media']
    if len(target) != 1 or not target[0].get('path'):
        raise ValueError('Exact fixed production target required')
    turn = value.get('auto_recovery_turn')
    if (not turn or value.get('owner_stopped_writing') is not True or value.get('recovery_class')!='system'
            or value.get('user_stopped') or not job['thread_id'] or thread['id'] != job['thread_id']
            or thread.get('hostId') != 'local' or Path(thread['cwd']).resolve() != project
            or thread['status'].get('type') not in ('idle', 'notLoaded')
            or len(turns) != 1 or turns[0]['id'] != turn
            or turns[0]['status'] not in ('completed','interrupted','failed')
            or not turns[0].get('completedAt')
            or any(i.get('status') in ('inProgress','running') for i in turns[0].get('items', []))):
        raise ValueError('Latest terminal original owner turn required')
    writers = _observe_related_processes(identity, {'path':path}, target[0])
    if writers['related_processes']:
        raise ValueError('Original writer still present')
    key = PREFIX+identity+':'+str(revision)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(identity,)).fetchone()
        now = time.time()
        if not 0 <= now-observed_at <= 300 or not 0 <= now-path.stat().st_mtime <= 300:
            raise ValueError('Fresh native observation required')
        if (not row or row['revision'] != revision or row['input_key'] != job['input_key']
                or row['thread_id'] != thread['id'] or row['dispatch_state'] not in ('committing','unknown')
                or row['lease_expires'] > now or now-row['updated'] < 3600
                or json.loads(row['payload']).get('auto_recovery_turn') != turn):
            raise ValueError('Original pending recovery or lease changed')
        receipt = c.execute("""SELECT receipt_id FROM dispatch_receipts WHERE job_id=? AND expected_revision=?
            AND outcome='interrupted' AND receipt_accepted=1 AND state_changed=0
            AND json_extract(envelope,'$.dispatch_action')='reuse'
            AND json_extract(envelope,'$.ticket_thread_id')=? ORDER BY recorded_at DESC LIMIT 1""",
            (identity,revision,thread['id'])).fetchone()
        if not receipt:
            raise ValueError('Verified interrupted original resume receipt required')
        result = {'job_id':identity,'revision':revision,'input_key':row['input_key'],'thread_id':thread['id'],
                  'turn_id':turn,'checked_at':now,'host_evidence':str(path),
                  'host_sha256':hashlib.sha256(raw).hexdigest(),'receipt_id':receipt[0],'writers':writers}
        if apply:
            # An old CLI must not advance the revision and lose the fence via
            # a legacy unknown callback, metadata refresh or a second begin.
            c.execute("""CREATE TRIGGER IF NOT EXISTS preserve_fenced_subtitle_resume
                BEFORE UPDATE ON subtitle_delivery_jobs WHEN OLD.dispatch_state IN ('unknown','committing')
                AND EXISTS(SELECT 1 FROM kv WHERE key='subtitle_dispatch_fence:'||OLD.id||':'||OLD.revision)
                AND NOT (NEW.dispatch_state='acknowledged' AND NEW.state='production'
                    AND NEW.revision=OLD.revision+1 AND NEW.thread_id=OLD.thread_id
                    AND NEW.id=OLD.id AND NEW.input_key=OLD.input_key)
                BEGIN SELECT RAISE(ABORT,'Pending resume is fenced'); END""")
            c.execute('INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)',(key,json.dumps(result)))
        return {**result,'applied':apply}
