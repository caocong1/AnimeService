# Explicit playback writer takeover — owner proposal v2

Status: **core/resolver/passive fences and isolated HTTP adapter implemented; no production v2
constructor, routes, advertisement or activation**. Production accepts v1 progress while the
capability is off and no durable fence exists. Finished releases require session+sequence; ordinary
media permits older clients, including TV, without these fields. V1 orders
packets within a session, but a different session can still write. The existing
`viewing-session` route protects subtitle work from active viewing; it is not a
cross-device progress writer fence. Do not change these meanings during a
watch.js refactor.

## Identity and negotiation

The server resolves an immutable canonical media key. Finished release identity
includes the release and video signature; versions keep independent progress.
A device label is diagnostic, never authorization. A writer fence is scoped to
one media key; it neither owns a delivery job nor replaces the episode viewing
lease. Manual watched corrections remain separate, shared and explicit.

Add a capability version before new requests. Older servers and missing
capabilities retain v1 behavior. V2 enforcement remains off until both Web and
the signed TV client handle conflict, expiry and takeover visibly. No silent
fallback to v1 after a negotiated v2 conflict. API paths below are reserved
proposals and must not be called by research branches yet.

## Proposed operations

* `GET /api/playback-sessions/state?media_key=...` reads current generation,
  expiry and a safe device label. It does not acquire or renew anything and
  never returns a writer secret. Merely opening a page must not claim a writer.
  With no writer it still returns the last durable generation (zero initially).
* `POST /api/playback-sessions/claim` is sent on an explicit Play/Resume action.
  It contains `media_key`, `session`, an idempotent `request_id`, a random
  client-generated `writer_token`, `device_id`, `expected_generation`, and
  `mode: open|takeover`.
  The server stores only the token digest. Tokens stay out of URLs, logs,
  analytics and shared storage. Existing local-origin/auth guards still apply.
* Both modes compare `expected_generation` atomically. `open` increments the
  durable generation when no live writer exists. Another live writer
  yields HTTP 409 `takeover_required` with its generation and safe label.
  A takeover requires a visible user action plus `expected_generation`; the
  atomic compare-and-swap either increments generation once or returns a fresh
  conflict. A stale response must never cause automatic takeover.
* Same request, token, media and session retries return the same result; reuse
  with different identity fails. Persist the generation and idempotence evidence
  atomically with acquisition, including across server restart. Use bounded
  receipt retention without reusing generations or accepting old writer tokens.
  After a receipt expires, its old claim cannot be granted again: its expected
  generation predates the successful acquisition. Return a conflict, never
  reinterpret it as a new open. Release and expiry retain the generation.
* Progress and heartbeat require the current generation, token, session and a
  strictly increasing sequence. A transaction checks the fence and sequence
  before changing progress. Never use client wall-clock time to select a winner.
  Duplicate/older packets return `ok:true, ignored:true` with a reason; fenced
  packets return reason `fenced`, and expired packets `expired`. The client stops
  background retries, preserves local position and offers explicit reclaim.
* Release carries the same fence; a late release cannot clear the next writer.
  Expiry also requires a fresh claim before further writes. Once v2 enforcement
  is enabled, legacy writes receive a structured 409 `session_required`, including
  after release/expiry. Expiry never resets the generation or restores legacy
  write access. This compatibility restriction is why activation is gated.

## UI and migration acceptance

Web and TV must show which device owns progress and a clear Take over action.
Failure remains visible on TV; `runCatching` must not hide a rejected save.
Playback may continue locally after conflict, but saved-state wording must
reflect that progress is not being written. Ended/seek/claim do not mark watched.
Back, pagehide, retry and release must retain the exact original media identity
and fence even after a source switch. Do not replay queued stale packets on a
new claim. Remote-control focus and cancellation need real TV verification.

Required isolated tests: Web A → TV B explicit takeover; delayed A progress,
heartbeat and release; simultaneous compare-and-swap; repeated claim; expiry
and restart; legacy and mixed-version clients; source switch with late response;
independent release versions; rejected save then explicit reclaim; watch/unwatch
remains manual. Use fake clocks, fresh DBs and synthetic media. Migration must
preserve existing progress/watches and start with no active v2 claim; never
repair or seed production ownership by hand. Rollout needs a signed APK,
verified signing identity/version and a staged capability switch after both
clients pass. A rollback must reject stale v2 traffic safely; it cannot silently
resume legacy writes while a v2 client believes its fence is enforced.

## Implemented core boundary (owner round 12)

`PlaybackWriters` is only constructed in isolated tests. The module is now imported
for passive legacy guards and structured 409 handling; imports create no tables/routes.
The capability callback defaults to false; its internal capabilities() result is
not a production capability response. No Web/TV client has been upgraded here.

