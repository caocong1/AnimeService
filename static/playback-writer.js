/* Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md, owner client contract round 17).
   A pure state machine over an injected transport; the page wires it in only after the bootstrap
   advertises capabilities.playback_writer. What it guarantees:
   - only an explicit Play/Resume claims; reading state never does;
   - a live writer elsewhere is a visible conflict; takeover needs a user action, a NEW request id and
     token, and the generation the user actually saw; a response to a superseded claim never wins;
   - every response is bound to the epoch/media/session/generation it was sent under: late answers from
     an old claim, an old release or an old heartbeat cannot touch the current fence;
   - only a 2xx with a valid body succeeds; 409 carries structured reasons; 500/403/bad JSON never do;
   - progress, heartbeat and release share one strictly increasing sequence; out_of_order is "not
     applied", never "saved"; fenced/expired/session_required stop writing and keep the local position;
   - a retried claim that succeeds is re-verified through state before it counts as ownership;
   - once v2 is chosen nothing here ever falls back to the legacy progress route. */
(function(root) {
  'use strict';
  const BASE = '/api/playback-sessions';
  const MAX_SEQUENCE = 2147483647, LABEL_MAX = 32;
  const UNSAFE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}]/gu;
  const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  function defaultRandom(n) {
    const bytes = new Uint8Array(n);
    (globalThis.crypto || root.crypto).getRandomValues(bytes);
    return hex(bytes);
  }
  const safeInt = (v, min = 0) => Number.isSafeInteger(v) && v >= min ? v : null;
  const text = v => typeof v === 'string' ? v : null;
  /* Device labels are display text only: strip control/format characters, trim, cap at 32 code points. */
  function sanitizeLabel(value, max = LABEL_MAX) {
    const clean = typeof value === 'string' ? value.replace(UNSAFE, '').trim() : '';
    return clean ? Array.from(clean).slice(0, max).join('') : null;
  }
  function deviceLabel(prefix, random = defaultRandom) {
    return sanitizeLabel(`${prefix} ${random(2).toUpperCase()}`);
  }
  function create(options) {
    const {post, get, mediaKey, session, random = defaultRandom, base = BASE, retries = 2} = options;
    const device = sanitizeLabel(options.device);
    if (typeof post !== 'function' || typeof get !== 'function') throw new TypeError('post and get transports are required');
    if (!mediaKey || typeof session !== 'string' || session.length < 16 || !device) throw new TypeError('mediaKey, a session of at least 16 characters and a device label are required');
    let status = 'idle', epoch = 0, fence = null, sequence = 0, conflict = null, lastError = null, unsaved = false;
    let observed = {generation: 0, active: false, device: null};
    let queue = Promise.resolve();
    const listeners = new Set();
    const snapshot = () => ({status, generation: fence?.generation ?? conflict?.generation ?? observed.generation, expires: fence?.expires ?? null,
                             conflict: conflict ? {...conflict} : null, writing: status === 'writing', unsaved, error: lastError});
    const notify = () => { for (const fn of listeners) fn(snapshot()); };
    const setStatus = (next, nextConflict = conflict) => { status = next; conflict = nextConflict; notify(); };
    const live = ticket => ticket.epoch === epoch && status !== 'disposed';
    /* Only a 2xx with a JSON object body is a success; everything else is classified, never trusted. */
    const classify = r => {
      const body = r && typeof r.body === 'object' && r.body !== null ? r.body : null;
      if (!r || r.status === 0) return {kind: 'network', body};
      if (r.status === 503 || body?.reason === 'disabled') return {kind: 'disabled', body};
      if (r.status === 409 && body && typeof body.reason === 'string') return {kind: 'conflict', body};
      if (r.ok && r.status >= 200 && r.status < 300 && body) return {kind: 'ok', body};
      return {kind: 'http', body, http: r.status};
    };
    const fail = (ticket, reason, http) => {
      if (!live(ticket)) return {ok: false, reason: 'stale_response'};
      lastError = http ? `${reason}_${http}` : reason;
      unsaved = true;
      // A chosen v2 never falls back to legacy: disabled/http/network stop the claim path visibly.
      setStatus(fence ? 'writing' : 'error');
      return {ok: false, reason: lastError};
    };
    async function state() {
      const ticket = {epoch};
      const r = await get(`${base}/state?media_key=${encodeURIComponent(mediaKey)}`);
      const {kind, body} = classify(r);
      if (!live(ticket)) return {supported: false, reason: 'stale_response'};
      if (kind === 'disabled') { setStatus(fence ? 'error' : 'unsupported', null); return {supported: false, reason: 'disabled'}; }
      if (kind !== 'ok' || body.media_key !== mediaKey || safeInt(body.generation) === null || typeof body.active !== 'boolean') {
        return {supported: false, reason: kind === 'ok' ? 'invalid_state' : kind};
      }
      observed = {generation: body.generation, active: body.active, device: body.active ? sanitizeLabel(body.device_id) : null};
      notify();
      return {supported: true, generation: observed.generation, active: observed.active, device: observed.device};
    }
    const packetIds = () => ({request_id: random(16), writer_token: random(32)});
    /* mode 'open' for an explicit Play/Resume; 'takeover' only from a visible user action with the generation shown. */
    async function claim({mode = 'open', expectedGeneration} = {}) {
      if (mode !== 'open' && mode !== 'takeover') throw new TypeError('mode must be open or takeover');
      if (mode === 'takeover' && safeInt(expectedGeneration) === null) throw new TypeError('takeover needs the generation the user saw');
      const expected = safeInt(expectedGeneration) ?? snapshot().generation;
      const ids = packetIds();
      if (ids.request_id.length < 16 || ids.writer_token.length < 32 || ids.writer_token.length > 128) throw new TypeError('random identifiers are too short');
      const request = {media_key: mediaKey, session, ...ids, device_id: device, expected_generation: expected, mode};
      const ticket = {epoch: ++epoch};          // supersedes every earlier claim and every packet of the old fence
      fence = null; sequence = 0; queue = Promise.resolve(); lastError = null;
      let r, attempt = 0;
      do {                                       // a network failure retries the SAME packet, never a second unknown claim
        attempt++;
        r = await post(`${base}/claim`, request, {});
      } while (classify(r).kind === 'network' && attempt <= retries && live(ticket));
      if (!live(ticket)) return {ok: false, reason: 'superseded'};
      const {kind, body, http} = classify(r);
      if (kind === 'conflict' || (kind === 'ok' && body.ok === false)) {
        conflict = {reason: body.reason || 'conflict', generation: safeInt(body.generation) ?? 0, device: body.active ? sanitizeLabel(body.device_id) : null};
        observed = {generation: conflict.generation, active: body.active === true, device: conflict.device};
        setStatus('conflict', conflict);
        return {ok: false, reason: conflict.reason, generation: conflict.generation, device: conflict.device};
      }
      if (kind !== 'ok') return fail(ticket, kind === 'disabled' ? 'disabled' : kind, http);
      if (body.ok !== true || body.media_key !== mediaKey || safeInt(body.generation, 1) === null || typeof body.expires !== 'number' || !Number.isFinite(body.expires)) {
        return fail(ticket, 'invalid_claim');
      }
      if (attempt > 1) {                         // a replayed result is history: confirm the writer is still ours
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
    }
    function enqueue(run, keepalive) {
      if (keepalive) return run();              // the final packet on pagehide bypasses the queue
      const next = queue.then(run, run);
      queue = next.catch(() => {});
      return next;
    }
    function send(path, extra, options = {}) {
      return enqueue(async () => {
        if (status !== 'writing' || !fence) return {ok: false, reason: status === 'idle' ? 'no_claim' : status};
        if (sequence >= MAX_SEQUENCE) return {ok: false, reason: 'sequence_exhausted'};
        const ticket = {epoch, generation: fence.generation};
        const packet = Object.freeze({media_key: mediaKey, session, generation: fence.generation, writer_token: fence.token, sequence: ++sequence, ...extra});
        const r = await post(path, packet, {keepalive: options.keepalive === true});
        const {kind, body, http} = classify(r);
        if (!live(ticket) || !fence || fence.generation !== ticket.generation) return {ok: false, reason: 'stale_response'};
        if (kind === 'disabled') return fail(ticket, 'disabled');
        if (kind === 'conflict' || (kind === 'ok' && (body.ignored || body.ok === false))) {
          const reason = body.reason || 'rejected';
          if (reason === 'fenced' || reason === 'expired' || reason === 'session_required') {
            fence = null; unsaved = true;      // another writer owns progress now (or ours lapsed): keep the local position
            setStatus(reason === 'expired' ? 'expired' : 'fenced', {reason, generation: safeInt(body.generation) ?? ticket.generation, device: body.active ? sanitizeLabel(body.device_id) : null});
            return {ok: false, reason};
          }
          if (body.ignored) return {ok: false, ignored: true, reason};   // out_of_order etc.: this packet was not applied
          return fail(ticket, reason);
        }
        if (kind !== 'ok') return fail(ticket, kind, http);
        if (body.ok !== true) return fail(ticket, 'invalid_reply');
        unsaved = false; lastError = null; notify();
        return {ok: true, sequence: packet.sequence};
      }, options.keepalive === true);
    }
    const progress = ({position, duration, playing}, options) => send(`${base}/progress`, {position, duration, playing: playing === true}, options);
    const heartbeat = options => send(`${base}/heartbeat`, {}, options);
    async function release(options) {
      const r = await send(`${base}/release`, {}, options);
      if (r.ok) { fence = null; setStatus('released', null); }
      return r;
    }
    function dispose() { epoch++; fence = null; queue = Promise.resolve(); setStatus('disposed', conflict); }
    const subscribe = fn => { listeners.add(fn); return () => listeners.delete(fn); };
    return {state, claim, progress, heartbeat, release, dispose, subscribe, snapshot, get status() { return status; }, get device() { return device; }};
  }
  /* The bootstrap advertises the capability; nothing else (health, a state 503) is a negotiation. */
  function negotiated(capabilities) {
    const c = capabilities && typeof capabilities === 'object' ? capabilities.playback_writer : null;
    return !!c && typeof c === 'object' && c.protocol === 'playback-writer/v2' && c.enabled === true;
  }
  const api = {create, negotiated, sanitizeLabel, deviceLabel, BASE};
  if (typeof module !== 'undefined') module.exports = api;
  else root.PlaybackWriter = api;
})(globalThis);
