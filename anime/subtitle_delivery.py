"""Durable episode delivery scheduling, without media or subtitle write authority.

This module records preparation, the one responsible chat and dispatch receipts.
It never downloads, freezes a mother, edits a workbook, accepts a build or changes
episodes/watches. An injected *read-only* assessor must verify readiness; the
default is deliberately conservative while actual audio/player capability is
unavailable. Dispatch is an external side effect: unknown outcomes are reconciled,
never retried just because a lease expired.
"""
import datetime
import hashlib
import json
import math
import time
import uuid
from pathlib import Path

from .net import safe_error
from .viewing_workflow import lane, queue_snapshot, scope_blocker, RESEARCH_MODES


STATES = {'preparing', 'ready', 'production', 'blocked', 'accepted', 'finished', 'late-review'}
PROTECTED_DISPATCH = {'committing', 'unknown', 'acknowledged'}
RETAINED_DISPATCH = PROTECTED_DISPATCH | {'blocked-stopped'}
# docs/DISPATCH-RECEIPT-CONTRACT.md: a receipt proves chat creation/absence for one exact dispatch,
# never subtitle completion. v1 is carried by the immutable begin ticket; enforcement stays off
# (kv RECEIPT_REQUIRED_KEY) until callers and their archived tickets have migrated.
TICKET_SCHEMA = 'dispatch-ticket/v1'
RECEIPT_SCHEMA = 'dispatch-receipt/v1'
LEGACY_RECEIPT_SCHEMA = 'dispatch-receipt/legacy'
RECEIPT_OUTCOMES = ('created', 'not_created', 'failed', 'interrupted')
RECEIPT_REQUIRED_KEY = 'dispatch_receipts_required'
EVIDENCE_LIMIT = 16 * 1024          # canonical UTF-8 bytes of bound evidence
EVIDENCE_FUTURE_TOLERANCE = 300     # seconds of clock skew tolerated on checked_at


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _digest(token):
    """Receipts carry digests of authority tokens, never the tokens themselves."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest() if token else ''


def _canonical(value):
    """Canonical receipt envelope: sorted keys, UTF-8, compact separators, None dropped (absent equals
    null), only finite numbers; identical retries hash identically, changed evidence does not."""
    def clean(v):
        if isinstance(v, dict):
            if not all(isinstance(k, str) for k in v):
                raise ValueError('receipt object keys must be strings')
            return {k: clean(x) for k, x in v.items() if x is not None}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError('receipt numbers must be finite')
            return v
        if v is None or isinstance(v, (bool, int, str)):
            return v
        raise ValueError('receipt envelope allows only strings, numbers, booleans, lists and objects')
    return _json(clean(value))


def validate_ticket(ticket):
    """The immutable begin ticket a v1 receipt must carry; nothing is read from the live job to fill gaps."""
    if not isinstance(ticket, dict) or ticket.get('schema') != TICKET_SCHEMA:
        raise ValueError('dispatch ticket required (schema dispatch-ticket/v1)')
    for key in ('job_id', 'dispatch_token', 'lease_token', 'owner', 'input_key', 'dispatch_action', 'thread_id'):
        if not isinstance(ticket.get(key), str):
            raise ValueError(f'dispatch ticket field {key} must be a string')
    revision = ticket.get('revision')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError('dispatch ticket revision must be a positive integer')
    if not (ticket['job_id'] and ticket['dispatch_token'] and ticket['owner'] and ticket['input_key']):
        raise ValueError('dispatch ticket is incomplete')
    if ticket['dispatch_action'] not in ('create', 'reuse') or bool(ticket['thread_id']) != (ticket['dispatch_action'] == 'reuse'):
        raise ValueError('dispatch ticket action and responsible thread disagree')
    issued = ticket.get('issued_at')
    if isinstance(issued, bool) or not isinstance(issued, (int, float)) or not math.isfinite(issued):
        raise ValueError('dispatch ticket issued_at must be a number')
    return ticket


def validate_absence_evidence(evidence, job_id, dispatch_token, issued_at, now):
    """Shared boundary for verified-absence evidence: who checked which chat list, when and how, bound to
    one dispatch. The file name and sha256 come from the tool that read the file, not from the file."""
    if not isinstance(evidence, dict):
        raise ValueError('absence evidence must be a JSON object')
    if not all(isinstance(evidence.get(k), str) and evidence[k].strip() for k in ('checked_at', 'method', 'checked_by')):
        raise ValueError('absence evidence needs checked_at, method and checked_by')
    if not (isinstance(evidence.get('sha256'), str) and len(evidence['sha256']) == 64
            and all(ch in '0123456789abcdef' for ch in evidence['sha256'])
            and isinstance(evidence.get('file'), str) and evidence['file'].strip()):
        raise ValueError('absence evidence needs the tool-computed file name and sha256')
    if evidence.get('job_id') != job_id:
        raise ValueError('absence evidence must identify this job_id')
    digest = _digest(dispatch_token)
    if evidence.get('dispatch_token') != dispatch_token and evidence.get('dispatch_digest') != digest:
        raise ValueError('absence evidence must identify this dispatch')
    try:
        checked = datetime.datetime.fromisoformat(evidence['checked_at'])
        if checked.tzinfo is None:
            raise ValueError()
        stamp = checked.timestamp()
    except (TypeError, ValueError):
        raise ValueError('absence evidence checked_at must be timezone-aware ISO 8601') from None
    if stamp < issued_at:
        raise ValueError('absence evidence checked_at precedes the dispatch it claims to verify')
    if stamp > now + EVIDENCE_FUTURE_TOLERANCE:
        raise ValueError('absence evidence checked_at lies in the future')
    bound = {k: v for k, v in evidence.items() if k != 'dispatch_token'}
    bound['dispatch_digest'] = digest
    if len(_canonical(bound).encode('utf-8')) > EVIDENCE_LIMIT:
        raise ValueError('absence evidence exceeds the receipt size limit')
    return bound


def delivery_policy(db):
    """The owner-maintained `subtitle_delivery_policy` record; missing keys mean the historical behaviour."""
    return db.get('subtitle_delivery_policy', {}) or {}


def last_served(db):
    """When each show last had a release observed, from the scheduler's own ledger."""
    if not _exists(db, 'subtitle_delivery_events'):
        return {}
    return {row['show_id']: row['served'] for row in db.rows('''SELECT j.show_id show_id, MAX(e.recorded) served
        FROM subtitle_delivery_events e JOIN subtitle_delivery_jobs j ON j.id=e.job_id
        WHERE e.kind='finished-observed' GROUP BY j.show_id''')}


