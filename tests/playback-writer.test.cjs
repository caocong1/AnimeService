const test = require('node:test');
const assert = require('node:assert/strict');
const {create, negotiated, sanitizeLabel, deviceLabel} = require('../static/playback-writer.js');

const SESSION = 'session-aaaaaaaaaaaaaaaa';
const ok = (generation, expires = 999) => ({ok: true, status: 200, body: {ok: true, media_key: 'media-1', generation, expires}});
const applied = sequence => ({ok: true, status: 200, body: {ok: true, generation: 1, sequence}});
const conflict = (reason, extra = {}) => ({ok: false, status: 409, body: {error: 'conflict', ok: false, reason, ...extra}});
function transport(handlers) {
  const calls = [];
  const post = async (path, body, options) => { calls.push({path, body, options}); return handlers.post(path, body, calls.length); };
  const get = async path => { calls.push({path}); return handlers.get ? handlers.get(path, calls.length) : {ok: true, status: 200, body: {media_key: 'media-1', generation: 0, active: false, expires: 0, device_id: null}}; };
  return {post, get, calls};
}
let seed = 0;
const base = () => ({mediaKey: 'media-1', session: SESSION, device: '网页播放器 7F3A', random: n => 'r' + String(++seed).padStart(3, '0') + 'x'.repeat(n), retries: 1});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const tick = () => new Promise(r => setImmediate(r));   // lets the serial queue issue the next packet

test('negotiation only through the bootstrap capability; labels are sanitized display text', () => {
  assert.equal(negotiated({playback_writer: {protocol: 'playback-writer/v2', enabled: true}}), true);
  assert.equal(negotiated({playback_writer: {protocol: 'playback-writer/v2', enabled: 'true'}}), false);
  assert.equal(negotiated({playback_writer: {protocol: 'playback-writer/v1', enabled: true}}), false);
  assert.equal(negotiated(undefined), false); assert.equal(negotiated({}), false);
  assert.equal(sanitizeLabel(' 电视播放器 <b>X</b>\u0007​ '), '电视播放器 <b>X</b>');   // text only; control/format stripped, rendering escapes
  assert.equal(sanitizeLabel('x'.repeat(40)).length, 32); assert.equal(sanitizeLabel('\u0000‎'), null); assert.equal(sanitizeLabel(42), null);
  assert.match(deviceLabel('网页播放器', n => 'ab'.repeat(n)), /^网页播放器 ABAB$/);
});

test('the client refuses weak identities and transports', () => {
  const t = transport({post: () => ok(1)});
  assert.throws(() => create({...base(), ...t, session: 'short'}), TypeError);
  assert.throws(() => create({...base(), ...t, device: '\u0000'}), TypeError);
  assert.throws(() => create({...base(), post: t.post}), TypeError);
  return assert.rejects(() => create({...base(), ...t, random: n => 'ab'}).claim(), TypeError);   // weak identifiers never reach the wire
});

test('reading state never claims and remembers the server generation for the next open', async () => {
  const t = transport({post: () => ok(8), get: () => ({ok: true, status: 200, body: {media_key: 'media-1', generation: 7, active: true, expires: 5, device_id: 'TV · 卧室'}})});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.state(), {supported: true, generation: 7, active: true, device: 'TV · 卧室'});
  assert.equal(w.status, 'idle'); assert.equal(w.snapshot().generation, 7);
  await w.claim({mode: 'open'});
  assert.equal(t.calls[1].body.expected_generation, 7);                       // Codex finding 3: not 0
  assert.equal(t.calls[1].body.mode, 'open');
});

test('an explicit Play claims with fresh identifiers and keeps the token out of the snapshot', async () => {
  const t = transport({post: () => ok(1)});
  const w = create({...base(), ...t});
  const r = await w.claim();
  assert.deepEqual(r, {ok: true, generation: 1, expires: 999});
  const claim = t.calls[0].body;
  assert.equal(claim.device_id, '网页播放器 7F3A'); assert.ok(claim.request_id.length >= 16); assert.ok(claim.writer_token.length >= 32);
  assert.equal(JSON.stringify(w.snapshot()).includes(claim.writer_token), false);
  assert.equal(w.status, 'writing');
});

