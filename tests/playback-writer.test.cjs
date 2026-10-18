const test = require('node:test');
const assert = require('node:assert/strict');
const {create} = require('../static/playback-writer.js');

function transport(handlers) {
  const calls = [];
  const post = async (path, body, options) => { calls.push({path, body, options}); return handlers.post(path, body, calls.length); };
  const get = async path => { calls.push({path}); return handlers.get ? handlers.get(path) : {ok: true, status: 200, body: {generation: 0, active: false}}; };
  return {post, get, calls};
}
let seed = 0;
const base = () => ({mediaKey: 'media-1', session: 'session-a', device: 'Web · 客厅', random: n => 'r' + (++seed) + 'x'.repeat(n)});

test('opening a page never claims; Play claims with a fresh request id and token kept out of the UI snapshot', async () => {
  const t = transport({post: (path, body) => ({ok: true, status: 200, body: {ok: true, media_key: 'media-1', generation: 1, expires: 999}})});
  const w = create({...base(), ...t});
  assert.equal(w.status, 'idle');
  await w.state();
  assert.deepEqual(t.calls.map(c => c.path), ['/playback-sessions/state?media_key=media-1']);
  const r = await w.claim({mode: 'open'});
  assert.deepEqual(r, {ok: true, generation: 1, expires: 999});
  const claim = t.calls[1].body;
  assert.equal(claim.mode, 'open'); assert.equal(claim.expected_generation, 0);
  assert.match(claim.request_id, /^r\d+x+$/); assert.match(claim.writer_token, /^r\d+x{32}$/);
  assert.equal(JSON.stringify(w.snapshot()).includes(claim.writer_token), false);
  assert.equal(w.status, 'writing');
});

test('a live writer elsewhere is a visible conflict; takeover needs a user action, the seen generation and a new request id', async () => {
  let claims = 0;
  const t = transport({post: (path, body) => {
    claims++;
    if (claims === 1) return {ok: false, status: 409, body: {ok: false, reason: 'takeover_required', generation: 4, active: true, device_id: 'TV · 卧室'}};
    return {ok: true, status: 200, body: {ok: true, generation: 5, expires: 50}};
  }});
  const w = create({...base(), ...t});
  const first = await w.claim({mode: 'open'});
  assert.deepEqual(first, {ok: false, reason: 'takeover_required', generation: 4, device: 'TV · 卧室'});
  assert.equal(w.status, 'conflict'); assert.equal(w.snapshot().conflict.device, 'TV · 卧室');
  await assert.rejects(() => w.claim({mode: 'takeover'}), TypeError);          // the UI must pass the generation it showed
  const second = await w.claim({mode: 'takeover', expectedGeneration: 4});
  assert.deepEqual(second, {ok: true, generation: 5, expires: 50});
  assert.equal(t.calls[1].body.mode, 'takeover'); assert.equal(t.calls[1].body.expected_generation, 4);
  assert.notEqual(t.calls[1].body.request_id, t.calls[0].body.request_id);
  assert.notEqual(t.calls[1].body.writer_token, t.calls[0].body.writer_token);
});

test('a stale claim response cannot take over after a newer claim started', async () => {
  const pending = [];
  const t = transport({post: () => new Promise(resolve => pending.push(resolve))});
  const w = create({...base(), ...t});
  const slow = w.claim({mode: 'open'});
  const fast = w.claim({mode: 'open'});
  pending[1]({ok: true, status: 200, body: {ok: true, generation: 2, expires: 9}});
  assert.deepEqual(await fast, {ok: true, generation: 2, expires: 9});
  pending[0]({ok: true, status: 200, body: {ok: true, generation: 1, expires: 9}});   // late answer to the superseded claim
  assert.deepEqual(await slow, {ok: false, reason: 'superseded'});
  assert.equal(w.snapshot().generation, 2);
});

