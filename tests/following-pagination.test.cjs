const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

const STATES = {wish: '想看', trial: '试看', watching: '在追', paused: '暂搁', dropped: '弃番', completed: '看完'};
const rows = Array.from({length: 45}, (_, i) => ({id: i + 1, title: `作品${i + 1}`, original: `Anime ${i + 1}`,
  state: i % 3 === 0 ? 'completed' : 'watching', watched: 0, total: 12, unwatched: 0, eps: [], air_date: ''}));
const settle = () => new Promise(resolve => setImmediate(resolve));
// The page's clock: tests advance it instead of waiting; timers are recorded, never started, and fired by the test.
const clock = {now: 1_700_000_000_000};
class PageDate extends Date { static now() { return clock.now; } }

async function page(search = '?view=all', fail = false) {
  const elements = new Map(), events = {}, pagers = [{}, {}], calls = [], timers = [];
  function $(selector) {
    if (!elements.has(selector)) elements.set(selector, {hidden: false, value: '', innerHTML: '', textContent: '',
      addEventListener(name, fn) { this[name] = fn; }, focus() {}});
    return elements.get(selector);
  }
  const location = {pathname: '/', search, hash: ''};
  const document = {hidden: false, querySelectorAll: () => pagers, addEventListener(name, fn) { events[name] = fn; }};
  const context = vm.createContext({URLSearchParams, Set, Promise, console, location, STATES, $, calls, document, Date: PageDate,
    setInterval(fn, ms) { timers.push({fn, ms}); return timers.length; }, clearInterval() {},
    history: Object.fromEntries(['pushState', 'replaceState'].map(k => [k, (_, __, url) => {location.search = new URL(url, 'http://test').search;}])),
    window: {scrollTo() {}}, addEventListener(name, fn) {events[name] = fn;},
    shell(name) {calls.push(name);}, boot: async () => {}, applyListAiringSchedules: async list => list,
    FinishedReleases: {load: async () => [], applyList: list => list, observe: () => () => {}},
    api: async path => { calls.push(path); if (fail) throw Error('offline'); return path === '/home' ? {resume: []} : rows.slice(); },
    esc: s => String(s ?? ''), stateChip: () => '', lineColour: () => '', cover: () => '',
    epStrip: () => '', ticket: () => '', icon: () => '', today: () => '2026-10-10', ranges: () => '', WEEK: [],
  });
  vm.runInContext(fs.readFileSync('static/home.js', 'utf8'), context);
  await settle();
  return {$, events, pagers, location, calls, context, timers, document,
    requests() {return calls.filter(c => c.startsWith('/')).length;},
    click(selector, values) {events.click({target: {closest: s => s === selector ? values : null}});},
    ids() {return [...$('#shelf').innerHTML.matchAll(/class="name" href="\/show\/(\d+)"/g)].map(m => Number(m[1]));}};
}

test('independent menu paginates all states without overlaps, and clamps the final page', async () => {
  const p = await page();
  assert.equal(p.calls[0], 'following');
  assert.equal(p.$('#watch-board').hidden, true);
  assert.equal(p.calls.includes('/home'), false);
  assert.deepEqual(p.ids(), Array.from({length: 20}, (_, i) => i + 1));
  p.click('[data-page]', {dataset: {page: '2'}});
  assert.deepEqual(p.ids(), Array.from({length: 20}, (_, i) => i + 21));
  assert.match(p.location.search, /page=2/);
  p.click('[data-page]', {dataset: {page: '3'}});
  assert.deepEqual(p.ids(), [41, 42, 43, 44, 45]);
  assert.match(p.pagers[0].innerHTML, /data-page="4" disabled/);
  const restored = await page('?view=all&page=2');
  assert.deepEqual(restored.ids(), Array.from({length: 20}, (_, i) => i + 21));
});

test('state, search and page-size changes reset pagination; IME intermediate text does not filter', async () => {
  const p = await page('?view=all&page=3');
  p.click('[data-f]', {dataset: {f: 'completed'}});
  assert.equal(p.ids().length, 15);
  assert.equal(p.location.search.includes('page='), false);
  p.$('#q').input({isComposing: true, target: {value: '作品1'}});
  assert.equal(p.ids().length, 15);
  p.$('#q').compositionend({target: {value: '作品1'}});
  assert.deepEqual(p.ids(), [1, 10, 13, 16, 19]);
  p.click('#clear', {});
  p.events.change({target: {id: 'page-size-0', value: '40', matches: () => true}});
  assert.equal(p.ids().length, 40);
  assert.match(p.location.search, /size=40/);
  assert.match(p.pagers[1].innerHTML, /value="40" selected/);
});

