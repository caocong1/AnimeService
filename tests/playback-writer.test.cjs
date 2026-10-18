const test = require('node:test');
const assert = require('node:assert/strict');
const {create, negotiated, legacyFenced, sanitizeLabel, deviceLabel} = require('../static/playback-writer.js');

const KEY = 'finished:0123456789abcdef0123456789abcdef', SESSION = 'session-aaaaaaaaaaaaaaaa';
const ok = (generation, expires = 999, extra = {}) => ({ok: true, status: 200, body: {ok: true, media_key: KEY, requested_media_key: KEY, generation, expires, ...extra}});
const applied = (generation, sequence) => ({ok: true, status: 200, body: {ok: true, generation, sequence}});
const conflict = (reason, extra = {}) => ({ok: false, status: 409, body: {error: 'conflict', ok: false, reason, generation:0, active:false, expires:0, media_key: KEY, requested_media_key: KEY, ...extra}});
const stateBody = (generation, active = false, device = null) => ({ok: true, status: 200, body: {media_key: KEY, requested_media_key: KEY, generation, active, expires: 0, device_id: device}});
function transport(handlers) {
  const calls = [];
  const post = async (path, body, options) => { calls.push({path, body, options}); return handlers.post(path, body, calls.length); };
  const get = async path => { calls.push({path}); return handlers.get ? handlers.get(path, calls.length) : stateBody(0); };
  return {post, get, calls};
}
let seed = 0;
const base = () => ({mediaKey: KEY, session: SESSION, device: '网页播放器 7F3A', random: n => 'r' + String(++seed).padStart(3, '0') + 'x'.repeat(n), retries: 1});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const tick = () => new Promise(r => setImmediate(r));
const gated = () => { const gates = []; const t = transport({post: (path, body) => { const d = deferred(); gates.push({d, body, path}); return d.promise; }}); return {gates, ...t}; };
async function claimed(w, gates, generation) { const c = w.claim(); await tick(); gates.at(-1).d.resolve(ok(generation)); return c; }

test('negotiation only through the bootstrap capability; legacy fence detection; sanitized labels', () => {
  assert.equal(negotiated({playback_writer: {protocol: 'playback-writer/v2', enabled: true}}), true);
  assert.equal(negotiated({playback_writer: {protocol: 'playback-writer/v2', enabled: 'true'}}), false);
  assert.equal(negotiated({playback_writer: {protocol: 'playback-writer/v1', enabled: true}}), false);
  assert.equal(negotiated(undefined), false); assert.equal(negotiated({}), false);
  assert.equal(legacyFenced(409, {reason: 'session_required'}), true); assert.equal(legacyFenced(409, {reason: 'fenced'}), true);
  assert.equal(legacyFenced(409, {reason: 'other'}), false); assert.equal(legacyFenced(500, {reason: 'session_required'}), false); assert.equal(legacyFenced(409, null), false);
  assert.equal(sanitizeLabel(' 电视播放器 <b>X</b>\u0007​ '), '电视播放器 <b>X</b>');
  assert.equal(sanitizeLabel('x'.repeat(40)).length, 32); assert.equal(sanitizeLabel('\u0000‎'), null); assert.equal(sanitizeLabel(42), null);
  assert.match(deviceLabel('网页播放器', n => 'ab'.repeat(n)), /^网页播放器 ABAB$/);
});

test('the client refuses weak identities and transports', async () => {
  const t = transport({post: () => ok(1)});
  assert.throws(() => create({...base(), ...t, session: 'short'}), TypeError);
  assert.throws(() => create({...base(), ...t, device: '\u0000'}), TypeError);
  assert.throws(() => create({...base(), ...t, mediaKey: ''}), TypeError);
  assert.throws(() => create({...base(), post: t.post}), TypeError);
  await assert.rejects(() => create({...base(), ...t, random: n => 'ab'}).claim(), TypeError);
});

