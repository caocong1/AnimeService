'use strict';
// A failed /api/bootstrap is not cached for the page lifetime: the next caller, retry button or refresh fetches it again.
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const read = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

/* The service answers /api/bootstrap with a 503 (or refuses connections) until `up` is set. */
function server(mode) {
  const s = {up: false, calls: []};
  s.fetch = async url => {
    s.calls.push(url);
    const json = (v, status = 200) => ({ok: status < 400, status, json: async () => v});
    if (mode === 'down' && !s.up) throw new TypeError('Failed to fetch');
    if (url === '/api/bootstrap') return s.up ? json({token: 'T', local: true, settings: {}, quarters: [], catalog_years: []}) : json({error: 'disk I/O error'}, 503);
    if (url === '/api/health') return json({heartbeat: {age: 5}, problems: {}});
    if (url.startsWith('/api/shows')) return json([]);
    if (url === '/api/home') return json({resume: []});
    if (url.startsWith('/api/source-review/subtitles/history')) return json({records: [], latest_only: true, history_versions: 0, note: ''});
    if (url === '/api/source-review/overview') return json({summary: {enrolled: 0, selected: 0, sampling: 0}, shows: []});
    return json({});
  };
  s.boots = () => s.calls.filter(u => u === '/api/bootstrap').length;
  return s;
}

function page(script, s) {
  const els = new Map(), docL = {}, intervals = [];
  const el = sel => {
    if (!els.has(sel)) els.set(sel, {
      sel, innerHTML: '', textContent: '', value: '', hidden: false, checked: false, open: false, href: '', title: '', tabIndex: 0,
      dataset: {}, lastElementChild: {textContent: ''}, classList: {add() {}, remove() {}, contains: () => false},
      setAttribute() {}, removeAttribute() {}, getAttribute: () => null, addEventListener() {}, append() {}, prepend() {}, focus() {}, querySelector: () => null,
    });
    return els.get(sel);
  };
  let n = 0;
  const document = {
    hidden: false, title: '', activeElement: null, querySelector: el, querySelectorAll: () => [],
    createElement: tag => el('<' + tag + '#' + (n++) + '>'), body: {prepend() {}, append() {}},
    addEventListener: (t, fn) => { (docL[t] ??= []).push(fn); },
  };
  const unhandled = [];
  const ctx = {
    console, Promise, Object, Error, TypeError, JSON, String, Number, Math, Date, Map, Set, Array, URL, URLSearchParams, Intl,
    setTimeout, clearTimeout, setImmediate, clearInterval() {},
    setInterval: (fn, ms) => { intervals.push({fn, ms, src: String(fn)}); return intervals.length; },
    location: {pathname: '/', search: '', hash: '', assign() {}, replace() {}}, history: {replaceState() {}, pushState() {}},
    document, matchMedia: () => ({matches: false}), addEventListener() {}, dispatchEvent() {}, CustomEvent: class {},
    FinishedReleases: {load: async () => [], applyList: l => l, observe: () => () => {}, applyBoard: (_s, b) => b},
  };
  ctx.window = ctx; ctx.fetch = s.fetch;
  vm.createContext(ctx);
  vm.runInContext(read('core.js') + '\n;globalThis.__core = {App, boot};', ctx, {filename: 'core.js'});
  if (script) vm.runInContext(read(script), ctx, {filename: script});
  return {el, intervals, docL, unhandled, core: ctx.__core};
}
const text = h => h.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

for (const mode of ['down', 'bootstrap503']) {
  test(`${mode}: the next boot() fetches the bootstrap again after a failure`, async () => {
    const s = server(mode), p = page(null, s);
    await assert.rejects(p.core.boot());
    s.up = true;
    assert.equal((await p.core.boot()).token, 'T');
    assert.equal((await p.core.boot()).token, 'T');
    assert.equal(s.boots(), 2);
  });

  test(`${mode}: home registers the health interval and its retry renders the list once the service answers`, async () => {
    const s = server(mode), p = page('home.js', s);
    await settle();
    assert.match(text(p.el('#screens').innerHTML), /连不上后台/);
    assert.equal(p.intervals.filter(i => i.ms === 60000 && i.src.includes('poll()')).length, 1);
    s.up = true;
    p.docL.click.forEach(fn => fn({target: {closest: sel => sel === '#retry' ? {} : null}}));
    await settle();
    assert.doesNotMatch(text(p.el('#screens').innerHTML), /连不上后台/);
    assert.equal(s.boots(), 2);
  });

  test(`${mode}: home's background refresh recovers after a failed first load`, async () => {
    const s = server(mode), p = page('home.js', s);
    await settle();
    s.up = true;
    for (const i of p.intervals.filter(i => i.ms === 60000 && !i.src.includes('poll()'))) i.fn();
    await settle();
    assert.doesNotMatch(text(p.el('#screens').innerHTML), /连不上后台/);
  });
}

test('subtitle reviews read their history even when the bootstrap fails, with no unhandled rejection', async () => {
  const unhandled = [], onUnhandled = r => unhandled.push(r);
  process.on('unhandledRejection', onUnhandled);
  try {
    const s = server('bootstrap503'), p = page('subtitle-reviews.js', s);
    await settle();
    assert.equal(s.calls.filter(u => u.startsWith('/api/source-review/subtitles/history')).length, 1);
    assert.match(p.el('#summary').textContent, /0 条当前记录/);
    await settle();
    assert.deepEqual(unhandled, []);
  } finally { process.off('unhandledRejection', onUnhandled); }
});