test('invalid parameters and stale pages normalize; empty results hide paging and remain recoverable', async () => {
  const p = await page('?view=all&page=999&size=invalid&state=invalid');
  assert.deepEqual(p.ids(), [41, 42, 43, 44, 45]);
  assert.equal(p.location.search, '?view=all&page=3');
  p.$('#q').input({target: {value: '没有此番'}});
  assert.deepEqual(p.ids(), []);
  assert.equal(p.pagers[0].hidden, true);
  p.click('#clear', {});
  assert.equal(p.ids().length, 20);
  assert.equal(p.pagers[0].hidden, false);
  const invalid = await page('?view=all&page=-2');
  assert.equal(invalid.ids()[0], 1);
});

test('browser history restores both page and filter', async () => {
  const p = await page();
  p.location.search = '?view=all&page=2'; p.events.popstate();
  assert.equal(p.ids()[0], 21);
  p.location.search = '?view=all&state=completed'; p.events.popstate();
  assert.equal(p.ids().length, 15);
});

test('home keeps the shelf hidden and the existing watch board visible', async () => {
  const p = await page('');
  assert.equal(p.calls[0], 'home');
  assert.equal(p.$('#shelf-section').hidden, true);
  assert.equal(p.$('#watch-board').hidden, false);
  assert.equal(p.calls.includes('/home'), true);
  assert.equal(p.location.search, '');
});

test('retry after a list request failure restores the independent list view', async () => {
  const p = await page('?view=all', true);
  assert.equal(p.$('#watch-board').hidden, false);
  p.context.api = async () => rows.slice();
  p.click('#retry', {}); await settle();
  assert.equal(p.$('#watch-board').hidden, true);
  assert.equal(p.$('#shelf-section').hidden, false);
  assert.equal(p.ids().length, 20);
});

test('states without shows stay visible but disabled, and a requested empty state stays selectable', async () => {
  const p = await page('?view=all');
  const html = p.$('#state-filter').innerHTML;
  assert.match(html, /data-f="watching" aria-pressed="false">在追 <span class="num">30<\/span>/);
  assert.match(html, /data-f="dropped" aria-pressed="false" disabled>弃番 <span class="num">0<\/span>/);
  assert.doesNotMatch(html, /data-f="" aria-pressed="false" disabled/);
  const empty = await page('?view=all&state=dropped');
  assert.match(empty.$('#state-filter').innerHTML, /data-f="dropped" aria-pressed="true">弃番/);
  assert.equal(empty.ids().length, 0);
  empty.click('[data-f]', {dataset: {f: 'completed'}});
  assert.equal(empty.ids().length, 15);
});

test('background refresh reloads on the timer, focus or visibility, at most once per 10 s, never hidden or while loading', async () => {
  const p = await page();
  const before = p.requests();
  assert.deepEqual(p.timers.map(t => t.ms), [60000]);
  p.timers[0].fn(); p.events.focus(); p.events.visibilitychange(); await settle();
  assert.equal(p.requests(), before);                                        // just loaded: throttled
  clock.now += 10001;
  p.timers[0].fn(); await settle();
  assert.equal(p.requests(), before + 1);                                    // the timer reloads
  p.events.focus(); await settle();
  assert.equal(p.requests(), before + 1);                                    // and starts a new 10 s window
  clock.now += 10001; p.document.hidden = true;
  p.timers[0].fn(); p.events.visibilitychange(); await settle();
  assert.equal(p.requests(), before + 1);                                    // hidden: nothing
  p.document.hidden = false; p.events.visibilitychange(); await settle();
  assert.equal(p.requests(), before + 2);                                    // visible again: reload
  clock.now += 10001;
  let release;
  p.context.api = async path => { p.calls.push(path); await new Promise(resolve => { release = resolve; }); return rows.slice(); };
  p.events.focus(); p.timers[0].fn(); p.events.visibilitychange(); await settle();
  assert.equal(p.requests(), before + 3);                                    // one load in flight, no second one
  release(); await settle();
  assert.equal(p.ids().length, 20);
});

test('a failed background refresh keeps the list, filter and page; a successful one keeps filter and page too', async () => {
  const p = await page('?view=all&page=2&state=watching');
  const ids = p.ids(), search = p.location.search;
  assert.equal(ids.length, 10);
  clock.now += 10001;
  p.context.api = async path => { p.calls.push(path); throw Error('offline'); };
  p.timers[0].fn(); await settle();
  assert.deepEqual(p.ids(), ids);                                            // the shelf stays
  assert.equal(p.location.search, search);
  assert.equal(p.$('#shelf-section').hidden, false);
  assert.equal(p.$('#watch-board').hidden, true);                            // no error board for a background failure
  assert.equal(p.$('#screens').innerHTML, '');
  clock.now += 10001;
  p.context.api = async path => { p.calls.push(path); return rows.slice(); };
  p.events.focus(); await settle();
  assert.deepEqual(p.ids(), ids);
  assert.equal(p.location.search, search);
  p.context.api = async path => { p.calls.push(path); throw Error('offline'); };
  p.click('#retry', {}); await settle();                                     // a user-initiated load still reports its failure
  assert.equal(p.$('#watch-board').hidden, false);
  assert.match(p.$('#screens').innerHTML, /连不上后台/);
});
