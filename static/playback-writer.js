/* Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md; owner client contract round 17,
   review round 19). A pure state machine over an injected transport; the page wires it in only after
   the bootstrap advertises capabilities.playback_writer.
   - identity: the wire media key is the canonical key the server pinned for the loaded version; every
     validated reply must echo requested_media_key and keep media_key bound to it;
   - every job (progress/heartbeat/release/final) captures epoch + generation + token + session + an
     immutable snapshot when it is created; it is dropped, without consuming a sequence, if the fence
     changed before it runs; sequence numbers are assigned only at execution, in order;
   - a success needs a 2xx, ok === true, no ignored flag, the packet's own generation and sequence;
   - a transport exception is a network failure bound to its epoch, never a success and never an escape
     from the unsaved state; a protocol/authorization failure (disabled, 404, 400, 401, 403, bad body)
     halts writing for this media: local position kept, error visible, no legacy fallback, no auto claim;
   - fenced/expired/session_required stop writing and keep the position until an explicit reclaim;
   - disposed is terminal: nothing is sent afterwards, including jobs scheduled earlier;
   - final(): the last snapshot on pagehide invalidates every pending job so no older position can
     follow it with a higher sequence; it never releases;
   - a claim answered after a newer claim, a replayed claim, or a retried claim never counts as live
     ownership without a state check in the same epoch. */