test('success needs a 2xx with a valid body: 500/403/404 without reason, empty or bad bodies never succeed', async () => {
  const answers = [{ok: false, status: 500, body: null}, {ok: false, status: 403, body: {}}, {ok: true, status: 200, body: {ok: true}},
                   {ok: true, status: 200, body: {ok: true, media_key: 'other', generation: 1, expires: 1}}, ok(1), {ok: false, status: 404, body: {detail: 'Not Found'}}];
  const t = transport({post: () => answers.shift()});
  const w = create({...base(), ...t});
  assert.equal((await w.claim()).reason, 'http_500'); assert.equal(w.status, 'error');
  assert.equal((await w.claim()).reason, 'http_403');
  assert.equal((await w.claim()).reason, 'invalid_claim');
  assert.equal((await w.claim()).reason, 'invalid_claim');
  assert.equal((await w.claim()).ok, true);
  const p = await w.progress({position: 1, duration: 2, playing: true});
  assert.deepEqual(p, {ok: false, reason: 'http_404'});                      // Codex finding 2: not a success
  assert.equal(w.status, 'writing'); assert.equal(w.snapshot().unsaved, true); assert.equal(w.snapshot().error, 'http_404');
});

test('a live writer elsewhere is a visible conflict; takeover needs the seen generation, a new request id and token', async () => {
  let claims = 0;
  const t = transport({post: () => (++claims === 1 ? conflict('takeover_required', {generation: 4, active: true, device_id: 'TV · 卧室'}) : ok(5, 50))});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.claim(), {ok: false, reason: 'takeover_required', generation: 4, device: 'TV · 卧室'});
  assert.equal(w.status, 'conflict'); assert.equal(w.snapshot().conflict.device, 'TV · 卧室');
  await assert.rejects(() => w.claim({mode: 'takeover'}), TypeError);
  assert.deepEqual(await w.claim({mode: 'takeover', expectedGeneration: 4}), {ok: true, generation: 5, expires: 50});
  assert.equal(t.calls[1].body.mode, 'takeover'); assert.equal(t.calls[1].body.expected_generation, 4);
  assert.notEqual(t.calls[1].body.request_id, t.calls[0].body.request_id); assert.notEqual(t.calls[1].body.writer_token, t.calls[0].body.writer_token);
});

test('late answers from an older heartbeat, release or claim never touch the current fence', async () => {
  const gates = [];
  const t = transport({post: () => { const d = deferred(); gates.push(d); return d.promise; }});
  const w = create({...base(), ...t});
  const first = w.claim(); gates[0].resolve(ok(1)); assert.equal((await first).generation, 1);
  const oldBeat = w.heartbeat(); await tick();                     // heartbeat under fence 1, answered later
  const second = w.claim(); gates[2].resolve(ok(2)); assert.equal((await second).generation, 2);   // claim B supersedes it
  gates[1].resolve(conflict('fenced', {generation: 9, active: true, device_id: 'TV · 卧室'}));     // Codex finding 1a
  assert.deepEqual(await oldBeat, {ok: false, reason: 'stale_response'});
  assert.equal(w.status, 'writing'); assert.equal(w.snapshot().generation, 2);
  const oldRelease = w.release(); await tick();                    // release under fence 2, answered later
  const third = w.claim(); gates[4].resolve(ok(3)); assert.equal((await third).generation, 3);
  gates[3].resolve(applied(1));                                                                   // Codex finding 1b
  assert.deepEqual(await oldRelease, {ok: false, reason: 'stale_response'});
  assert.equal(w.status, 'writing'); assert.equal(w.snapshot().generation, 3);
  const slow = w.claim(); const fast = w.claim();                  // claim D answered after claim E won
  gates[6].resolve(ok(5)); assert.equal((await fast).generation, 5);
  gates[5].resolve(ok(4)); assert.deepEqual(await slow, {ok: false, reason: 'superseded'});
  assert.equal(w.snapshot().generation, 5);
});

test('disposal makes every pending answer inert', async () => {
  const gates = [];
  const t = transport({post: () => { const d = deferred(); gates.push(d); return d.promise; }});
  const w = create({...base(), ...t});
  const c = w.claim(); gates[0].resolve(ok(1)); await c;
  const p = w.progress({position: 3, duration: 9, playing: true}); await tick();
  w.dispose();
  gates[1].resolve(applied(1));
  assert.deepEqual(await p, {ok: false, reason: 'stale_response'});
  assert.equal(w.status, 'disposed');
  assert.deepEqual(await w.heartbeat(), {ok: false, reason: 'disposed'});
});