test('state never claims, remembers the server generation and requires the requested echo', async () => {
  const t = transport({post: () => ok(8), get: () => stateBody(7, true, 'TV · 卧室')});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.state(), {supported: true, generation: 7, active: true, device: 'TV · 卧室'});
  assert.equal(w.status, 'idle');
  await w.claim({mode: 'open'});
  assert.equal(t.calls[1].body.expected_generation, 7);
  const wrong = transport({get: () => ({ok: true, status: 200, body: {media_key: KEY, requested_media_key: 'other', generation: 1, active: false}})});
  const w2 = create({...base(), ...wrong});
  assert.equal((await w2.state()).reason, 'invalid_state'); assert.equal(w2.status, 'halted');
});

test('a successful claim needs ok, canonical media_key, requested echo, generation and finite expires', async () => {
  const answers = [{ok: false, status: 500, body: null}, {ok: true, status: 200, body: {ok: true}},
                   {ok: true, status: 200, body: {ok: true, media_key: 'other', requested_media_key: KEY, generation: 1, expires: 1}},
                   {ok: true, status: 200, body: {ok: true, media_key: KEY, requested_media_key: 'other', generation: 1, expires: 1}},
                   ok(1, Infinity), ok(1)];
  const t = transport({post: () => answers.shift()});
  const w = create({...base(), ...t, retries: 0});
  assert.equal((await w.claim()).reason, 'server_500'); assert.equal(w.status, 'error');
  for (const expected of ['protocol_200', 'protocol_200', 'protocol_200', 'protocol_200']) {
    const fresh = create({...base(), ...t});
    assert.equal((await fresh.claim()).reason, expected); assert.equal(fresh.status, 'halted');
    const count=t.calls.length;assert.equal((await fresh.claim()).reason,'halted');assert.equal(t.calls.length,count);
  }
  const fresh=create({...base(),...t});assert.equal((await fresh.claim()).ok, true); assert.equal(fresh.status, 'writing');
});

test('a save succeeds only with the packet\'s own generation and sequence; anything else stays unsaved', async () => {
  const answers = [ok(3), {ok: true, status: 200, body: {ok: true}}, applied(2, 2), applied(3, 9), applied(3, 1), {ok: false, status: 404, body: {detail: 'Not Found'}}, {ok: false, status: 500, body: null}];
  const t = transport({post: () => answers.shift()});
  const w = create({...base(), ...t});
  await w.claim();
  assert.equal((await w.progress({position: 1, duration: 9, playing: true})).reason, 'protocol_200'); assert.equal(w.status, 'halted');   // {ok:true} alone is not a save
  await assert.rejects(async () => { throw Error('x'); });
  const w2 = create({...base(), ...transport({post: (path, body, n) => n === 1 ? ok(3) : answers.shift()})});
  await w2.claim();
  assert.equal((await w2.heartbeat()).reason, 'protocol_200');                 // wrong generation
  assert.equal(w2.status, 'halted');
  const w3 = create({...base(), ...transport({post: (path, body, n) => n === 1 ? ok(3) : answers.shift()})});
  await w3.claim();
  assert.equal((await w3.heartbeat()).reason, 'protocol_200');                 // wrong sequence
  const w4 = create({...base(), ...transport({post: (path, body, n) => n === 1 ? ok(3) : answers.shift()})});
  await w4.claim();
  assert.deepEqual(await w4.progress({position: 2, duration: 9, playing: true}), {ok: true, sequence: 1});
  assert.equal((await w4.heartbeat()).reason, 'protocol_404'); assert.equal(w4.status, 'halted'); assert.equal(w4.snapshot().unsaved, true);
  const w5 = create({...base(), ...transport({post: (path, body, n) => n === 1 ? ok(3) : answers.shift()})});
  await w5.claim();
  assert.equal((await w5.heartbeat()).reason, 'server_500'); assert.equal(w5.status, 'writing'); assert.equal(w5.snapshot().unsaved, true);   // a server fault keeps the fence
});