(function(root) {
  'use strict';
  const BASE = '/api/playback-sessions';
  const MAX_SEQUENCE = 2147483647, LABEL_MAX = 32;
  const UNSAFE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}]/gu;
  const HALT = new Set(['disabled', 'protocol', 'unauthorized']);
  const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  function defaultRandom(n) {
    const bytes = new Uint8Array(n);
    (globalThis.crypto || root.crypto).getRandomValues(bytes);
    return hex(bytes);
  }
  const safeInt = (v, min = 0) => Number.isSafeInteger(v) && v >= min ? v : null;
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  /* Device labels are display text only: strip control/format characters, trim, cap at 32 code points. */
  function sanitizeLabel(value, max = LABEL_MAX) {
    const clean = typeof value === 'string' ? value.replace(UNSAFE, '').trim() : '';
    return clean ? Array.from(clean).slice(0, max).join('') : null;
  }
  function deviceLabel(prefix, random = defaultRandom) {
    return sanitizeLabel(`${prefix} ${random(2).toUpperCase()}`);
  }
  /* Only a 2xx with a JSON object body can succeed; everything else is classified, never trusted. */
  function classify(r) {
    const body = r && typeof r.body === 'object' && r.body !== null ? r.body : null;
    if (!r || r.status === 0 || r.status === undefined) return {kind: 'network', body, http: 0};
    if (r.status === 503 || body?.reason === 'disabled') return {kind: 'disabled', body, http: r.status};
    if (r.status === 409 && body && typeof body.reason === 'string') return {kind: 'conflict', body, http: 409};
    if (r.status === 401 || r.status === 403) return {kind: 'unauthorized', body, http: r.status};
    if (r.ok && r.status >= 200 && r.status < 300 && body) return {kind: 'ok', body, http: r.status};
    if (r.status >= 500) return {kind: 'server', body, http: r.status};
    return {kind: 'protocol', body, http: r.status};
  }
  function create(options) {
    const {post, get, mediaKey, session, random = defaultRandom, base = BASE, retries = 2} = options;
    const device = sanitizeLabel(options.device);
    if (typeof post !== 'function' || typeof get !== 'function') throw new TypeError('post and get transports are required');
    if (typeof mediaKey !== 'string' || !mediaKey || typeof session !== 'string' || session.length < 16 || !device) {
      throw new TypeError('mediaKey, a session of at least 16 characters and a device label are required');
    }
    let status = 'idle', epoch = 0, lane = 0, fence = null, sequence = 0, conflict = null, lastError = null, unsaved = false;
    let observed = {generation: 0, active: false, device: null};
    let queue = Promise.resolve(), claiming = false;
    const listeners = new Set();
    const snapshot = () => ({status, generation: fence?.generation ?? conflict?.generation ?? observed.generation, expires: fence?.expires ?? null,
                             conflict: conflict ? {...conflict} : null, writing: status === 'writing', unsaved, error: lastError});
    const notify = () => { for (const fn of listeners) fn(snapshot()); };
    const setStatus = (next, nextConflict = conflict) => { status = next; conflict = nextConflict; notify(); };
    const live = ticket => ticket.epoch === epoch && status !== 'disposed';
    const call = async (fn, ...args) => { try { return await fn(...args); } catch (_) { return {status: 0, ok: false, body: null}; } };
    const bound = body => body.requested_media_key === undefined || body.requested_media_key === mediaKey;   // the echo must be ours
    /* Failures: a short network fault keeps the fence (the next snapshot may be rebuilt); a protocol or
       authorization fault halts this media until an explicit new claim after the capability is re-read. */
    const fail = (ticket, kind, http) => {
      if (!live(ticket)) return {ok: false, reason: 'stale_response'};
      lastError = http ? `${kind}_${http}` : kind;
      unsaved = true;
      if (HALT.has(kind)) { fence = null; setStatus('halted'); }
      else setStatus(fence ? 'writing' : 'error');
      return {ok: false, reason: lastError};
    };
    async function state() {
      if (status === 'disposed') return {supported: false, reason: 'disposed'};
      const ticket = {epoch};
      const r = await call(get, `${base}/state?media_key=${encodeURIComponent(mediaKey)}`);
      const {kind, body, http} = classify(r);
      if (!live(ticket)) return {supported: false, reason: 'stale_response'};
      if (kind === 'disabled' || kind === 'protocol' || kind === 'unauthorized') { fail(ticket, kind, http); if (!fence) setStatus('unsupported'); return {supported: false, reason: lastError}; }
      if (kind !== 'ok') return {supported: false, reason: kind};
      if (body.media_key !== mediaKey || !bound(body) || safeInt(body.generation) === null || typeof body.active !== 'boolean') {
        fail(ticket, 'protocol', http); return {supported: false, reason: 'invalid_state'};
      }
      observed = {generation: body.generation, active: body.active, device: body.active ? sanitizeLabel(body.device_id) : null};
      notify();
      return {supported: true, generation: observed.generation, active: observed.active, device: observed.device};
    }
    /* mode 'open' for an explicit Play/Resume; 'takeover' only from a visible user action with the generation shown. */
    async function claim({mode = 'open', expectedGeneration} = {}) {
      if (mode !== 'open' && mode !== 'takeover') throw new TypeError('mode must be open or takeover');
      if (mode === 'takeover' && safeInt(expectedGeneration) === null) throw new TypeError('takeover needs the generation the user saw');
      if (status === 'disposed') return {ok: false, reason: 'disposed'};
      if (claiming) return {ok: false, reason: 'claim_in_progress'};
      claiming = true;
      try {
        const expected = safeInt(expectedGeneration) ?? snapshot().generation;
        const ids = {request_id: random(16), writer_token: random(32)};
        if (ids.request_id.length < 16 || ids.writer_token.length < 32 || ids.writer_token.length > 128) throw new TypeError('random identifiers are too short');
        const request = {media_key: mediaKey, session, ...ids, device_id: device, expected_generation: expected, mode};
        const ticket = {epoch: ++epoch};           // every older claim, job and response is now inert
        lane++; fence = null; sequence = 0; queue = Promise.resolve(); lastError = null;
        let r, attempt = 0;
        do {                                       // a network failure retries the SAME packet, never a second unknown claim
          attempt++;
          r = await call(post, `${base}/claim`, request, {});
        } while (classify(r).kind === 'network' && attempt <= retries && live(ticket));
        if (!live(ticket)) return {ok: false, reason: 'superseded'};
        const {kind, body, http} = classify(r);
        if (kind === 'conflict' || (kind === 'ok' && body.ok === false)) {
          if (!bound(body)) return fail(ticket, 'protocol', http);
          conflict = {reason: body.reason || 'conflict', generation: safeInt(body.generation) ?? 0, device: body.active ? sanitizeLabel(body.device_id) : null};
          observed = {generation: conflict.generation, active: body.active === true, device: conflict.device};
          setStatus('conflict', conflict);
          return {ok: false, reason: conflict.reason, generation: conflict.generation, device: conflict.device};
        }
        if (kind !== 'ok') return fail(ticket, kind, http);
        if (body.ok !== true || body.media_key !== mediaKey || !bound(body) || safeInt(body.generation, 1) === null || !finite(body.expires)) {
          return fail(ticket, 'protocol', http);
        }
        if (attempt > 1 || body.replayed === true) {   // history is not live ownership: confirm through state in this epoch
          const current = await state();
          if (!live(ticket) || !current.supported || !current.active || current.generation !== body.generation) {
            if (live(ticket)) { fence = null; setStatus('stale', {reason: 'stale', generation: current.generation ?? body.generation, device: current.device ?? null}); }
            return {ok: false, reason: 'verify_failed'};
          }
        }
        fence = {generation: body.generation, token: request.writer_token, expires: body.expires};
        observed = {generation: body.generation, active: true, device};
        unsaved = false;
        setStatus('writing', null);
        return {ok: true, generation: fence.generation, expires: fence.expires};
      } finally { claiming = false; }
    }
    /* A job is captured now (identity + immutable snapshot) and checked again when it runs. */
    function job(path, extra, options = {}) {
      if (status !== 'writing' || !fence) return {skip: {ok: false, reason: status === 'idle' ? 'no_claim' : status}};
      return {capture: {epoch, lane, generation: fence.generation, token: fence.token, path, extra: Object.freeze({...extra}), keepalive: options.keepalive === true}};
    }
    async function execute(c) {
      if (status === 'disposed' || c.epoch !== epoch || c.lane !== lane || !fence || fence.generation !== c.generation || status !== 'writing') {
        return {ok: false, reason: 'stale_response'};             // the fence or the lane moved: dropped, no sequence consumed
      }
      if (sequence >= MAX_SEQUENCE) return {ok: false, reason: 'sequence_exhausted'};
      const packet = Object.freeze({media_key: mediaKey, session, generation: c.generation, writer_token: c.token, sequence: ++sequence, ...c.extra});
      const ticket = {epoch: c.epoch, generation: c.generation};
      const r = await call(post, c.path, packet, {keepalive: c.keepalive});
      const {kind, body, http} = classify(r);
      if (!live(ticket) || !fence || fence.generation !== ticket.generation) return {ok: false, reason: 'stale_response'};
      if (kind === 'conflict' || (kind === 'ok' && (body.ignored || body.ok === false))) {
        const reason = body.reason || 'rejected';
        if (reason === 'fenced' || reason === 'expired' || reason === 'session_required') {
          fence = null; unsaved = true;                             // another writer owns progress now (or ours lapsed): keep the local position
          setStatus(reason === 'expired' ? 'expired' : 'fenced', {reason, generation: safeInt(body.generation) ?? ticket.generation, device: body.active ? sanitizeLabel(body.device_id) : null});
          return {ok: false, reason};
        }
        if (body.ignored) { unsaved = true; notify(); return {ok: false, ignored: true, reason}; }   // out_of_order: this packet was not applied
        return fail(ticket, reason);
      }
      if (kind !== 'ok') return fail(ticket, kind, http);
      if (body.ok !== true || body.ignored || body.generation !== packet.generation || body.sequence !== packet.sequence) return fail(ticket, 'protocol', http);
      unsaved = false; lastError = null; notify();
      return {ok: true, sequence: packet.sequence};
    }
    function send(path, extra, options) {
      const j = job(path, extra, options);
      if (j.skip) return Promise.resolve(j.skip);
      if (j.capture.keepalive) return execute(j.capture);          // the final snapshot bypasses the queue
      const next = queue.then(() => execute(j.capture), () => execute(j.capture));
      queue = next.catch(() => {});
      return next;
    }
    const progress = ({position, duration, playing}, options) => send(`${base}/progress`, {position, duration, playing: playing === true}, options);
    const heartbeat = options => send(`${base}/heartbeat`, {}, options);
    async function release(options) {
      const r = await send(`${base}/release`, {}, options);
      if (r.ok) { fence = null; setStatus('released', null); }
      return r;
    }
    /* The last snapshot when the page hides: pending jobs are invalidated so no older position can
       overtake it, it goes out with keepalive, and nothing more is written until an explicit claim. */
    function final({position, duration, playing}) {
      lane++; queue = Promise.resolve();
      const result = send(`${base}/progress`, {position, duration, playing: playing === true}, {keepalive: true});
      if (fence) { fence = null; setStatus('suspended', null); }
      return result;
    }
    function dispose() { epoch++; lane++; fence = null; queue = Promise.resolve(); listeners.clear(); setStatus('disposed', conflict); }
    const subscribe = fn => { listeners.add(fn); return () => listeners.delete(fn); };
    return {state, claim, progress, heartbeat, release, final, dispose, subscribe, snapshot, get status() { return status; }, get device() { return device; }, get mediaKey() { return mediaKey; }};
  }
  /* The bootstrap advertises the capability; nothing else (health, a state 503) is a negotiation. */
  function negotiated(capabilities) {
    const c = capabilities && typeof capabilities === 'object' ? capabilities.playback_writer : null;
    return !!c && typeof c === 'object' && c.protocol === 'playback-writer/v2' && c.enabled === true;
  }
  /* A legacy progress reply that carries a durable fence: writing must stop for this media. */
  function legacyFenced(status, body) {
    return status === 409 && !!body && (body.reason === 'session_required' || body.reason === 'fenced');
  }
  const api = {create, negotiated, legacyFenced, sanitizeLabel, deviceLabel, classify, BASE};
  if (typeof module !== 'undefined') module.exports = api;
  else root.PlaybackWriter = api;
})(globalThis);
