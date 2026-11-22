'use strict';
// 放映室 startup: independent reads start together, so the player is built after at most 2 request waves,
// while every failure keeps today's precedence. Runs the real core.js, finished-releases.js and danmu
// helpers in a vm with a stubbed DOM and a fetch that answers every pending request of one wave together.
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const ROOT = path.join(__dirname, '..');
const settle = () => new Promise(r => setImmediate(r));
const SHA = 'a'.repeat(64);

function anyEl(name) {
  const t = function () {};
  t.__name = name;
  return new Proxy(t, {
    get: (o, k) => {
      if (k in o) return o[k];
      if (k === 'children') return [];
      if (k === 'lastElementChild' || k === 'firstElementChild') return (o[k] = anyEl(name + '>' + String(k)));
      if (k === 'dataset') return (o.dataset = {});
      if (k === 'style') return (o.style = {setProperty() {}});
      if (k === 'classList') return {add() {}, remove() {}, toggle() {}, contains: () => false};
      if (['addEventListener', 'removeEventListener', 'append', 'prepend', 'focus', 'setAttribute', 'removeAttribute',
        'toggleAttribute', 'showModal', 'close', 'remove', 'replaceChildren'].includes(k)) return () => {};
      if (k === 'querySelector' || k === 'closest') return () => null;
      if (k === 'querySelectorAll') return () => [];
      if (k === 'matches') return () => false;
      if (k === Symbol.toPrimitive) return () => '';
      return '';
    },
    set: (o, k, v) => { o[k] = v; return true; },
    apply: () => undefined,
  });
}