test('a live writer elsewhere is a visible conflict; takeover needs the seen generation, a new request id and token; claims never double-send', async () => {
  let claims = 0;
  const t = transport({post: () => (++claims === 1 ? conflict('takeover_required', {generation: 4, active: true, device_id: 'TV · 卧室'}) : ok(5, 50))});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.claim(), {ok: false, reason: 'takeover_required', generation: 4, device: 'TV · 卧室'});
  assert.equal(w.status, 'conflict');
  await assert.rejects(() => w.claim({mode: 'takeover'}), TypeError);
  const [a, b] = await Promise.all([w.claim({mode: 'takeover', expectedGeneration: 4}), w.claim({mode: 'takeover', expectedGeneration: 4})]);
  assert.deepEqual(a, {ok: true, generation: 5, expires: 50}); assert.deepEqual(b, {ok: false, reason: 'claim_in_progress'});
  assert.equal(t.calls.length, 2);
  assert.notEqual(t.calls[1].body.request_id, t.calls[0].body.request_id); assert.notEqual(t.calls[1].body.writer_token, t.calls[0].body.writer_token);
});

test('a queued packet from an older fence is dropped, never re-bound to a new claim (owner repro 1)', async () => {
  const t = gated(); const w = create({...base(), ...t});
  await claimed(w, t.gates, 1);
  const p10 = w.progress({position: 10, duration: 100, playing: true}); await tick();     // in flight under g1 (seq 1)
  const p11 = w.progress({position: 11, duration: 100, playing: true});                  // queued under g1
  const second = claimed(w, t.gates, 2);                                                  // new claim g2 supersedes both
  assert.equal((await second).generation, 2);
  t.gates[1].d.resolve(applied(1, 1));                                                    // the old in-flight answer arrives late
  assert.deepEqual(await p10, {ok: false, reason: 'stale_response'});
  assert.deepEqual(await p11, {ok: false, reason: 'stale_response'});
  assert.equal(t.gates.length, 3);                                                        // p11 was never sent
  const fresh = w.progress({position: 12, duration: 100, playing: true}); await tick();
  assert.equal(t.gates[3].body.generation, 2); assert.equal(t.gates[3].body.sequence, 1); // the new fence starts at 1
  t.gates[3].d.resolve(applied(2, 1)); assert.deepEqual(await fresh, {ok: true, sequence: 1});
});

test('late answers of an older heartbeat, release or claim never touch the current fence', async () => {
  const t = gated(); const w = create({...base(), ...t});
  await claimed(w, t.gates, 1);
  const oldBeat = w.heartbeat(); await tick();
  await claimed(w, t.gates, 2);
  t.gates[1].d.resolve(conflict('fenced', {generation: 9, active: true, device_id: 'TV · 卧室'}));
  assert.deepEqual(await oldBeat, {ok: false, reason: 'stale_response'});
  assert.equal(w.status, 'writing'); assert.equal(w.snapshot().generation, 2);
  const oldRelease = w.release(); await tick();
  await claimed(w, t.gates, 3);
  t.gates[3].d.resolve(applied(2, 1));
  assert.deepEqual(await oldRelease, {ok: false, reason: 'stale_response'});
  assert.equal(w.status, 'writing'); assert.equal(w.snapshot().generation, 3);
});

test('disabled, 404 or authorization failures halt writing: no legacy fallback, no further packets, no auto claim (owner repro 3)', async () => {
  for (const [status, body, expected] of [[503, {reason: 'disabled'}, 'disabled_503'], [404, {detail: 'Not Found'}, 'protocol_404'], [401, {error: 'login'}, 'unauthorized_401']]) {
    const answers = [ok(1), {ok: false, status, body}];
    const t = transport({post: () => answers.shift() ?? (() => { throw Error('must not send'); })()});
    const w = create({...base(), ...t});
    await w.claim();
    assert.equal((await w.progress({position: 0, duration: 1, playing: true})).reason, expected);
    assert.equal(w.status, 'halted'); assert.equal(w.snapshot().unsaved, true);
    assert.deepEqual(await w.heartbeat(), {ok: false, reason: 'halted'});
    assert.deepEqual(await w.progress({position: 5, duration: 9}), {ok: false, reason: 'halted'});
    assert.equal(t.calls.length, 2);
  }
});