def _exists(db, table):
    return bool(db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def _finished(db, sid, ep):
    row = db.one('SELECT finished FROM watches WHERE show_id=? AND episode=?', (sid, ep))
    return bool(row and row['finished'])


def _delivered(db, sid, ep):
    # This host ledger is separate from both watched and formally accepted.
    value = db.get(f'subtitle_delivery_finished_{sid}_{ep}', {}) or {}
    return bool(value.get('delivery_status') == 'finished' and value.get('release_id')
                and value.get('manifest_sha256') and value.get('accepted') is False)


def _scope(db, sid, ep):
    if lane(db, sid) != 'watching':
        return 'Only explicitly selected watching episodes may enter delivery production'
    return scope_blocker(db, sid, ep)


def _scope_transaction(c, sid, ep):
    """Recheck permission in the same short write transaction as the claim."""
    show = c.execute('SELECT * FROM shows WHERE id=?', (sid,)).fetchone()
    if not show or not show['selected'] or show['cleanup_hold'] or show['state'] != 'watching':
        return 'Delivery personal scope changed'
    mapping = json.loads(show['mapping'])
    if (not show['authorized'] or not mapping.get('confirmed') or not mapping.get('inventory_checked')
            or not mapping.get('start', 1) <= 1 <= mapping.get('end', show['total'] or 999)
            or not mapping.get('start', 1) <= ep <= mapping.get('end', show['total'] or 999)):
        return 'Delivery authorization or episode bounds changed'
    policy = c.execute('SELECT config FROM source_policies WHERE show_id=?', (sid,)).fetchone()
    if not policy or json.loads(policy['config']).get('mode') not in RESEARCH_MODES:
        return 'Delivery research policy changed'
    return ''


def conservative_readiness(db, sid, ep, payload):
    """Inspect existing inputs without constructing an Archive/Engine or polling qB.

    Raw/MKV labels and existing workbooks do not establish cleanliness or the
    ability to hear audio. Completion reconciliation/archival remains the host's
    responsibility. The richer assessor is a trusted read-only host adapter,
    rather than an HTTP client assertion that something is ready.
    """
    payload = payload or {}
    reasons = []
    target = payload.get('target') or {}
    book = Path(payload['workbook']) if payload.get('workbook') else None
    book_sha = None
    if book and book.is_file():
        book_sha = hashlib.sha256(book.read_bytes()).hexdigest()
    else:
        reasons.append('needs_subtitle_inventory_or_archive')
    if not target:
        retained = []
        for task in db.rows("SELECT hash,selection FROM tasks WHERE show_id=? AND status IN ('complete','linked')", (sid,)):
            try:
                if str(ep) in json.loads(task['selection']).get('selected', {}):
                    retained.append(task['hash'])
            except (TypeError, ValueError):
                continue
        reasons.append('completed_media_needs_inventory_or_reconcile' if retained else 'needs_verified_completed_media')
    else:
        reasons.append('needs_actual_clean_mother_review')
    reasons.append('needs_verified_original_audio_and_continuous_player_capability')
    return {
        'material_ready': False, 'capability_ready': False, 'clean_review_ready': False,
        'reasons': reasons, 'target_fingerprint': target.get('fingerprint'),
        'input_signature': _hash({'target': target, 'draft_ids': sorted(payload.get('draft_ids') or [])}),
        'book_sha256': book_sha,
        'material_refs': [], 'capability_refs': [], 'accepted_verified': False,
    }


def default_readiness(db, sid, ep, payload):
    try:
        from .subtitle_delivery_materials import readiness
    except ImportError:
        return conservative_readiness(db, sid, ep, payload)
    return readiness(db, sid, ep, payload)


class SubtitleDelivery:
    def __init__(self, db, readiness=None, clock=None, initialize=True, absence_verifier=None):
        self.db = db
        self.readiness = readiness or default_readiness
        self.clock = clock or time.time
        # Trusted, local, read-only host adapter; never supplied by receipt JSON.
        # No production adapter is enabled until terminal host evidence is available.
        self.absence_verifier = absence_verifier
        if initialize:
            self.initialize()

    def initialize(self):
        """Create only the scheduler's additive tables, no production input changes."""
        with self.db.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS subtitle_delivery_jobs(
              id TEXT PRIMARY KEY, show_id INTEGER NOT NULL, episode INTEGER NOT NULL,
              kind TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              input_key TEXT NOT NULL, payload TEXT NOT NULL, accepted_build_id TEXT,
              lease_owner TEXT NOT NULL DEFAULT '', lease_token TEXT NOT NULL DEFAULT '',
              lease_expires REAL NOT NULL DEFAULT 0, dispatch_token TEXT NOT NULL DEFAULT '',
              dispatch_state TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '',
              created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS subtitle_delivery_episode
              ON subtitle_delivery_jobs(show_id,episode,created);
            CREATE TABLE IF NOT EXISTS subtitle_delivery_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              kind TEXT NOT NULL, payload TEXT NOT NULL, recorded REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS subtitle_delivery_late_inputs(
              show_id INTEGER NOT NULL, episode INTEGER NOT NULL, baseline_build_id TEXT NOT NULL,
              source_hash TEXT NOT NULL, text_hash TEXT NOT NULL, job_id TEXT,
              disposition TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL,
              PRIMARY KEY(show_id,episode,baseline_build_id,source_hash));
            CREATE TABLE IF NOT EXISTS dispatch_receipts(
              receipt_id TEXT PRIMARY KEY, schema TEXT NOT NULL, job_id TEXT NOT NULL,
              expected_revision INTEGER NOT NULL, request_digest TEXT NOT NULL, envelope TEXT NOT NULL,
              outcome TEXT NOT NULL, receipt_accepted INTEGER NOT NULL, state_changed INTEGER NOT NULL,
              reason TEXT NOT NULL DEFAULT '', resulting_state TEXT NOT NULL DEFAULT '',
              resulting_dispatch_state TEXT NOT NULL DEFAULT '', resulting_revision INTEGER,
              result TEXT NOT NULL, recorded_at REAL NOT NULL, recorded_by TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS dispatch_receipts_job ON dispatch_receipts(job_id,recorded_at);
            ''')

    @staticmethod
    def episode_id(sid, ep):
        return f'episode:{sid}:{ep}'

    def _event(self, c, identity, kind, payload):
        c.execute('INSERT INTO subtitle_delivery_events(job_id,kind,payload,recorded) VALUES(?,?,?,?)',
                  (identity, kind, _json(payload), self.clock()))

    def _assessment(self, sid, ep, payload):
        result = dict(self.readiness(self.db, sid, ep, payload) or {})
        result['reasons'] = list(result.get('reasons') or [])
        # Four-stage acceptance is intentionally NOT a dispatch prerequisite.
        # Actual material/cleanliness facts and usable QA capability are.
        result['ready'] = all(result.get(key) is True for key in
                              ('material_ready', 'clean_review_ready', 'capability_ready'))
        if result['ready'] and (not result.get('input_signature') or not result.get('target_fingerprint')
                                or not result.get('material_refs') or not result.get('capability_refs')):
            result['ready'] = False
            result['reasons'].append('readiness_proof_or_exact_input_missing')
        result['input_signature'] = result.get('input_signature') or _hash(payload or {})
        return result

    def _job(self, identity):
        row = self.db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,))
        if not row:
            raise ValueError('Unknown subtitle delivery job')
        return {**row, 'payload': json.loads(row['payload'])}

    def get(self, identity):
        return self._job(identity)

    def has_active(self, sid, ep):
        if not _exists(self.db, 'subtitle_delivery_jobs'):
            return False
        return bool(self.db.one('''SELECT id FROM subtitle_delivery_jobs
            WHERE show_id=? AND episode=? AND
            (dispatch_state IN ('committing','unknown','acknowledged') OR lease_expires>?) LIMIT 1''',
                                (sid, ep, self.clock())))

    def _dispatch_assessment(self, job):
        assessment = self._assessment(job['show_id'], job['episode'], self._source_payload(job))
        if job['kind'] == 'refinement':
            observation = self.db.get(f"subtitle_delivery_finished_{job['show_id']}_{job['episode']}", {}) or {}
            if (observation.get('release_id') != job['payload']['baseline_release_id']
                    or assessment.get('target_fingerprint') != job['payload'].get('baseline_target_fingerprint', job['payload']['assessment'].get('target_fingerprint'))):
                assessment['ready'] = False
                assessment['reasons'].append('refinement_baseline_or_target_changed')
            assessment['input_signature'] = job['input_key']
        if job['kind'] == 'late':
            # The accepted baseline and explicit immutable difference bundle are
            # the V2 input; ordinary editorial book changes are not new dispatches.
            if assessment.get('target_fingerprint') != job['payload']['assessment'].get('target_fingerprint'):
                assessment['ready'] = False
                assessment['reasons'].append('late_review_target_changed')
            assessment['input_signature'] = _hash({'baseline': job['payload']['baseline_build_id'],
                                                 'late_sources': job['payload']['late_sources']})
        return assessment

    def refresh(self, snapshot=None):
        """Prepare durable jobs from current gates; never dispatch or perform acquisition."""
        from .subtitle_enrollment import reconcile
        enrollment = reconcile(self.db)
        snapshot = snapshot or queue_snapshot(self.db)
        changes = 0
        present = set()
        late_candidates = []
        for show in snapshot['queues']['watching']:
            jobs = {row['episode']: row for row in show.get('jobs', [])}
            if show.get('next_unwatched'):
                jobs.setdefault(show['next_unwatched'], {'episode': show['next_unwatched']})
            for ep, studio_job in sorted(jobs.items()):
                identity = self.episode_id(show['id'], ep)
                present.add(identity)
                error_key = 'subtitle_delivery_refresh_error_' + identity
                try:
                    source = self.db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?', (show['id'], ep))
                    source = json.loads(source['payload']) if source else dict(studio_job)
                    assessment = self._assessment(show['id'], ep, source)
                    blocker = _scope(self.db, show['id'], ep)
                    value = {'assessment': assessment, 'finished': _finished(self.db, show['id'], ep),
                             'next_unwatched': show.get('next_unwatched'), 'scope_blocker': blocker}
                    key = assessment['input_signature']
                    state = ('blocked' if blocker else 'preparing' if not
                             (assessment.get('material_ready') is True and assessment.get('clean_review_ready') is True)
                             else 'ready' if assessment['ready'] else 'blocked')
                    with self.db.connect() as c:
                        c.execute('BEGIN IMMEDIATE')
                        old = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
                        if old:
                            prior = json.loads(old['payload'])
                            if old['dispatch_state'] in RETAINED_DISPATCH or old['lease_expires'] > self.clock():
                                # In-flight work keeps its original input and responsible chat.
                                value = {**prior, 'latest_assessment': value,
                                         'scope_blocker': blocker, 'finished': value['finished']}
                                key, state = old['input_key'], old['state']
                            elif old['state'] in ('accepted', 'finished'):
                                baseline_refs = prior.get('accepted_observation', {}).get('material_refs') or []
                                baseline_raw = {ref.get('raw_hash') for ref in baseline_refs
                                                if isinstance(ref, dict) and ref.get('raw_hash')}
                                if not blocker and old['state'] == 'accepted':
                                    for ref in assessment.get('material_refs') or []:
                                        if (isinstance(ref, dict) and ref.get('kind') == 'archived_draft'
                                                and ref.get('raw_hash') and ref.get('text_hash')
                                                and ref['raw_hash'] not in baseline_raw):
                                            late_candidates.append((show['id'], ep, ref))
                                value = {**prior, 'latest_assessment': value,
                                         'scope_blocker': blocker, 'finished': value['finished']}
                                key, state = old['input_key'], old['state']
                            observational = (old['dispatch_state'] in RETAINED_DISPATCH or old['lease_expires'] > self.clock()) and (
                                assessment['input_signature'] == old['input_key']
                                and prior.get('scope_blocker') == blocker
                                and prior.get('finished') == value['finished'])
                            if old['payload'] == _json(value) and old['state'] == state:
                                c.execute('DELETE FROM kv WHERE key=?', (error_key,))
                                continue
                            if observational:
                                # Latest diagnostic evidence is not a new dispatch identity.
                                # Keep the immutable ticket revision and activity timestamp;
                                # actual input/scope/watched changes still take the CAS path.
                                c.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?', (_json(value), identity))
                                c.execute('DELETE FROM kv WHERE key=?', (error_key,))
                                self._event(c, identity, 'readiness-observed', {'state': state, 'input_key': key})
                                changes += 1
                                continue
                            c.execute('UPDATE subtitle_delivery_jobs SET state=?,revision=revision+1,input_key=?,payload=?,updated=? WHERE id=?',
                                      (state, key, _json(value), self.clock(), identity))
                        else:
                            c.execute('INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,created,updated) VALUES(?,?,?,\'episode\',?,?,?,?,?)',
                                      (identity, show['id'], ep, state, key, _json(value), self.clock(), self.clock()))
                        self._event(c, identity, 'readiness', {'state': state, 'input_key': key})
                        changes += 1
                    if self.db.get(error_key) is not None:
                        self.db.execute('DELETE FROM kv WHERE key=?', (error_key,))
                except Exception as error:  # one episode's assessment must not stop the others
                    self.db.set(error_key, {'time': self.clock(), 'error': safe_error(error)})
        # Scope changes only block future claims; unknown/acknowledged dispatch stays protected.
        for row in self.db.rows("SELECT id,show_id,episode,state,dispatch_state FROM subtitle_delivery_jobs WHERE kind='episode'"):
            blocker = _scope(self.db, row['show_id'], row['episode'])
            if row['id'] not in present and blocker and row['state'] not in ('accepted', 'finished'):
                with self.db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    old = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (row['id'],)).fetchone()
                    value = json.loads(old['payload']); value['scope_blocker'] = blocker
                    state = old['state'] if old['dispatch_state'] in RETAINED_DISPATCH else 'blocked'
                    if old['payload'] != _json(value) or state != old['state']:
                        c.execute('UPDATE subtitle_delivery_jobs SET state=?,payload=?,revision=revision+1,updated=? WHERE id=?',
                                  (state, _json(value), self.clock(), row['id']))
                        changes += 1
        for sid, ep, ref in late_candidates:
            self.enqueue_late_review(sid, ep, ref['raw_hash'], ref['text_hash'], ref)
        from .subtitle_refinement import two_pass, enqueue_refinement
        if two_pass(self.db):
            for row in self.db.rows("SELECT id FROM subtitle_delivery_jobs WHERE kind='episode' AND state!='accepted' AND dispatch_state IN ('completed','blocked-stopped')"):
                enqueue_refinement(self, row['id'])
        return {'changed': changes, 'enrollment': enrollment, **self.snapshot()}

    def record_preparation(self, sid, ep):
        """Assess one current episode without creating or claiming a chat."""
        current = queue_snapshot(self.db)
        rows = [show for show in current['queues']['watching'] if show['id'] == sid]
        if not rows or _scope(self.db, sid, ep):
            raise ValueError('Preparation is outside current watching authorization')
        row = dict(rows[0])
        row['jobs'] = [job for job in row.get('jobs', []) if job['episode'] == ep]
        if not row['jobs']:
            row['jobs'] = [{'episode': ep}]
        row['next_unwatched'] = ep if row.get('next_unwatched') == ep else None
        self.refresh({'queues': {'watching': [row]}})
        return self.get(self.episode_id(sid, ep))

    def snapshot(self):
        """Read-only queue; an expired lease never erases dispatch uncertainty."""
        # The live Store otherwise opens a new connection for every lookup.
        # Keep the backlog and job facts consistent, without running migrations.
        if not hasattr(self.db, 'con'):
            from .read_projection import read_projection
            with read_projection(self.db.path) as db:
                return SubtitleDelivery(db, clock=self.clock, initialize=False).snapshot()
        if not _exists(self.db, 'subtitle_delivery_jobs'):
            return {'jobs': [], 'summary': {state: 0 for state in sorted(STATES)}}
        policy = delivery_policy(self.db)
        lane = {x for x in policy.get('priority_show_ids') or [] if isinstance(x, int)}
        jobs = []
        for row in self.db.rows('SELECT * FROM subtitle_delivery_jobs ORDER BY created,id'):
            value = {**row, 'payload': json.loads(row['payload'])}
            value['finished'] = _finished(self.db, row['show_id'], row['episode'])
            value['delivery_finished'] = _delivered(self.db, row['show_id'], row['episode'])
            # A long-running pre-delivery-first host can refresh the legacy job
            # to ready after the owner has recorded its finished receipt. The
            # durable verified ledger remains authoritative without disturbing
            # a committing/unknown/active dispatch or the stopped Maou audit.
            if (row['kind'] == 'episode' and value['delivery_finished']
                    and row['dispatch_state'] == 'completed' and row['state'] != 'accepted'):
                value['state'] = 'finished'
            value['scope_blocker'] = _scope(self.db, row['show_id'], row['episode'])
            # Public control-plane reads expose ownership, not lease/dispatch tokens.
            value.pop('lease_token'); value.pop('dispatch_token')
            value['priority'] = 100 if value['finished'] or value['delivery_finished'] else 0
            if row['kind'] == 'late':
                value['priority'] += 50
            if (row['kind'] == 'refinement' or value['payload'].get('refinement_pending')) and not value['finished']:
                # Opt-in: first versions of other shows come before any v1 polish.
                value['priority'] = 10 if policy.get('v0_first_across_shows') is True else -10
            if row['show_id'] in lane and value['priority'] < 100:
                value['priority'] = -20  # the viewer's priority lane: this show's unfinished work goes before everything
            jobs.append(value)
        served = last_served(self.db) if policy.get('queue_order') == 'longest_unserved' else {}
        # Opt-in fairness: among first versions, the show that waited longest since its last release goes first.
        jobs.sort(key=lambda job: (job['priority'], served.get(job['show_id'], 0) if job['priority'] == 0 else 0,
                                   job['episode'], job['created'], job['id']))
        from .subtitle_enrollment import coverage
        from .subtitle_preparation import preparation_plan
        return {'jobs': jobs, 'summary': {state: sum(job['state'] == state for job in jobs) for state in sorted(STATES)},
                'preparation': preparation_plan(self.db, jobs),
                'enrollment_coverage': coverage(self.db),
                'preparation_features': {'research_modes': list(RESEARCH_MODES), 'all_available_unwatched': True,
                                         'watched_acquisition_guard': True, 'watching_auto_enrollment': True}}

    def reserve(self, identity, owner, expected_revision, lease_seconds=900, allow_experience=False,
                resume_thread=None):
        """Reserve one ready episode globally; safe undispatched leases may be reclaimed."""
        if not isinstance(owner, str) or not owner.strip() or not 30 <= lease_seconds <= 86400:
            raise ValueError('Invalid delivery owner or lease duration')
        job = self._job(identity)
        from .subtitle_refinement import two_pass
        continuing_v1 = (two_pass(self.db) and job['payload'].get('refinement_pending')
                         and resume_thread and resume_thread == job['thread_id'])
        if job['kind'] == 'episode' and _delivered(self.db, job['show_id'], job['episode']) and not continuing_v1:
            raise ValueError('A finished viewing release already exists; preserve it for incremental revision')
        blocker = _scope(self.db, job['show_id'], job['episode'])
        if blocker:
            raise ValueError(blocker)
        if _finished(self.db, job['show_id'], job['episode']) and not allow_experience:
            raise ValueError('Finished episodes are retained experience samples, not first-delivery priority')
        from .subtitle_refinement import two_pass
        if _finished(self.db, job['show_id'], job['episode']) and two_pass(self.db):
            raise ValueError('Watched episodes are frozen under the v0/v1 policy')
        assessment = self._dispatch_assessment(job)
        if not assessment['ready'] or assessment['input_signature'] != job['input_key']:
            raise ValueError('Readiness changed; refresh current inputs before dispatch')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if _scope_transaction(c, row['show_id'], row['episode']):
                raise ValueError('Delivery scope changed during claim')
            watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?',
                                (row['show_id'], row['episode'])).fetchone()
            if watched and watched['finished'] and (not allow_experience or two_pass(self.db)):
                raise ValueError('Finished episodes are retained experience samples, not first-delivery priority')
            if row['kind'] == 'late':
                primary = c.execute('SELECT accepted_build_id FROM subtitle_delivery_jobs WHERE id=?',
                                    (self.episode_id(row['show_id'], row['episode']),)).fetchone()
                if not primary or primary['accepted_build_id'] != json.loads(row['payload'])['baseline_build_id']:
                    raise ValueError('Late review baseline changed; reconcile before dispatch')
            if row['revision'] != expected_revision:
                raise ValueError('Delivery revision conflict')
            resuming = (resume_thread and row['thread_id'] == resume_thread
                        and row['dispatch_state'] == 'blocked-stopped')
            if row['dispatch_state'] in PROTECTED_DISPATCH or row['state'] in ('accepted', 'finished'):
                raise ValueError('Existing dispatch/receipt must be reconciled, never dispatched again')
            if row['dispatch_state'] == 'blocked-stopped' and not resuming:
                raise ValueError('Stopped episode must resume its existing responsible chat')
            occupied = c.execute('''SELECT id FROM subtitle_delivery_jobs WHERE id!=? AND
                (dispatch_state IN ('committing','unknown','acknowledged') OR lease_expires>?) LIMIT 1''',
                (identity, self.clock())).fetchone()
            if occupied:
                raise ValueError('Another episode already has the single production owner')
            if row['lease_expires'] > self.clock():
                if row['lease_owner'] != owner:
                    raise ValueError('Episode already leased')
                return {**dict(row), 'job_id': identity, 'payload': json.loads(row['payload'])}
            if row['state'] not in ('ready', 'late-review', 'production') and not resuming:
                raise ValueError('Episode is not dispatchable')
            token = uuid.uuid4().hex
            value = json.loads(row['payload']); value['experience_only'] = allow_experience is True
            value['owner_stopped_writing'] = False
            c.execute('UPDATE subtitle_delivery_jobs SET state=\'production\',lease_owner=?,lease_token=?,lease_expires=?,payload=?,revision=revision+1,updated=? WHERE id=?',
                      (owner, token, self.clock() + lease_seconds, _json(value), self.clock(), identity))
            self._event(c, identity, 'reserved', {'owner': owner, 'input_key': row['input_key']})
            record = dict(c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone())
            return {**record, 'job_id': identity, 'payload': json.loads(record['payload'])}

    def resume(self, identity, thread_id, owner, expected_revision, lease_seconds=900,
               allow_experience=False):
        return self.reserve(identity, owner, expected_revision, lease_seconds,
                            allow_experience=allow_experience, resume_thread=thread_id)

    def release(self, identity, owner, expected_revision, lease_token=None, reason='released'):
        """Give back a lease that never reached dispatch; a started dispatch must be reconciled instead."""
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if (not row or row['revision'] != expected_revision or row['lease_owner'] != owner
                    or (lease_token is not None and row['lease_token'] != lease_token) or row['lease_expires'] <= self.clock()):
                raise ValueError('No live lease for this owner and revision')
            if row['dispatch_state'] in PROTECTED_DISPATCH:
                raise ValueError('Dispatch already started; reconcile its result instead of releasing the lease')
            state = row['state']
            if row['dispatch_state'] != 'blocked-stopped':
                state = 'late-review' if row['kind'] == 'late' else 'ready'
            c.execute('UPDATE subtitle_delivery_jobs SET state=?,lease_owner=\'\',lease_token=\'\',lease_expires=0,revision=revision+1,updated=? WHERE id=?',
                      (state, self.clock(), identity))
            self._event(c, identity, 'lease-released', {'owner': owner, 'reason': reason})
            record = dict(c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone())
            return {**record, 'job_id': identity, 'payload': json.loads(record['payload'])}

    def _source_payload(self, job):
        source = self.db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?',
                             (job['show_id'], job['episode']))
        return json.loads(source['payload']) if source else {}

    def begin_dispatch(self, identity, owner, lease_token, expected_revision):
        """Persist intent BEFORE creating a chat; do not call the external tool here."""
        job = self._job(identity)
        if (job['kind'] == 'episode' and _delivered(self.db, job['show_id'], job['episode'])
                and not job['payload'].get('refinement_pending')):
            raise ValueError('Episode was already delivered; do not dispatch another first release')
        if _scope(self.db, job['show_id'], job['episode']):
            raise ValueError('Delivery scope changed')
        assessment = self._dispatch_assessment(job)
        if not assessment['ready'] or assessment['input_signature'] != job['input_key']:
            raise ValueError('Exact ready inputs changed')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if _scope_transaction(c, row['show_id'], row['episode']):
                raise ValueError('Delivery scope changed during dispatch')
            watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?',
                                (row['show_id'], row['episode'])).fetchone()
            from .subtitle_refinement import two_pass
            if watched and watched['finished'] and (not json.loads(row['payload']).get('experience_only') or two_pass(self.db)):
                raise ValueError('Episode became finished before first delivery; refresh its priority')
            if (row['revision'] != expected_revision or row['lease_owner'] != owner
                    or row['lease_token'] != lease_token or row['lease_expires'] <= self.clock()):
                raise ValueError('Delivery lease/revision expired or changed')
            if row['dispatch_state'] in PROTECTED_DISPATCH:
                raise ValueError('Dispatch already started; reconcile its result')
            token = uuid.uuid4().hex
            c.execute('UPDATE subtitle_delivery_jobs SET dispatch_token=?,dispatch_state=\'committing\',revision=revision+1,updated=? WHERE id=?',
                      (token, self.clock(), identity))
            self._event(c, identity, 'dispatch-started', {'input_key': row['input_key']})
            record = dict(c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone())
            # The immutable ticket a v1 receipt must present; the CLI archives it privately with its tokens.
            ticket = {'schema': TICKET_SCHEMA, 'job_id': identity, 'revision': record['revision'], 'dispatch_token': token,
                      'lease_token': row['lease_token'], 'owner': owner, 'input_key': row['input_key'],
                      'dispatch_action': 'reuse' if row['thread_id'] else 'create', 'thread_id': row['thread_id'] or '',
                      'issued_at': self.clock()}
            self._event(c, identity, 'dispatch-ticket-issued', {'ticket_digest': _digest(_canonical(ticket))})
            return {**record, 'job_id': identity, 'payload': json.loads(record['payload']),
                    'assessment': json.loads(row['payload'])['assessment'],
                    'action': 'reuse-thread' if row['thread_id'] else 'create-thread', 'ticket': ticket}

    def reconcile_dispatch(self, identity, dispatch_token, thread_id=None, definitely_not_created=False,
                           absence_evidence=None):
        """Legacy receipt path (no begin ticket); lost responses remain non-retryable."""
        if thread_id and definitely_not_created:
            raise ValueError('Contradictory dispatch receipt')
        if definitely_not_created and not absence_evidence:
            raise ValueError('Verified absence evidence required before releasing unknown dispatch')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            required = c.execute('SELECT value FROM kv WHERE key=?', (RECEIPT_REQUIRED_KEY,)).fetchone()
            if required and json.loads(required['value']):
                raise ValueError('dispatch-receipt/v1 ticket required; the legacy reconcile path is disabled')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if not row or not dispatch_token or row['dispatch_token'] != dispatch_token:
                raise ValueError('Dispatch token mismatch')
            if row['thread_id']:
                if thread_id == row['thread_id'] and row['dispatch_state'] not in ('committing', 'unknown'):
                    return {**dict(row), 'payload': json.loads(row['payload']), 'duplicate': True}
                if thread_id and thread_id != row['thread_id']:
                    raise ValueError('A responsible chat already exists; preserve its receipt')
            if thread_id:
                if not isinstance(thread_id, str) or not thread_id.strip():
                    raise ValueError('Invalid chat receipt')
                state, status = 'production', 'acknowledged'
            elif definitely_not_created:
                if row['thread_id'] or row['dispatch_state'] not in ('committing', 'unknown'):
                    raise ValueError('A responsible chat or resolved dispatch must be preserved')
                # Caller must have independently established no chat was created.
                state = 'late-review' if row['kind'] == 'late' else 'ready'
                status = 'not-created'
            else:
                state, status = 'blocked', 'unknown'
            c.execute('UPDATE subtitle_delivery_jobs SET state=?,dispatch_state=?,thread_id=?,lease_expires=0,revision=revision+1,updated=? WHERE id=?',
                      (state, status, thread_id or row['thread_id'], self.clock(), identity))
            self._event(c, identity, 'dispatch-reconciled', {'result': status, 'thread_id': thread_id,
                                                              **({'absence_evidence': absence_evidence} if definitely_not_created else {})})
            record = dict(c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone())
            self._legacy_receipt(c, identity, row, status, thread_id, record)
            return {**record, 'payload': json.loads(record['payload'])}

    def _legacy_receipt(self, c, identity, before, status, thread_id, after):
        """Provenance row for the legacy path so readers see one ledger; it validates nothing beyond reconcile."""
        outcome = {'acknowledged': 'created', 'not-created': 'not_created'}.get(status, 'unknown')
        envelope = {'schema': LEGACY_RECEIPT_SCHEMA, 'job_id': identity, 'expected_revision': before['revision'],
                    'dispatch_digest': _digest(before['dispatch_token']), 'thread_id': thread_id or '', 'outcome': outcome,
                    'recorded_at': self.clock()}
        canonical = _canonical(envelope)
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        c.execute('INSERT OR IGNORE INTO dispatch_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (digest, LEGACY_RECEIPT_SCHEMA, identity, before['revision'], digest, canonical, outcome, 1, 1, 'legacy',
                   after['state'], after['dispatch_state'], after['revision'], _json({'legacy': True}), self.clock(), 'reconcile_dispatch'))

    def record_dispatch_receipt(self, ticket, outcome, thread_id=None, evidence=None, host_ref=None,
                                recorded_by='', receipt_id=None):
        """dispatch-receipt/v1: one durable, idempotent receipt bound to the begin ticket.

        created/not_created drive the existing dispatch reconciliation only; failed/interrupted are
        diagnostics. Nothing here releases a lease, retries, moves ownership or marks delivery finished.
        Binding failures leave the receipt unaccepted; a true fact that cannot be applied is kept with
        receipt_accepted=1 and state_changed=0."""
        validate_ticket(ticket)
        if outcome not in RECEIPT_OUTCOMES:
            raise ValueError('receipt outcome must be created, not_created, failed or interrupted')
        if outcome == 'created':
            if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 200:
                raise ValueError('a created receipt must name the confirmed chat thread')
            if ticket['dispatch_action'] == 'reuse' and thread_id != ticket['thread_id']:
                raise ValueError('a reuse ticket can only confirm its own responsible thread')
        elif thread_id:
            raise ValueError('only a created receipt names a thread')
        if outcome == 'not_created':
            evidence = validate_absence_evidence(evidence, ticket['job_id'], ticket['dispatch_token'], ticket['issued_at'], self.clock())
        elif evidence is not None and not isinstance(evidence, dict):
            raise ValueError('receipt evidence must be a JSON object')
        evidence_json = _canonical(evidence or {})
        if any(token and token in evidence_json for token in (ticket['dispatch_token'], ticket['lease_token'])):
            raise ValueError('receipt evidence must not contain raw authority tokens')
        for name, value in (('host_ref', host_ref), ('recorded_by', recorded_by)):
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError(f'{name} must be a short string')
        envelope = {'schema': RECEIPT_SCHEMA, 'job_id': ticket['job_id'], 'expected_revision': ticket['revision'],
                    'ticket_digest': _digest(_canonical(ticket)),
                    'dispatch_digest': _digest(ticket['dispatch_token']), 'lease_digest': _digest(ticket['lease_token']),
                    'owner': ticket['owner'], 'input_key': ticket['input_key'], 'dispatch_action': ticket['dispatch_action'],
                    'ticket_thread_id': ticket['thread_id'], 'thread_id': thread_id or '', 'outcome': outcome,
                    'evidence': evidence or {}, 'host_ref': host_ref or ''}
        canonical = _canonical(envelope)
        if len(canonical.encode('utf-8')) > 2 * EVIDENCE_LIMIT:
            raise ValueError('receipt envelope exceeds the size limit')
        request_digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        if receipt_id is None:
            receipt_id = request_digest
        elif not (isinstance(receipt_id, str) and len(receipt_id) == 64 and all(ch in '0123456789abcdef' for ch in receipt_id)):
            raise ValueError('receipt_id must be a 64-hex digest')
        reused = self.db.one('SELECT request_digest FROM dispatch_receipts WHERE receipt_id=?', (receipt_id,))
        if reused and reused['request_digest'] != request_digest:
            with self.db.connect() as c:
                self._event(c, ticket['job_id'], 'dispatch-receipt-rejected', {'receipt_id': receipt_id, 'reason': 'id_reused'})
            raise ValueError('receipt id reused with different content')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT request_digest,result FROM dispatch_receipts WHERE receipt_id=?', (receipt_id,)).fetchone()
            if existing:  # idempotent replay comes before any comparison with the job's current revision
                if existing['request_digest'] != request_digest:
                    self._event(c, ticket['job_id'], 'dispatch-receipt-rejected', {'receipt_id': receipt_id, 'reason': 'id_reused'})
                    c.commit()  # preserve the rejection even when another writer won after the precheck
                    raise ValueError('receipt id reused with different content')
                return {**json.loads(existing['result']), 'replayed': True}
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (ticket['job_id'],)).fetchone()
            if not row:
                raise ValueError('Unknown subtitle delivery job')
            reason, accepted = self._receipt_binding(c, row, ticket, envelope)
            changed = 0
            if accepted and not reason and outcome in ('created', 'not_created'):
                if outcome == 'created':
                    state, status = 'production', 'acknowledged'
                else:
                    state, status = ('late-review' if row['kind'] == 'late' else 'ready'), 'not-created'
                c.execute('UPDATE subtitle_delivery_jobs SET state=?,dispatch_state=?,thread_id=?,lease_expires=0,revision=revision+1,updated=? WHERE id=?',
                          (state, status, thread_id or row['thread_id'], self.clock(), ticket['job_id']))
                self._event(c, ticket['job_id'], 'dispatch-reconciled', {'result': status, 'thread_id': thread_id, 'receipt_id': receipt_id,
                                                                         **({'absence_evidence': evidence} if outcome == 'not_created' else {})})
                changed = 1
            else:
                self._event(c, ticket['job_id'], 'dispatch-receipt', {'outcome': outcome, 'receipt_id': receipt_id,
                                                                      'receipt_accepted': accepted, 'reason': reason})
            after = dict(c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (ticket['job_id'],)).fetchone())
            job = {k: v for k, v in after.items() if k not in ('lease_token', 'dispatch_token')}
            job['payload'] = json.loads(job['payload'])
            result = {'receipt_id': receipt_id, 'schema': RECEIPT_SCHEMA, 'outcome': outcome, 'receipt_accepted': accepted,
                      'state_changed': changed, 'reason': reason, 'resulting_state': after['state'],
                      'resulting_dispatch_state': after['dispatch_state'], 'resulting_revision': after['revision'], 'job': job}
            c.execute('INSERT INTO dispatch_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (receipt_id, RECEIPT_SCHEMA, ticket['job_id'], ticket['revision'], request_digest, canonical, outcome,
                       accepted, changed, reason, after['state'], after['dispatch_state'], after['revision'], _json(result),
                       self.clock(), recorded_by or ''))
            return result

    def _receipt_binding(self, c, row, ticket, envelope):
        """Binding failures (receipt not accepted) come before gating failures (fact kept, state unchanged)."""
        outcome, thread_id = envelope['outcome'], envelope['thread_id']
        if _digest(row['dispatch_token']) != envelope['dispatch_digest']:
            return 'dispatch_mismatch', 0
        if row['revision'] != ticket['revision']:
            return 'revision_moved', 0
        if row['input_key'] != ticket['input_key']:
            return 'input_moved', 0
        if row['lease_owner'] != ticket['owner'] or (row['lease_token'] and _digest(row['lease_token']) != envelope['lease_digest']):
            return 'owner_mismatch', 0
        issued = c.execute("SELECT payload FROM subtitle_delivery_events WHERE job_id=? AND kind='dispatch-ticket-issued' ORDER BY id DESC LIMIT 1",
                           (ticket['job_id'],)).fetchone()
        if not issued or json.loads(issued['payload']).get('ticket_digest') != envelope['ticket_digest']:
            return 'evidence_invalid', 0
        if outcome in ('failed', 'interrupted'):
            return '', 1
        if _scope_transaction(c, row['show_id'], row['episode']):
            return 'scope_changed', 1
        watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?', (row['show_id'], row['episode'])).fetchone()
        policy = c.execute("SELECT value FROM kv WHERE key='subtitle_delivery_policy'").fetchone()
        two_pass_enabled = bool(policy and json.loads(policy['value']).get('editions') == ['v0', 'v1'])
        if watched and watched['finished'] and (not json.loads(row['payload']).get('experience_only') or two_pass_enabled):
            return 'scope_changed', 1
        if row['state'] in ('accepted', 'finished') or row['dispatch_state'] == 'blocked-stopped':
            return 'state_conflict', 1
        if outcome == 'created':
            if row['dispatch_state'] == 'acknowledged' and row['thread_id'] == thread_id:
                return 'already_acknowledged', 1
            if row['dispatch_state'] not in ('committing', 'unknown') or (row['thread_id'] and row['thread_id'] != thread_id):
                return 'state_conflict', 1
            return '', 1
        # not_created: only a creation attempt can be proven absent; an existing responsible thread is never cleared by it
        if ticket['dispatch_action'] != 'create' or row['thread_id'] or row['dispatch_state'] not in ('committing', 'unknown'):
            return 'state_conflict', 1
        # A list snapshot and caller assertions cannot rule out a delayed create.
        # The adapter must verify terminal absence against a bound host operation,
        # perform no IPC side effects, and fail closed. JSON booleans are not authority.
        try:
            verified = self.absence_verifier and self.absence_verifier(dict(ticket), envelope['evidence'], envelope['host_ref']) is True
        except Exception:
            verified = False
        if not verified:
            return 'evidence_invalid', 0
        return '', 1

    def receipts(self, identity):
        """The durable receipt ledger of one job (v1 and legacy provenance), oldest first; readers fall back to events."""
        if not _exists(self.db, 'dispatch_receipts'):
            return []
        rows = self.db.rows('SELECT * FROM dispatch_receipts WHERE job_id=? ORDER BY recorded_at,rowid', (identity,))
        return [{**r, 'envelope': json.loads(r['envelope']), 'result': json.loads(r['result'])} for r in rows]

    def reconciliation_plan(self, ticket, receipt_id):
        """Read-only review of a recorded created fact after revision changes.

        This is NOT a new ticket or authorization to apply. Explicit local recovery
        needs operator-reviewed host reverification plus a fresh current
        revision CAS, retaining the ORIGINAL dispatch/input/owner binding.
        """
        validate_ticket(ticket)
        if not (isinstance(receipt_id, str) and len(receipt_id) == 64 and all(ch in '0123456789abcdef' for ch in receipt_id)):
            raise ValueError('original receipt id must be a 64-hex digest')
        source = self.db.one('SELECT * FROM dispatch_receipts WHERE receipt_id=?', (receipt_id,))
        td = _digest(_canonical(ticket))
        issued = self.db.rows("SELECT payload FROM subtitle_delivery_events WHERE job_id=? AND kind='dispatch-ticket-issued'", (ticket['job_id'],))
        if not any(json.loads(r['payload']).get('ticket_digest') == td for r in issued):
            raise ValueError('original begin ticket is not server-bound')
        if not source or source['schema'] != RECEIPT_SCHEMA or source['job_id'] != ticket['job_id']:
            raise ValueError('original v1 receipt required')
        envelope = json.loads(source['envelope'])
        if source['outcome'] != 'created' or envelope.get('ticket_digest') != td:
            raise ValueError('a created fact bound to this original ticket is required')
        row = self.get(ticket['job_id'])
        reasons = []
        if _digest(row['dispatch_token']) != _digest(ticket['dispatch_token']): reasons.append('dispatch_mismatch')
        if row['input_key'] != ticket['input_key']: reasons.append('input_moved')
        if row['lease_owner'] != ticket['owner'] or _digest(row['lease_token']) != _digest(ticket['lease_token']): reasons.append('owner_mismatch')
        from .subtitle_refinement import two_pass
        if _scope(self.db, row['show_id'], row['episode']) or (_finished(self.db, row['show_id'], row['episode'])
                and (not row['payload'].get('experience_only') or two_pass(self.db))): reasons.append('scope_changed')
        already = row['dispatch_state'] == 'acknowledged' and row['thread_id'] == envelope['thread_id']
        if (row['state'] in ('accepted', 'finished') or row['dispatch_state'] == 'blocked-stopped'
                or (row['thread_id'] and row['thread_id'] != envelope['thread_id'])
                or (row['dispatch_state'] == 'acknowledged' and not already)
                or row['dispatch_state'] not in ('committing', 'unknown', 'acknowledged')):
            reasons.append('state_conflict')
        return {'job_id': ticket['job_id'], 'source_receipt_id': receipt_id, 'ticket_revision': ticket['revision'],
                'current_revision': row['revision'], 'recorded_thread_id': envelope['thread_id'],
                'status': 'blocked' if reasons else 'already_acknowledged' if already else 'requires_host_reverification',
                'blockers': reasons, 'can_apply': False, 'read_only': True,
                'next_action': 'owner review of authoritative host fact and explicit current-revision CAS; no credential retrofit'}

    def record_blocked(self, identity, thread_id, reasons, owner_stopped=False):
        """Keep the same responsible chat; a blocker does not authorize a second chat."""
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if not row or not thread_id or row['thread_id'] != thread_id:
                raise ValueError('Only the responsible chat may report its blocker')
            if row['state'] in ('accepted', 'finished'):
                raise ValueError('Accepted episodes cannot be downgraded to an ordinary blocker')
            if row['dispatch_state'] in ('committing', 'unknown'):
                raise ValueError('Unknown dispatch cannot be released as an ordinary production blocker')
            value = json.loads(row['payload']); value['production_blockers'] = list(reasons or [])
            value['owner_stopped_writing'] = owner_stopped is True
            status = 'blocked-stopped' if owner_stopped is True else row['dispatch_state']
            c.execute('UPDATE subtitle_delivery_jobs SET state=\'blocked\',dispatch_state=?,lease_expires=0,payload=?,revision=revision+1,updated=? WHERE id=?',
                      (status, _json(value), self.clock(), identity))
            self._event(c, identity, 'production-blocked', {'reasons': value['production_blockers']})
        return self.get(identity)

    def record_finished(self, identity, thread_id, release, player_report):
        """Verify a published viewing file, then release its sole writer's slot.

        No acceptance/default/watching change, and unknown dispatch is protected.
        """
        from .subtitle_finished_delivery import verify_release
        job = self._job(identity)
        if _scope(self.db, job['show_id'], job['episode']):
            raise ValueError('Delivery scope changed')
        if not thread_id or job['thread_id'] != thread_id or job['dispatch_state'] in ('committing', 'unknown'):
            raise ValueError('Only the confirmed responsible chat can finish its delivery')
        assessment = job['payload']['assessment']
        targets = [ref['target'] for ref in assessment.get('material_refs', [])
                   if ref.get('kind') == 'completed_media' and ref.get('target', {}).get('fingerprint')
                   == assessment.get('target_fingerprint')]
        if len(targets) != 1:
            raise ValueError('Exact dispatched target proof missing')
        observation = verify_release(self.db.path.parent.parent, release, player_report,
                                     job['show_id'], job['episode'], targets[0])
        from .subtitle_refinement import two_pass
        staged = two_pass(self.db)
        continuing = staged and job['kind'] == 'episode' and observation.get('edition', 'v0') == 'v0'
        # Opt-in: a finished v0 hands the single slot to another show's waiting first version; this
        # owner's v1 is queued behind them as a refinement task that resumes the same chat later.
        yielded_to = (self._first_version_waiting(job['show_id'])
                      if continuing and delivery_policy(self.db).get('v0_first_across_shows') is True else None)
        holding = continuing and not yielded_to
        if staged and observation.get('edition') == 'v1':
            baseline = job['payload'].get('baseline_release_id') or job['payload'].get('v0_observation', {}).get('release_id')
            if observation.get('baseline_release_id') != baseline:
                raise ValueError('V1 must refine this owner\'s exact playable v0')
        if staged and job['kind'] == 'refinement' and observation.get('edition') != 'v1':
            raise ValueError('Refinement task must deliver an evidenced v1')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if (_scope_transaction(c, row['show_id'], row['episode']) or row['thread_id'] != thread_id
                    or row['dispatch_state'] in ('committing', 'unknown') or row['revision'] != job['revision']):
                raise ValueError('Owner/scope/revision changed while verifying the release')
            if row['state'] == 'accepted':
                raise ValueError('Preserve the formal accepted record')
            value = json.loads(row['payload'])
            watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?', (row['show_id'], row['episode'])).fetchone()
            if staged and watched and watched['finished']:
                raise ValueError('Episode was watched; stop refinement and preserve existing subtitles')
            if row['state'] == 'finished' and value.get('finished_observation') == observation:
                return {**self.get(identity), 'duplicate': True, 'yielded_to': None}
            if holding and value.get('v0_observation') == observation:
                return {**self.get(identity), 'duplicate': True, 'continue_refinement': True, 'yielded_to': None}
            value['finished_observation'] = observation
            value['owner_stopped_writing'] = not holding
            value['refinement_pending'] = holding
            if continuing:
                value['v0_observation'] = observation
                c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                          (f"subtitle_delivery_v0_{row['show_id']}_{row['episode']}", _json(observation)))
            if holding:
                value['edition'] = 'v1'
            c.execute("UPDATE subtitle_delivery_jobs SET state=?,dispatch_state=?,lease_expires=0,payload=?,revision=revision+1,updated=? WHERE id=?",
                      ('production' if holding else 'finished', 'acknowledged' if holding else 'completed',
                       _json(value), self.clock(), identity))
            c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                      (f"subtitle_delivery_finished_{row['show_id']}_{row['episode']}", _json(observation)))
            self._event(c, identity, 'finished-observed', {**observation, 'yielded_to': yielded_to})
        return {**self.get(identity), 'continue_refinement': holding, 'yielded_to': yielded_to}

    def _first_version_waiting(self, show_id):
        """Another show's ready, undelivered, unwatched first version, if any; this show never counts."""
        for row in self.db.rows("""SELECT show_id,episode FROM subtitle_delivery_jobs WHERE kind='episode' AND state='ready'
                                   AND dispatch_state='' AND show_id!=? ORDER BY episode,created""", (show_id,)):
            if not (_delivered(self.db, row['show_id'], row['episode']) or _finished(self.db, row['show_id'], row['episode'])
                    or _scope(self.db, row['show_id'], row['episode'])):
                return {'show_id': row['show_id'], 'episode': row['episode']}
        return None

    def record_accepted(self, identity, thread_id, build_id):
        """Observe host acceptance, never create a quality certificate/selection."""
        job = self._job(identity)
        if _scope(self.db, job['show_id'], job['episode']):
            raise ValueError('Delivery scope changed; acceptance cannot be registered')
        assessment = self._assessment(job['show_id'], job['episode'], self._source_payload(job))
        if (assessment.get('accepted_verified') is not True
                or not build_id or assessment.get('accepted_build_id') != build_id
                or assessment.get('target_fingerprint') != job['payload']['assessment'].get('target_fingerprint')):
            raise ValueError('Actual exact accepted build has not been verified by the host')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (identity,)).fetchone()
            if _scope_transaction(c, row['show_id'], row['episode']):
                raise ValueError('Delivery scope changed during acceptance registration')
            if not row or not thread_id or row['thread_id'] != thread_id:
                raise ValueError('Acceptance must come from the responsible chat')
            if row['state'] == 'accepted' and row['accepted_build_id'] == build_id:
                return {**dict(row), 'payload': json.loads(row['payload']), 'build_id': build_id,
                        'published': False, 'duplicate': True}
            value = json.loads(row['payload']); value['accepted_observation'] = assessment
            if row['kind'] == 'late':
                primary_id = self.episode_id(row['show_id'], row['episode'])
                primary = c.execute('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (primary_id,)).fetchone()
                expected_baseline = value['baseline_build_id']
                if not primary or primary['accepted_build_id'] != expected_baseline:
                    raise ValueError('Accepted V2 baseline changed; reconcile before advancing it')
                primary_payload = json.loads(primary['payload'])
                primary_payload.setdefault('accepted_history', []).append(expected_baseline)
                primary_payload['accepted_observation'] = assessment
                c.execute('UPDATE subtitle_delivery_jobs SET accepted_build_id=?,payload=?,revision=revision+1,updated=? WHERE id=?',
                          (build_id, _json(primary_payload), self.clock(), primary_id))
            c.execute('UPDATE subtitle_delivery_jobs SET state=\'accepted\',accepted_build_id=?,payload=?,dispatch_state=\'completed\',lease_expires=0,revision=revision+1,updated=? WHERE id=?',
                      (build_id, _json(value), self.clock(), identity))
            self._event(c, identity, 'accepted-observed', {'build_id': build_id})
        return {**self.get(identity), 'build_id': build_id, 'published': False}

    def enqueue_late_review(self, sid, ep, source_hash, text_hash, evidence):
        """Deduplicate late sources against one accepted baseline; propose V2 only.

        Matching normalized full text records the new source but creates no new
        reviewer chat. New distinct text is coalesced into one undispatched V2
        job; new text during an active V2 creates a later pending batch, keeping
        the active input unchanged. None of these calls alter the accepted build.
        """
        if _scope(self.db, sid, ep):
            raise ValueError('Late review is outside current watching authorization')
        if not source_hash or not text_hash or not evidence:
            raise ValueError('Late source hash, normalized text hash and provenance required')
        initial = self._job(self.episode_id(sid, ep))
        if initial['state'] != 'accepted' or not initial['accepted_build_id']:
            raise ValueError('Late review requires an actually accepted baseline')
        baseline = initial['accepted_build_id']
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if _scope_transaction(c, sid, ep):
                raise ValueError('Late review scope changed before recording')
            current = c.execute('SELECT state,accepted_build_id FROM subtitle_delivery_jobs WHERE id=?',
                                (initial['id'],)).fetchone()
            if not current or current['state'] != 'accepted' or current['accepted_build_id'] != baseline:
                raise ValueError('Accepted baseline changed before late-input recording')
            old = c.execute('SELECT * FROM subtitle_delivery_late_inputs WHERE show_id=? AND episode=? AND baseline_build_id=? AND source_hash=?',
                            (sid, ep, baseline, source_hash)).fetchone()
            if old:
                if old['text_hash'] != text_hash:
                    raise ValueError('Same immutable source hash has conflicting text')
                return {'job_id': old['job_id'], 'disposition': old['disposition'], 'duplicate': True}
            baseline_texts = set(initial['payload'].get('accepted_observation', {}).get('text_hashes') or [])
            baseline_texts.update(ref['text_hash'] for ref in initial['payload'].get('accepted_observation', {}).get('material_refs') or []
                                  if isinstance(ref, dict) and ref.get('text_hash'))
            seen_text = c.execute('SELECT job_id FROM subtitle_delivery_late_inputs WHERE show_id=? AND episode=? AND baseline_build_id=? AND text_hash=? LIMIT 1',
                                 (sid, ep, baseline, text_hash)).fetchone()
            if text_hash in baseline_texts or seen_text:
                identity = seen_text['job_id'] if seen_text else None
                disposition = 'same-text'
            else:
                pending = c.execute("SELECT * FROM subtitle_delivery_jobs WHERE show_id=? AND episode=? AND kind='late' AND state='late-review' AND dispatch_state NOT IN ('committing','unknown','acknowledged') ORDER BY created DESC LIMIT 1", (sid, ep)).fetchone()
                new_source = {'source_hash': source_hash, 'text_hash': text_hash, 'evidence': evidence}
                if pending and json.loads(pending['payload']).get('baseline_build_id') == baseline:
                    identity = pending['id']; value = json.loads(pending['payload'])
                    value['late_sources'].append(new_source)
                    c.execute('UPDATE subtitle_delivery_jobs SET payload=?,input_key=?,revision=revision+1,updated=? WHERE id=?',
                              (_json(value), _hash({'baseline': baseline, 'late_sources': value['late_sources']}), self.clock(), identity))
                else:
                    identity = f'late:{sid}:{ep}:{uuid.uuid4().hex}'
                    value = {'baseline_build_id': baseline, 'late_sources': [new_source],
                             'assessment': initial['payload'].get('accepted_observation') or {},
                             'proposal_only': True, 'finished': _finished(self.db, sid, ep)}
                    c.execute('INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,thread_id,created,updated) VALUES(?,?,?,\'late\',\'late-review\',?,?,?,?,?)',
                              (identity, sid, ep, _hash({'baseline': baseline, 'late_sources': value['late_sources']}), _json(value), initial['thread_id'], self.clock(), self.clock()))
                disposition = 'incremental-v2-review'
                self._event(c, identity, 'late-source', {'baseline_build_id': baseline, 'source_hash': source_hash})
            c.execute('INSERT INTO subtitle_delivery_late_inputs VALUES(?,?,?,?,?,?,?,?,?)',
                      (sid, ep, baseline, source_hash, text_hash, identity, disposition, _json(evidence), self.clock()))
            return {'job_id': identity, 'disposition': disposition, 'duplicate': False, 'published': False}