test('progress, heartbeat and release share one sequence, are serialized, and out_of_order is not a save', async () => {
  const gates = [];
  const t = transport({post: (path, body) => { if (path.endsWith('/claim')) return ok(3); const d = deferred(); gates.push({d, body}); return d.promise; }});
  const w = create({...base(), ...t});
  await w.claim();
  const a = w.progress({position: 10, duration: 100, playing: true});
  const b = w.heartbeat();
  const c = w.progress({position: 20, duration: 100, playing: false});
  await tick();
  assert.equal(gates.length, 1);                                               // serialized: the next packet waits
  gates[0].d.resolve(applied(1)); assert.deepEqual(await a, {ok: true, sequence: 1}); await tick();
  gates[1].d.resolve({ok: false, status: 409, body: {ok: false, ignored: true, reason: 'out_of_order'}});
  assert.deepEqual(await b, {ok: false, ignored: true, reason: 'out_of_order'}); await tick();
  assert.equal(w.status, 'writing');
  gates[2].d.resolve(applied(3)); assert.deepEqual(await c, {ok: true, sequence: 3});
  const last = w.progress({position: 30, duration: 100, playing: true}, {keepalive: true});   // final packet: bypasses the queue
  assert.equal(gates.length, 4); assert.deepEqual(gates[3].body.sequence, 4); assert.deepEqual(t.calls.at(-1).options, {keepalive: true});
  gates[3].d.resolve(applied(4)); await last;
  const rel = w.release(); await tick(); gates[4].d.resolve(applied(5)); await rel;
  assert.equal(w.status, 'released');
  assert.ok(t.calls.slice(1).every(x => x.body.generation === 3 && x.body.session === SESSION && x.body.writer_token === t.calls[0].body.writer_token));
});

test('fenced, expired and session_required stop writing, keep the position and need an explicit reclaim', async () => {
  for (const reason of ['fenced', 'expired', 'session_required']) {
    let n = 0;
    const t = transport({post: path => path.endsWith('/claim') ? ok(++n) : conflict(reason, {generation: 7, active: true, device_id: '<script>TV</script>'})});
    const w = create({...base(), ...t});
    await w.claim();
    assert.deepEqual(await w.progress({position: 1, duration: 2, playing: true}), {ok: false, reason});
    assert.equal(w.status, reason === 'expired' ? 'expired' : 'fenced'); assert.equal(w.snapshot().unsaved, true);
    assert.equal(w.snapshot().conflict.device, '<script>TV</script>');          // plain text for the UI to escape
    assert.deepEqual(await w.heartbeat(), {ok: false, reason: w.status});     // no background retries
    assert.equal(t.calls.length, 2);
    await w.claim({mode: 'takeover', expectedGeneration: 7});
    assert.equal(t.calls[2].body.expected_generation, 7);
    await w.progress({position: 1, duration: 2, playing: true});
    assert.equal(t.calls[3].body.sequence, 1);                                 // a new fence restarts the sequence
  }
});

test('a retried claim is verified through state before it counts as ownership', async () => {
  const good = transport({post: (path, body, n) => (n === 1 ? {ok: false, status: 0, body: null} : ok(2)),
                          get: () => ({ok: true, status: 200, body: {media_key: 'media-1', generation: 2, active: true, expires: 9, device_id: '网页播放器 7F3A'}})});
  const w = create({...base(), ...good});
  assert.deepEqual(await w.claim(), {ok: true, generation: 2, expires: 999});
  assert.equal(good.calls[0].body.request_id, good.calls[1].body.request_id);  // the SAME packet was retried
  assert.equal(good.calls[2].path.includes('/state?'), true);
  const stale = transport({post: (path, body, n) => (n === 1 ? {ok: false, status: 0, body: null} : ok(2)),
                           get: () => ({ok: true, status: 200, body: {media_key: 'media-1', generation: 3, active: true, expires: 9, device_id: 'TV · 卧室'}})});
  const w2 = create({...base(), ...stale});
  assert.deepEqual(await w2.claim(), {ok: false, reason: 'verify_failed'});
  assert.equal(w2.status, 'stale'); assert.equal(w2.snapshot().conflict.device, 'TV · 卧室');
  assert.deepEqual(await w2.progress({position: 0, duration: 1}), {ok: false, reason: 'stale'});
});

test('disabled after v2 was chosen is a visible failure, never a legacy fallback', async () => {
  const answers = [ok(1), {ok: false, status: 503, body: {error: 'playback writer disabled', reason: 'disabled'}}];
  const t = transport({post: () => answers.shift()});
  const w = create({...base(), ...t});
  await w.claim();
  assert.deepEqual(await w.progress({position: 0, duration: 1, playing: true}), {ok: false, reason: 'disabled'});
  assert.equal(w.snapshot().unsaved, true); assert.equal(w.snapshot().error, 'disabled');
  const w2 = create({...base(), ...transport({post: () => { throw Error('no claim expected'); }, get: () => ({ok: false, status: 503, body: {reason: 'disabled'}})})});
  assert.deepEqual(await w2.state(), {supported: false, reason: 'disabled'});
  assert.equal(w2.status, 'unsupported');
});
