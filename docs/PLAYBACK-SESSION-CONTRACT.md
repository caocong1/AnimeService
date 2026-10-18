# Explicit playback writer takeover — owner proposal v2

Status: **isolated backend core implemented; no production imports, routes,
advertisement or activation**. Production accepts v1 progress. Finished releases require session+sequence; ordinary
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

`anime/playback_writers.py` is a standalone core, only constructed in isolated
tests. It is not imported by the app, and creates no production tables or routes.
The capability callback defaults to false; its internal capabilities() result is
not a production capability response. No Web/TV client has been upgraded here.

The trusted future adapter resolves canonical media identity (including finished
release signature), applies existing origin/auth guards, and maps claim conflicts
and WriterConflict to structured HTTP 409. The core returns historical claim
results on identical retries; replay is not evidence that a writer is still live.
A conflict, reclaim or explicit takeover uses a NEW request_id and observed
expected_generation; clients must never auto-change mode or reuse stale packets.

Progress, heartbeat and release share one strictly increasing sequence. Writer
generation rows are never pruned, including after release, expiry or feature-off.
Claim receipts retain both accepted and rejected requests for seven days. Removal
of a successful claim receipt cannot regrant its old expected_generation.

Before activation, integrate guard_legacy into the SAME transaction of ALL old
progress writes (ordinary, finished, external history import, and any fallback),
and wire v2 saves atomically to the same canonical progress row. Do not activate
only claim routes: current v1 writes would bypass the fence. This round deliberately
leaves those production paths unchanged. A feature-off core rollback is tested;
rolling the whole server back to an old binary without these guards is NOT safe
and has not been accepted. Web/TV visible conflict and explicit takeover, source
switch/pagehide, mixed-version and real-device QA remain required.
