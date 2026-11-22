'use strict';
// After a service restart the write token rotates: core.js post()/refreshToken() and watch.js's three POSTs recover once.
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const core = fs.readFileSync(path.join(__dirname, '../static/core.js'), 'utf8');
const watch = fs.readFileSync(path.join(__dirname, '../static/watch.js'), 'utf8');
const TOKEN = {error: '会话已更新，请刷新页面'};

function response(status, body) {
  const text = JSON.stringify(body);
  const make = () => ({status, ok: status >= 200 && status < 300, json: async () => JSON.parse(text), clone: make});
  return make();
}
/* A fake service: one token per process; `rotate()` is a restart. `next` overrides the next POST answer. */
function server() {
  const s = {token: 'old', boots: 0, posts: [], next: [], bootFails: 0, rotateOnBoot: false};
  s.fetch = async (url, init = {}) => {
    if (url === '/api/bootstrap') {
      s.boots++;
      if (s.bootFails) { s.bootFails--; throw new TypeError('Failed to fetch'); }
      const answer = response(200, {token: s.token, local: true, settings: {}, quarters: []});
      if (s.rotateOnBoot) { s.rotateOnBoot = false; s.token = 'newer'; }   // a second restart right after this bootstrap
      return answer;
    }
    if (init.method !== 'POST') return response(200, {});
    s.posts.push({url, token: init.headers['X-Anime-Token'], body: init.body, keepalive: init.keepalive});
    const forced = s.next.shift();
    if (forced instanceof Error) throw forced;
    if (forced) return response(...forced);
    return init.headers['X-Anime-Token'] === s.token ? response(200, {ok: true}) : response(403, TOKEN);
  };
  return s;
}
function load(s, extra = '') {
  const ctx = vm.createContext({console, Promise, Object, Error, TypeError, JSON, String, Number, Math, Date, setTimeout, clearTimeout,
    location: {pathname: '/watch', assign() {}}, document: {addEventListener() {}, querySelector: () => null}, matchMedia: () => ({matches: false}),
    Intl, addEventListener() {}, dispatchEvent() {}, CustomEvent: class {}});
  ctx.window = ctx; ctx.fetch = s.fetch;
  vm.runInContext(core + '\n' + extra + '\nglobalThis.__ = {api, boot, post, App};', ctx);
  return {ctx, ...ctx.__};
}

test('a write refused for its stale token is sent again once, unchanged, after one bootstrap', async () => {
  const s = server(), p = load(s); await p.boot();
  s.token = 'new';
  assert.equal((await p.api('/shows/1/watch', {episode: 1, finished: true})).ok, true);
  assert.equal(s.boots, 2);
  assert.deepEqual(s.posts.map(x => x.token), ['old', 'new']);
  assert.equal(s.posts[0].body, s.posts[1].body);
  assert.equal(p.App.token, 'new');
});

for (const [name, answer] of [
  ['a network failure', new TypeError('Failed to fetch')],
  ['a 503', [503, {error: '服务暂时异常'}]],
  ['a cross-site 403', [403, {error: '跨站请求被拒绝'}]],
  ['an untrusted-host 403', [403, {error: '访问地址不受信任'}]],
  ['a handler 403', [403, {detail: 'Bangumi 账号和同步设置请在家中电脑管理'}]],
  ['a remote-only 403', [403, {error: '此操作需要在家中电脑执行'}]],
]) {
  test(`${name} is never resent and keeps its message`, async () => {
    const s = server(), p = load(s); await p.boot();
    s.next.push(answer);
    await assert.rejects(p.api('/check', {}), e => answer instanceof Error ? e.message === 'Failed to fetch' : e.message === (answer[1].error || answer[1].detail));
    assert.equal(s.posts.length, 1); assert.equal(s.boots, 1);
  });
}

test('concurrent refusals share one bootstrap and each write is sent at most twice', async () => {
  const s = server(), p = load(s); await p.boot();
  s.token = 'new';
  const results = await Promise.all([p.api('/a', {}), p.api('/b', {}), p.api('/c', {})]);
  assert.ok(results.every(r => r.ok)); assert.equal(s.boots, 2); assert.equal(s.posts.length, 6);
});