function run(file, scenario) {
  const mode = scenario.mode;
  const releaseId = 'rel-1', mediaKey = mode === 'release' ? 'finished:' + releaseId : 'abc';
  let wave = 0, pending = [], playerWave = null, playerUrl = null, playerMs = null, navigations = [];
  const log = [], rejections = [];
  const onRejection = reason => rejections.push(String(reason && reason.message || reason));
  process.on('unhandledRejection', onRejection);
  const els = new Map();
  const el = s => { if (!els.has(s)) els.set(s, anyEl(s)); return els.get(s); };
  const release = {id: releaseId, status: 'finished', show_id: 5, season: 1, episode: 2, version: 1, edition: 'v0',
    duration_seconds: 1400, video_sha256: SHA, title: 'T'};
  const defaults = {
    '/api/bootstrap': {status: 200, body: {token: 't', local: true, settings: {}, quarters: []}},
    '/api/health': {status: 200, body: {}},
    '/api/web/alignment-capabilities': {status: 200, body: {sites: ['bilibili', 'bahamut'], bahamut_reference: 'bounded-hls-v1'}},
    '/static/finished-media/index.json': {status: 200, body: {releases: mode === 'release' ? [release] : []}},
    '/api/finished-viewing': {status: 200, body: {releases: mode === 'release' ? [{id: releaseId, show_id: 5, season: 1, episode: 2,
      video_sha256: SHA, media_id: 'finished:' + 'b'.repeat(32), progress: null, finished: false, show_state: 'watching'}] : []}},
    ['/api/finished-viewing/' + releaseId]: {status: 200, body: {id: releaseId, show_id: 5, season: 1, episode: 2, video_sha256: SHA,
      media_id: 'finished:' + 'b'.repeat(32), progress: null, finished: false, show_state: 'watching'}},
    ['/api/web/media/' + mediaKey]: {status: 200, body: {id: mediaKey, name: mode === 'release' ? releaseId + '.mp4' : 'x.mkv', folder: 'F',
      show_id: 5, episode: 2, progress: null, viewing_session_supported: true,
      ...(mode === 'release' ? {release_id: releaseId, release, finished: false} : {})}},
    '/api/shows/5': {status: 200, body: {show: {id: 5, title: 'T', mapping: {season: 1}}, board: {eps: [{n: 2, s: 'ready', media: 'abc'}], next: null}}},
  };
  const answer = (url, method) => {
    const p = url.split('?')[0];
    const a = (scenario.answers || {})[p] || defaults[p];
    if (a) return a;
    if (method === 'POST') return {status: 200, body: {ok: true}};
    if (p.endsWith('/danmu')) return {status: 200, body: {status: 'unmatched', candidates: []}};
    if (p.endsWith('/subtitles')) return {status: 200, body: {tracks: [], default: null}};
    if (p.endsWith('/audio-tracks')) return {status: 200, body: {tracks: [], supported: false, fingerprint: ''}};
    if (p.endsWith('/picture')) return {status: 200, body: {width: 1920, height: 1080}};
    return {status: 200, body: {}};
  };
  const response = a => ({ok: a.status >= 200 && a.status < 300, status: a.status, json: async () => a.body,
    headers: {get: k => k.toLowerCase() === 'content-type' ? 'application/json' : null}});
  const fetchStub = (url, init = {}) => {
    const method = init.method || 'GET';
    log.push([wave, method, url]);
    // Timed mode: each request answers after RTT + simulated server time, in real time.
    if (scenario.delay) return new Promise(resolve => setTimeout(() => resolve(response(answer(url, method))), scenario.delay(url.split('?')[0], method)));
    return new Promise(resolve => pending.push(() => resolve(response(answer(url, method)))));
  };
  const started = performance.now();
  const storage = scenario.storage || {};
  const ctx = {
    console, Promise, Map, Set, WeakMap, URL, URLSearchParams, AbortController, Date, JSON, Math, Number, String, Array, Object, Error,
    Symbol, Proxy, Reflect, Uint8Array, Intl, CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o?.detail; } },
    location: {search: mode === 'release' ? '?release=' + releaseId : '?media=abc', pathname: '/watch',
      replace(u) { navigations.push(u); }, assign(u) { navigations.push(u); }},
    document: {hidden: false, activeElement: null, addEventListener() {}, querySelector: s => el(s), querySelectorAll: () => [],
      createElement: t => anyEl('<' + t + '>'), body: anyEl('body'), documentElement: anyEl('html'), fullscreenElement: null},
    addEventListener() {}, dispatchEvent() {}, setInterval: () => 0, clearInterval() {},
    setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 1)), clearTimeout, requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    crypto: {randomUUID: () => '0123456789abcdef0123456789abcdef', getRandomValues: a => a},
    localStorage: {getItem: k => Object.hasOwn(storage, k) ? storage[k] : null, setItem() {}},
    CSS: {escape: s => s}, getComputedStyle: () => ({getPropertyValue: () => '#000'}), matchMedia: () => ({matches: false}),
    innerWidth: 1440, innerHeight: 1000, fetch: fetchStub,
    PlaybackWriter: {sanitizeLabel: () => null, deviceLabel: () => 'w'},
    ClipExport: {mount: () => ({close() {}})}, PictureQuality: {mount: () => ({close() {}})}, Screenshot: {}, BiliUid: {uidCandidates: () => []},
    SubtitleRenderer: class { select() { return Promise.resolve(); } }, artplayerPluginDanmuku: () => ({}),
  };
  ctx.Artplayer = function (options) {
    playerWave = wave; playerUrl = options.url; playerMs = performance.now() - started;
    return {on() {}, template: {$player: anyEl('player'), $video: anyEl('video'), $danmuku: anyEl('danmuku'), $subtitle: anyEl('subtitle')},
      controls: {add() {}}, setting: {find: () => null, add() {}, update() {}, remove() {}},
      plugins: {artplayerPluginDanmuku: {option: {}, config() {}, load: async () => {}, isHide: false, reset() {}}},
      video: {paused: true, error: null}, notice: {}, subtitle: {}, icons: {}, duration: 1400};
  };
  ctx.Artplayer.utils = {secondToTime: () => ''};
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const f of ['core.js', 'danmu-timing.js', 'danmu-retry.js', 'danmu-alignment.js', 'danmu-match.js', 'finished-releases.js'])
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'static', f), 'utf8'), ctx, {filename: f});
  vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, {filename: path.basename(file)});
  return (async () => {
    if (scenario.delay) {
      while (playerWave === null && performance.now() - started < 5000) await new Promise(r => setTimeout(r, 2));
      process.off('unhandledRejection', onRejection);
      return {playerMs, playerUrl, rejections};
    }
    for (let i = 0; i < 12; i++) {
      await settle(); await settle();
      const batch = pending; pending = []; wave++; batch.forEach(f => f());
      await settle(); await settle();
      const screen = els.get('#screen-state');
      if (playerWave !== null && i > playerWave + 1) break;
      if (screen && screen.innerHTML && playerWave === null && pending.length === 0) break;
    }
    await new Promise(r => setTimeout(r, 20));
    process.off('unhandledRejection', onRejection);
    const before = log.filter(([w]) => playerWave === null || w < playerWave);
    const get = (s, k) => els.has(s) ? els.get(s)[k] : undefined;
    return {
      playerWave, playerUrl,
      wavesBeforePlayer: playerWave,
      requestsBeforePlayer: before.map(([w, m, u]) => `${w}:${m === 'GET' ? '' : m + ' '}${u}`),
      screen: String(get('#screen-state', 'innerHTML') || ''),
      tracking: String(get('#tracking-status', 'textContent') || ''),
      clipDisabled: get('#open-clip', 'disabled') === true,
      danmuSelected: String(get('#danmu-selected', 'innerHTML') || ''),
      navigations, rejections,
    };
  })();
}