test('disposed is terminal: no state, claim or packet leaves afterwards, including jobs scheduled earlier (owner repro 4)', async () => {
  const t = gated(); const w = create({...base(), ...t});
  await claimed(w, t.gates, 1);
  const inflight = w.progress({position: 3, duration: 9, playing: true}); await tick();
  const queued = w.progress({position: 4, duration: 9, playing: true});
  w.dispose();
  t.gates[1].d.resolve(applied(1, 1));
  assert.deepEqual(await inflight, {ok: false, reason: 'stale_response'});
  assert.deepEqual(await queued, {ok: false, reason: 'stale_response'});
  assert.deepEqual(await w.claim(), {ok: false, reason: 'disposed'});
  assert.deepEqual(await w.state(), {supported: false, reason: 'disposed'});
  assert.deepEqual(await w.heartbeat(), {ok: false, reason: 'disposed'});
  assert.equal(t.gates.length, 2); assert.equal(w.status, 'disposed');
});

test('transport exceptions are network failures bound to their epoch (owner repro 5)', async () => {
  let n = 0;
  const t = transport({post: () => { n++; if (n === 1) throw Error('boom'); if (n === 2) return ok(1); throw Error('boom'); }, get: () => stateBody(1, true, '网页播放器 7F3A')});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.claim(), {ok: true, generation: 1, expires: 999});   // the same claim packet retried after the throw
  assert.equal(t.calls[0].body.request_id, t.calls[1].body.request_id); assert.ok(t.calls[2].path.includes('/state?'));   // retried: verified
  assert.deepEqual(await w.progress({position: 1, duration: 2, playing: true}), {ok: false, reason: 'network'});
  assert.equal(w.status, 'writing'); assert.equal(w.snapshot().unsaved, true);
  const g = gated(); const w2 = create({...base(), ...g});
  await claimed(w2, g.gates, 1);
  const p = w2.progress({position: 1, duration: 2, playing: true}); await tick();
  await claimed(w2, g.gates, 2);
  g.gates[1].d.resolve(Promise.reject(Error('late failure')));
  assert.deepEqual(await p, {ok: false, reason: 'stale_response'});
  assert.equal(w2.snapshot().unsaved, false);                                    // the new fence's state is untouched
});

test('the final snapshot invalidates pending jobs so no older position can follow it (owner repro 7)', async () => {
  const t = gated(); const w = create({...base(), ...t});
  await claimed(w, t.gates, 1);
  const p10 = w.progress({position: 10, duration: 100, playing: true}); await tick();      // seq 1 in flight
  const p20 = w.progress({position: 20, duration: 100, playing: true});                   // queued
  const last = w.final({position: 30, duration: 100, playing: false});                     // pagehide: seq 2, keepalive, no release
  await tick();
  assert.equal(t.gates.length, 3); assert.equal(t.gates[2].body.sequence, 2); assert.equal(t.gates[2].body.position, 30);
  assert.deepEqual(t.calls.at(-1).options, {keepalive: true});
  assert.equal(w.status, 'suspended');
  t.gates[1].d.resolve(applied(1, 1)); t.gates[2].d.resolve(applied(1, 2));
  assert.deepEqual(await p10, {ok: false, reason: 'stale_response'});                      // answered after the final: ignored
  assert.deepEqual(await p20, {ok: false, reason: 'stale_response'});                      // never sent
  await last; assert.equal(t.gates.length, 3);
  assert.deepEqual(await w.progress({position: 40, duration: 100, playing: true}), {ok: false, reason: 'suspended'});
  await claimed(w, t.gates, 2); assert.equal(w.status, 'writing');                          // an explicit Resume claims again
});