The explicitly injected adapter resolves canonical media identity (including finished
release signature), applies existing origin/auth guards, and maps claim conflicts
and WriterConflict to structured HTTP 409. The core returns historical claim
results on identical retries; replay is not evidence that a writer is still live.
A conflict, reclaim or explicit takeover uses a NEW request_id and observed
expected_generation; clients must never auto-change mode or reuse stale packets.

Progress, heartbeat and release share one strictly increasing sequence. Writer
generation rows are never pruned, including after release, expiry or feature-off.
Claim receipts retain both accepted and rejected requests for seven days. Removal
of a successful claim receipt cannot regrant its old expected_generation.

Owner round 15 integrates passive guards into ordinary progress, finished progress
(including the Web alias) and external history import. Each uses BEGIN IMMEDIATE
before checking the durable generation, before in_order/session-receipt writes.
With no v2 table the guard creates nothing. A positive generation remains fenced
after expiry, release or feature-off. Direct progress returns structured 409
session_required; the importer skips/counts only fenced progress, preserving
independently verified watched facts and manual priority.

The explicitly injected adapter installs its enabled callback on the shared Store
as playback_writer_enabled, including all three legacy paths, and atomically wire
v2 saves to the same canonical row. No production callback or new KV is installed.
A feature-off core rollback is tested;
rolling the whole server back to an old binary without these guards is NOT safe
and has not been accepted. Web/TV visible conflict and explicit takeover, source
switch/pagehide, mixed-version and real-device QA remain required.

## Server resolver boundary (owner round 15)

PlaybackMediaResolver consumes the actual WebPlayer inventory and FinishedViewing
catalog, not a caller alias map. Ordinary IDs must equal the server path-derived
inventory key and pass existing file/size checks. finished:release-id and the
catalog's signature-derived media_id resolve to the SAME progress row; distinct
release signatures stay independent. Missing/ambiguous entries, changed mapping,
missing/changed-size files and a manifest version moving during lookup fail.
Resolution does not acquire a writer, create tables, or change progress/watches.

Owner round 16 requires OrdinaryMediaVersions for ordinary v2 resolution. No
verifier/registration means rejection, not an automatic first-request pin. A
trusted control-plane verifier supplies its independently known full SHA-256 to
register(); the local registry binds the existing media key/path/size and
show/episode/season association immutably. Rebinding that key to changed content
or association is forbidden, including after restart. This has no HTTP enrolment
endpoint, no production constructor and no progress/history backfill. Unknown
historical progress is not retroactively certified by initial registration.

Every resolution rereads all bytes, compares the digest, checks handle stability
and path identity, and rechecks association. Default limits are 1 GiB and two
seconds; limits are trusted constructor values, not packet fields. Missing,
changed, oversize, timed-out or unverifiable media fail closed. Same-size content
replacement with restored mtime is rejected; identical content at the same alias
continues to use the same progress row. Windows stat/fstat ctime semantics differ,
so ctime is compared only within one API, not across path/handle calls.

This verifies observed bytes; it does not freeze future writes or make filesystem
and database updates atomic. Full hashing on every resolution can be expensive,
and cannot interrupt arbitrary blocked OS I/O. Production registration provenance,
immutable-publication/handle lifetime and realistic performance acceptance remain
rollout prerequisites. There is no silent stat-cache shortcut and no mechanism
to migrate/rebind a replaced ordinary alias in this round.

## Isolated HTTP adapter (owner round 16)

create_app(..., playback_writers=None) is the production default. Only an explicit
trusted injected instance registers state/claim/progress/heartbeat/release under
/api/playback-sessions and installs a dynamic playback_writer_enabled callback on
the SAME Store. It constructs no core or registry itself. Normal production has
no v2 tables, routes, callback or capability advertisement. No Web/TV negotiation
or behavior is upgraded.

The routes run through the existing Host/session/Origin/x-anime-token middleware.
Disabled returns structured 503; takeover/generation/fence/expiry/sequence conflicts
return structured 409 with ok=false. State does not claim or renew. Invalid input
does not echo tokens or paths. Claim keeps core idempotence; progress/heartbeat/
release share sequence and target the existing canonical progress row. Registering
evidence, changing enablement and client-provided aliases/digests are not public
operations. No production injection/activation or new key is introduced here.

Transaction references: [SQLite transactions](https://www.sqlite.org/lang_transaction.html),
[Python sqlite3 transaction control](https://docs.python.org/3.14/library/sqlite3.html#transaction-control).
Content evidence references: [Python hashlib](https://docs.python.org/3.14/library/hashlib.html),
[Python stat fields](https://docs.python.org/3.14/library/os.html#os.stat_result).
