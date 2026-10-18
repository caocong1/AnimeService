/* Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md).
   Pure state machine over an injected transport; nothing here runs unless the page wires it in and
   the server advertises the capability. Rules it enforces for the UI:
   - only an explicit Play/Resume claims; opening a page never does;
   - a live writer elsewhere is a visible conflict: takeover needs a user action, a NEW request id and
     the generation the user actually saw; stale responses never take over by themselves;
   - progress, heartbeat and release share one strictly increasing sequence under one fence;
   - fenced or expired means stop writing, keep the local position, offer an explicit reclaim;
   - a new claim never replays packets queued under the old fence. */
(function(root) {
  'use strict';
  const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  function defaultRandom(n) {
    const bytes = new Uint8Array(n);
    (globalThis.crypto || root.crypto).getRandomValues(bytes);
    return hex(bytes);
  }
  function create(options) {
    const {post, get, mediaKey, session, device} = options;
    const random = options.random || defaultRandom, now = options.now || Date.now;
    if (typeof post !== 'function' || typeof get !== 'function') throw new TypeError('post and get transports are required');
    if (!mediaKey || !session || !device) throw new TypeError('mediaKey, session and device are required');
    let status = 'idle', fence = null, sequence = 0, lastResult = null, conflict = null, inflight = null;
    const listeners = new Set();
    const notify = () => { for (const fn of listeners) fn(snapshot()); };
    const snapshot = () => ({status, generation: fence?.generation ?? conflict?.generation ?? 0, expires: fence?.expires ?? null,
                             conflict: conflict ? {...conflict} : null, writing: status === 'writing'});
    function setStatus(next, extra) { status = next; if (extra !== undefined) conflict = extra; notify(); }
    async function state() {
      const r = await get(`/playback-sessions/state?media_key=${encodeURIComponent(mediaKey)}`);
      if (r.status === 503 || r.reason === 'disabled') { setStatus('unsupported', null); return {supported: false}; }
      if (!r.ok && r.status) throw Error('playback state unavailable');
      return {supported: true, ...r.body};
    }
    /* mode 'open' for Play/Resume; 'takeover' only from a visible user action with the generation shown. */
    async function claim({mode = 'open', expectedGeneration} = {}) {
      if (mode === 'takeover' && !Number.isInteger(expectedGeneration)) throw new TypeError('takeover needs the generation the user saw');
      const expected = Number.isInteger(expectedGeneration) ? expectedGeneration : (lastResult?.generation ?? conflict?.generation ?? 0);
      const request = {media_key: mediaKey, session, request_id: random(16), writer_token: random(32), device_id: device,
                       expected_generation: expected, mode};
      const attempt = post('/playback-sessions/claim', request, {retry: 'same-request'});
      inflight = attempt;
      const r = await attempt;
      if (inflight !== attempt) return {ok: false, reason: 'superseded'};  // a newer claim replaced this one
      inflight = null;
      if (r.status === 503 || r.body?.reason === 'disabled') { setStatus('unsupported', null); return {ok: false, reason: 'disabled'}; }
      const body = r.body || {};
      if (r.status === 400 || !r.ok && !body.reason) { setStatus('error', null); return {ok: false, reason: 'invalid_request'}; }
      if (body.ok === false) {
        lastResult = body;
        conflict = {reason: body.reason, generation: body.generation ?? 0, device: body.device_id ?? null, expires: body.expires ?? null};
        setStatus('conflict', conflict);
        return {ok: false, reason: body.reason, generation: conflict.generation, device: conflict.device};
      }
      // A fresh fence: packets queued under the old one are never replayed.
      fence = {generation: body.generation, token: request.writer_token, expires: body.expires ?? null};
      sequence = 0; lastResult = body;
      setStatus('writing', null);
      return {ok: true, generation: fence.generation, expires: fence.expires};
    }
    function packet(extra) {
      return {media_key: mediaKey, session, generation: fence.generation, writer_token: fence.token, sequence: ++sequence, ...extra};
    }
    async function send(path, extra, options) {
      if (status !== 'writing' || !fence) return {ok: false, reason: status === 'idle' ? 'no_claim' : status};
      const r = await post(path, packet(extra), options);
      const body = r.body || {};
      if (r.status === 503 || body.reason === 'disabled') { setStatus('unsupported', null); return {ok: false, reason: 'disabled'}; }
      if (body.ignored && (body.reason === 'fenced' || body.reason === 'expired')) {
        // Another writer owns progress now (or ours lapsed): stop writing, keep the local position.
        fence = null;
        setStatus(body.reason, {reason: body.reason, generation: body.generation ?? snapshot().generation, device: body.device_id ?? null, expires: null});
        return {ok: false, reason: body.reason};
      }
      if (body.ignored) return {ok: true, ignored: true, reason: body.reason};  // duplicate/older packet: harmless
      if (body.ok === false) return {ok: false, reason: body.reason || 'rejected'};
      return {ok: true, sequence: body.sequence ?? sequence};
    }
    const progress = ({position, duration, playing}, options) => send('/playback-sessions/progress', {position, duration, playing: playing === true}, options);
    const heartbeat = options => send('/playback-sessions/heartbeat', {}, options);
    async function release(options) {
      const r = await send('/playback-sessions/release', {}, options);
      if (r.ok || r.reason === 'no_claim') { fence = null; if (status === 'writing') setStatus('released', null); }
      return r;
    }
    const subscribe = fn => { listeners.add(fn); return () => listeners.delete(fn); };
    return {state, claim, progress, heartbeat, release, subscribe, get status() { return status; }, snapshot};
  }
  const api = {create};
  if (typeof module !== 'undefined') module.exports = api;
  else root.PlaybackWriter = api;
})(globalThis);