test('progress, heartbeat and release share one serialized sequence; out_of_order is not a save', async () => {
  const t = gated(); const w = create({...base(), ...t});
  await claimed(w, t.gates, 3);
  const a = w.progress({position: 10, duration: 100, playing: true}); const b = w.heartbeat(); const c = w.progress({position: 20, duration: 100, playing: false});
  await tick(); assert.equal(t.gates.length, 2);
  t.gates[1].d.resolve(applied(3, 1)); assert.deepEqual(await a, {ok: true, sequence: 1}); await tick();
  t.gates[2].d.resolve({ok: false, status: 409, body: {ok: false, ignored: true, reason: 'out_of_order'}});
  assert.deepEqual(await b, {ok: false, ignored: true, reason: 'out_of_order'}); assert.equal(w.snapshot().unsaved, true); await tick();
  t.gates[3].d.resolve(applied(3, 3)); assert.deepEqual(await c, {ok: true, sequence: 3}); assert.equal(w.snapshot().unsaved, false);
  const rel = w.release(); await tick(); t.gates[4].d.resolve(applied(3, 4)); assert.equal((await rel).ok, true); assert.equal(w.status, 'released');
  assert.ok(t.gates.slice(1).every(x => x.body.generation === 3 && x.body.session === SESSION && x.body.writer_token === t.gates[0].body.writer_token));
});

test('fenced, expired and session_required stop writing, keep the position and need an explicit reclaim', async () => {
  for (const reason of ['fenced', 'expired', 'session_required']) {
    let n = 0;
    const t = transport({post: path => path.endsWith('/claim') ? ok(++n) : conflict(reason, {generation: 7, active: true, device_id: '<script>TV</script>'})});
    const w = create({...base(), ...t});
    await w.claim();
    assert.deepEqual(await w.progress({position: 1, duration: 2, playing: true}), {ok: false, reason});
    assert.equal(w.status, reason === 'expired' ? 'expired' : 'fenced'); assert.equal(w.snapshot().conflict.device, '<script>TV</script>');
    assert.deepEqual(await w.heartbeat(), {ok: false, reason: w.status}); assert.equal(t.calls.length, 2);
    await w.claim({mode: 'takeover', expectedGeneration: 7});
    assert.equal(t.calls[2].body.expected_generation, 7);
    await w.progress({position: 1, duration: 2, playing: true}); assert.equal(t.calls[3].body.sequence, 1);
  }
});

test('retried or replayed claims are verified through state before they count', async () => {
  const good = transport({post: (path, body, n) => (n === 1 ? {ok: false, status: 0, body: null} : ok(2)), get: () => stateBody(2, true, '网页播放器 7F3A')});
  const w = create({...base(), ...good});
  assert.deepEqual(await w.claim(), {ok: true, generation: 2, expires: 999});
  assert.equal(good.calls[0].body.request_id, good.calls[1].body.request_id); assert.ok(good.calls[2].path.includes('/state?'));
  const replay = transport({post: () => ok(2, 999, {replayed: true}), get: () => stateBody(3, true, 'TV · 卧室')});
  const w2 = create({...base(), ...replay});
  assert.deepEqual(await w2.claim(), {ok: false, reason: 'verify_failed'});
  assert.equal(w2.status, 'stale'); assert.equal(w2.snapshot().conflict.device, 'TV · 卧室');
});

test('an older server without the capability leaves the client unsupported before any claim', async () => {
  const t = transport({post: () => { throw Error('must not be called'); }, get: () => ({ok: false, status: 503, body: {reason: 'disabled'}})});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.state(), {supported: false, reason: 'disabled_503'});
  assert.equal(w.status, 'unsupported'); assert.equal(t.calls.length, 1);
});