test('a failed refresh surfaces the original refusal, and the next write tries the bootstrap again', async () => {
  const s = server(), p = load(s); await p.boot();
  s.token = 'new'; s.bootFails = 1;
  await assert.rejects(p.api('/a', {}), {message: '会话已更新，请刷新页面', status: 403});
  assert.equal(s.posts.length, 1);
  assert.equal((await p.api('/a', {})).ok, true);
  assert.equal(s.boots, 3); assert.deepEqual(s.posts.map(x => x.token), ['old', 'old', 'new']);
});

test('a second restart between refresh and resend ends after two attempts, no loop', async () => {
  const s = server(), p = load(s); await p.boot();
  s.token = 'new'; s.rotateOnBoot = true;
  await assert.rejects(p.api('/a', {}), {status: 403});
  assert.equal(s.posts.length, 2); assert.equal(s.boots, 2);
});

test('a structured reason from an updated server is honoured without the message', async () => {
  const s = server(), p = load(s); await p.boot();
  s.token = 'new'; s.next.push([403, {error: 'x', reason: 'token'}]);
  assert.equal((await p.api('/a', {})).ok, true); assert.equal(s.posts.length, 2);
});

/* watch.js's own viewingLease/saveProgress text, run with the real core. */
function slice(start, end) { const a = watch.indexOf(start), b = watch.indexOf(end, a); assert.ok(a >= 0 && b > a); return watch.slice(a, b + end.length); }
function player(release) {
  const s = server(), shown = [];
  const p = load(s, slice('  const viewingLease = ', ': Promise.resolve();') + '\n' + slice('  async function saveProgress(', '\n  }\n')
    + '\nglobalThis.__w = {viewingLease, saveProgress};');
  Object.assign(p.ctx, {id: 'a'.repeat(32), finishedEndpoint: release ? '/api/finished-viewing/rel-v0' : null, media: {viewing_session_supported: true},
    viewingSession: 'page-session-000000000001', sequence: 0, moved: true, progressQueue: Promise.resolve(), lastPosition: 0,
    audioSwitching: false, played: true, writerChosen: false, writer: null, legacyFenced: false, progressUnsaved: false,
    art: {duration: 1420, currentTime: 300, video: {paused: false}}, tracking: t => shown.push(t)});
  return {s, p, shown, ...p.ctx.__w};
}
for (const release of [false, true]) {
  test(`${release ? 'release' : 'ordinary'} player: after a restart the next lease and progress carry the new token`, async () => {
    const {s, p, shown, viewingLease, saveProgress} = player(release); await p.boot();
    await viewingLease(); await saveProgress();
    s.token = 'new';
    p.ctx.art.currentTime += 15; p.ctx.moved = true;
    await viewingLease(); await saveProgress();
    assert.deepEqual(s.posts.map(x => [x.url.replace(/[0-9a-f]{32}/, '<id>').split('/').pop(), x.token]),
      [['viewing-session', 'old'], ['progress', 'old'], ['viewing-session', 'old'], ['viewing-session', 'new'], ['progress', 'new']]);
    assert.equal(s.boots, 2);
    const seq = s.posts.filter(x => x.url.endsWith('/progress')).map(x => JSON.parse(x.body).sequence);
    assert.deepEqual(seq, [1, 2]);   // the stale-token packet never reached the server; the resend keeps its sequence
    if (release) assert.match(shown.at(-1), /续播进度已同步/);
    else assert.deepEqual(shown, []);
  });
}
test('ordinary player: a failure the client cannot repair is visible instead of console-only', async () => {
  const {s, p, shown, saveProgress} = player(false); await p.boot();
  s.next.push([503, {error: '服务暂时异常'}]);
  await saveProgress();
  assert.deepEqual(shown, ['进度未保存']); assert.equal(s.posts.length, 1);
});
test('the closing lease keeps keepalive on both attempts', async () => {
  const {s, p, viewingLease} = player(false); await p.boot();
  s.token = 'new';
  await viewingLease(true);
  assert.deepEqual(s.posts.map(x => x.keepalive), [true, true]);
  assert.equal(JSON.parse(s.posts[1].body).closed, true);
});
test('ordinary player: a later successful save clears the unsaved notice', async () => {
  const {s, p, shown, saveProgress} = player(false); await p.boot();
  s.next.push([503, {error: '服务暂时异常'}]);
  await saveProgress(); p.ctx.art.currentTime += 15; await saveProgress();
  assert.deepEqual(shown, ['进度未保存', '']); assert.equal(s.posts.length, 2);
});
