# Owner review: dispatch receipt v1 (staged implementation)

The ledger, immutable begin-ticket binding, created acknowledgement and diagnostic
paths are implemented. Enforcement remains off. Production v1 not-created
application is disabled until an owner-reviewed trusted host adapter can verify
terminal absence; a list snapshot or caller assertion cannot authorize release.
Existing deployment and ownership protections remain in force.

## Distinguish the facts

`confirm` calls `reconcile_dispatch`: created means a responsible **chat** was
created/reused, not that subtitles or a viewing release were completed.
`not-created` proves chat absence for one exact dispatch. A media path, hash,
duration or subtitle row count cannot prove either fact. Start with schema
`dispatch-receipt/v1` and a `dispatch_receipts` table. A future finished-release
receipt needs a separate schema/kind and must enter through `record_finished` and
the existing release/player verifier. Never derive finished/accepted from a
successful dispatch acknowledgement or a process exit.

The current code already handles an identical confirmed thread as `duplicate`,
and `record_finished` handles identical observations. Describe the gap as a
missing versioned durable receipt ledger, not as absence of all idempotence.

## Fields and identity

Keep schema, job_id, owner/responsible-thread identity, input identity, outcome,
evidence, server recorded_at/recorded_by and a reason. Rename revision_seen to
expected_revision and record resulting_revision separately. Bind all request
identity fields to the immutable ticket returned by **begin_dispatch**. Do not
read the latest job token/revision at confirm time to retrofit an old callback.
The v1 CLI path must accept that ticket explicitly; an unbound old invocation
must not become a v1-accepted transition by borrowing current credentials.
Ship enforcement off initially and preserve the old workflow as explicitly
legacy. Migrate callers and their archived begin tickets through an owner-reviewed
rollout before requiring v1 globally. No task/prompt edits as a hidden migration.
Add explicit thread_id and dispatch_action=create|reuse: created requires the
confirmed thread id. A not-created result for an attempted resume never proves
that the already responsible thread disappeared and cannot clear that owner.
Bind the host operation/request id where available. An empty chat-list search
alone cannot prove an unknown creation request will not complete later: require
authoritative terminal absence evidence, otherwise preserve unknown.

Store a digest/reference for dispatch/lease credentials rather than duplicating
raw authority tokens in exported receipt JSON. The live token still has to be
verified by the service. Lease expiry after begin is not alone evidence that an
already-created chat did not exist; do not throw away a late true acknowledgement
or release its ownership. Input_key/input_signature need one authoritative
meaning and a match to the dispatched assessment, not two unchecked caller values.

receipt_id hashes a specified canonical immutable request envelope: schema,
job/ticket identity, expected revision, input identity, outcome and bound evidence.
Exclude server recorded_at, recorded_by, receipt_accepted, reason and resulting
revision. Define UTF-8 encoding, key ordering, number/time normalization and absent
versus null semantics. Evidence's own checked_at remains bound evidence. Identical
retries must have the same id; changed evidence is a different request. Preserve
request_digest to detect a supplied id reused with different content.

Use `receipt_accepted` for receipt acceptance, distinct from formal subtitle
quality acceptance, and `state_changed` separately. Store resulting state/revision and the original result so a
retry can return exactly that result even after the job advances. No history
backfill that pretends to validate old jobs; old facts retain explicit legacy
provenance and readers have a defined fallback while v1 coverage is incomplete.

## Atomic acceptance and failure

1. Validate and bound the envelope/evidence. Malformed syntax, unauthorized calls
   and contradictory arguments remain nonzero CLI/errors; never label them as
   successful completion. Bounded diagnostic rejection records are allowed.
2. In one write transaction, look up receipt_id **before** comparing current job
   revision. Identical duplicates return the first receipt result; do not insert
   accepted=0/reason=duplicate or overwrite the original acceptance.
3. For a new receipt, require the original job, dispatch token, expected revision,
   input identity and applicable owner/lease binding. Also retain current stop,
   scope, watched, committing/unknown and responsible-thread protections. A valid
   created-chat fact that cannot be applied is still preserved as evidence; it
   must not resurrect stopped work. State changes need an explicit allowed-state
   check in the same transaction, including for not-created returning to ready.
4. Insert the receipt and perform its permitted job CAS atomically. CAS failure
   rolls back the attempted mutation and records a rejected result in that same
   transaction; it never leaves an accepted receipt without its state change.
   Reasons include dispatch_mismatch, revision_moved, input_moved, owner_mismatch,
   scope_changed, evidence_invalid and state_conflict. No partial event/job/receipt
   commits. File verification may happen outside the transaction, but bind its
   hash and recheck all mutable job/scope facts inside.
5. created/not_created use the existing reconcile paths **only for dispatch**.
   failed/interrupted record diagnostics without release, retry, owner transfer,
   cancellation reversal or business-complete flags. Preserve unknown external
   outcomes. `not-created` retains the existing checked_at/method/checked_by,
   timezone, job/dispatch binding and trusted file hash requirements in the
   shared service boundary, not only a CLI parser.
   Bound payload sizes, verify timezone/finite values and reject implausible
   future evidence times; reported methods/actors are provenance, not authority.

Tests use isolated DBs, fake clocks/host receipts and injected faults: duplicate
after revision advancement; concurrent different outcomes; stale/mismatched
ticket; changed input/owner/scope/watch/stop; lease expiry after a real create;
reused id with changed payload; interruption before and after commit; replay after
restart; malformed evidence; legacy callers and readers. No real IPC interruption,
chat creation, production DB backfill or production owner manipulation in tests.

## Explicit dispatch reconciliation review (owner round 12)

The only new command is READ ONLY:

    subtitle_delivery_queue.py --db DB reconcile-plan JOB --ticket ORIGINAL_TICKET --receipt-id ORIGINAL_CREATED_RECEIPT

It uses ReadStore (SQLite mode=ro/query_only, initialize=False), verifies the
server-bound original ticket and original v1 created fact, and reports current
revision and protection blockers. can_apply is always false. It issues no ticket,
changes no job/event/receipt/owner, does not call IPC and does not refresh a view.
No token or evidence body is printed. already_acknowledged is not a reapply plan.

Migration audit found the dispatcher prompt still describes legacy confirm and
unknown without --ticket or --host-ref. Prompts/configuration and the enforcement
flag are unchanged. Begin ticket_path must be archived BEFORE the external call;
carry that exact file and an actual host operation reference through callbacks.
Use interrupted --ticket on lost response; legacy unknown moves revision and is
not a substitute for migrated diagnostics. Never fabricate host_ref to evade a
stored idempotent result. Ticket/archive failure leaves committing protected.

A FUTURE mutating reconciliation needs a separate owner-reviewed contract:
original ticket + immutable original receipt id + explicit expected CURRENT
revision + fresh authoritative host reverification evidence/operation identity.
Inside one transaction, first replay a reconciliation id, then CAS that explicit
current revision and recheck ORIGINAL dispatch/input/owner/lease binding, current
scope/watch/stop/state/responsible-thread protections; record a derived event and
receipt referencing the original fact atomically. Never borrow new credentials,
issue a fake replacement begin ticket, rewrite the original receipt or reuse a
host reference as a retry nonce. Changed dispatch/input/owner or stopped work is
a conflict, not permission to transfer or restart it. not_created remains disabled
without an approved terminal-absence verifier. No mutating recovery command or
production caller migration has been implemented in this round.