test('progress, heartbeat and release share one strictly increasing sequence under the claimed fence', async () => {
  const t = transport({post: (path, body) => ({ok: true, status: 200, body: path.endsWith('/claim') ? {ok: true, generation: 3, expires: 1} : {ok: true, generation: 3, sequence: body.sequence}})});
  const w = create({...base(), ...t});
  await w.claim();
  await w.progress({position: 10, duration: 100, playing: true});
  await w.heartbeat();
  await w.progress({position: 20, duration: 100, playing: false});
  await w.release({keepalive: true});
  const packets = t.calls.slice(1).map(c => c.body);
  assert.deepEqual(packets.map(p => p.sequence), [1, 2, 3, 4]);
  assert.ok(packets.every(p => p.generation === 3 && p.session === 'session-a' && p.writer_token === t.calls[0].body.writer_token));
  assert.equal(packets[0].playing, true); assert.equal(packets[2].playing, false);
  assert.deepEqual(t.calls[4].options, {keepalive: true});
  assert.equal(w.status, 'released');
});

test('fenced or expired stops writing, keeps the conflict visible and needs an explicit reclaim with a fresh sequence', async () => {
  let n = 0;
  const t = transport({post: path => {
    n++;
    if (path.endsWith('/claim')) return {ok: true, status: 200, body: {ok: true, generation: n, expires: 1}};
    return {ok: false, status: 409, body: {ok: false, ignored: true, reason: 'fenced', generation: 7, device_id: 'TV · 卧室'}};
  }});
  const w = create({...base(), ...t});
  await w.claim();
  const r = await w.progress({position: 1, duration: 2, playing: true});
  assert.deepEqual(r, {ok: false, reason: 'fenced'});
  assert.equal(w.status, 'fenced'); assert.equal(w.snapshot().conflict.device, 'TV · 卧室');
  assert.deepEqual(await w.heartbeat(), {ok: false, reason: 'fenced'});      // no background retries once fenced
  assert.equal(t.calls.length, 2);
  await w.claim({mode: 'takeover', expectedGeneration: 7});
  assert.equal(t.calls[2].body.expected_generation, 7);
  const after = await w.progress({position: 1, duration: 2, playing: true});
  assert.equal(after.reason, 'fenced');                                     // the fixture fences again; sequence restarted at 1
  assert.equal(t.calls[3].body.sequence, 1);
});

test('duplicate or older packets are harmless, other rejections surface, and a disabled server means unsupported', async () => {
  const answers = [{ok: true, status: 200, body: {ok: true, generation: 1, expires: 1}}, {ok: false, status: 409, body: {ok: false, ignored: true, reason: 'out_of_order'}},
                   {ok: false, status: 409, body: {ok: false, reason: 'generation_moved'}}, {ok: false, status: 503, body: {reason: 'disabled'}}];
  const t = transport({post: () => answers.shift()});
  const w = create({...base(), ...t});
  await w.claim();
  assert.deepEqual(await w.heartbeat(), {ok: true, ignored: true, reason: 'out_of_order'});
  assert.equal(w.status, 'writing');
  assert.deepEqual(await w.heartbeat(), {ok: false, reason: 'generation_moved'});
  assert.deepEqual(await w.heartbeat(), {ok: false, reason: 'disabled'});
  assert.equal(w.status, 'unsupported');
  assert.deepEqual(await w.progress({position: 0, duration: 1}), {ok: false, reason: 'unsupported'});
});

test('an older server without the capability leaves the client unsupported before any claim', async () => {
  const t = transport({post: () => { throw Error('must not be called'); }, get: () => ({ok: false, status: 503, body: {reason: 'disabled'}})});
  const w = create({...base(), ...t});
  assert.deepEqual(await w.state(), {supported: false});
  assert.equal(w.status, 'unsupported');
  assert.equal(t.calls.length, 1);
});

test('the client refuses to run without an identity or a transport', () => {
  assert.throws(() => create({post() {}, get() {}, mediaKey: 'm', session: 's'}), TypeError);
  assert.throws(() => create({post() {}, mediaKey: 'm', session: 's', device: 'd'}), TypeError);
});