const WATCH = path.join(ROOT, 'static/watch.js');
const outcome = r => ({player: r.playerWave !== null, url: r.playerUrl, screen: r.screen.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(),
  tracking: r.tracking, clipDisabled: r.clipDisabled, bahamutWaiting: r.danmuSelected.includes('巴哈姆特自动对轴等待服务空闲更新'),
  login: r.navigations.includes('/login'), rejections: r.rejections});
const played = (url, extra = {}) => ({player: true, url, screen: '', tracking: '', clipDisabled: false, bahamutWaiting: false, login: false, rejections: [], ...extra});
const failed = (screen, extra = {}) => ({player: false, url: null, screen: screen + ' 去片库', tracking: '', clipDisabled: false, bahamutWaiting: false, login: false, rejections: [], ...extra});
const bahamut = JSON.stringify([{id: 'bh', source_identity: 'bh-1', title: '第2集', site: 'bahamut'}]);
const MEDIA = '/api/web/media/abc/stream', RELEASE = '/static/finished-media/rel-1/episode.mp4';
const signedOut = {status: 401, body: {error: '请登录这台设备'}};
const cases = [
  ['media ok', {mode: 'media'}, 2, played(MEDIA)],
  ['release ok', {mode: 'release'}, 2, played(RELEASE)],
  ['media: an unreadable release index is optional', {mode: 'media', answers: {'/static/finished-media/index.json': {status: 500, body: {}}}},
    2, played(MEDIA, {tracking: '成品清单暂时无法读取'})],
  ['release: an unreadable release index is fatal', {mode: 'release', answers: {'/static/finished-media/index.json': {status: 500, body: {}}}},
    null, failed('打不开这个文件 成品清单暂时无法读取')],
  ['release: a media 404 falls back to the verified release record', {mode: 'release', answers: {'/api/web/media/finished:rel-1': {status: 404, body: {detail: '未找到已盘点的媒体'}}}},
    3, played(RELEASE, {clipDisabled: true})],
  ['release: a media 404 with a SHA mismatch is refused', {mode: 'release', answers: {'/api/web/media/finished:rel-1': {status: 404, body: {detail: 'x'}},
    '/api/finished-viewing/rel-1': {status: 200, body: {show_id: 5, episode: 2, video_sha256: 'c'.repeat(64), progress: null, finished: false}}}},
    null, failed('打不开这个文件 成品版本无法核验，请刷新后重试')],
  ['release: a media 503 is fatal', {mode: 'release', answers: {'/api/web/media/finished:rel-1': {status: 503, body: {error: '服务暂时异常'}}}},
    null, failed('打不开这个文件 服务暂时异常')],
  ['media: a media 404 shows not found', {mode: 'media', answers: {'/api/web/media/abc': {status: 404, body: {detail: '未找到已盘点的媒体'}}}},
    null, failed('找不到这个文件 未找到已盘点的媒体')],
  ['media: a capabilities 404 keeps bahamut waiting', {mode: 'media', storage: {'fanyu-danmu-sources:abc': bahamut},
    answers: {'/api/web/alignment-capabilities': {status: 404, body: {detail: 'Not Found'}}}}, 2, played(MEDIA, {bahamutWaiting: true})],
  ['media: advertised bahamut alignment is used', {mode: 'media', storage: {'fanyu-danmu-sources:abc': bahamut}}, 2, played(MEDIA)],
  ['media: a malformed capabilities body is ignored', {mode: 'media', answers: {'/api/web/alignment-capabilities': {status: 200, body: null}}}, 2, played(MEDIA)],
  ['media: a bootstrap 503 is fatal', {mode: 'media', answers: {'/api/bootstrap': {status: 503, body: {error: '服务暂时异常'}}}},
    null, failed('打不开这个文件 服务暂时异常')],
  ['media: a signed-out device goes to /login', {mode: 'media', answers: {'/api/bootstrap': signedOut, '/api/web/media/abc': signedOut,
    '/api/web/alignment-capabilities': signedOut, '/api/finished-viewing': signedOut}}, null, failed('打不开这个文件 请登录这台设备', {login: true})],
];
for (const [name, scenario, waves, expected] of cases) {
  test(name, async () => {
    const r = await run(WATCH, scenario);
    assert.deepEqual(outcome(r), expected);
    assert.equal(r.wavesBeforePlayer, waves);
  });
}
