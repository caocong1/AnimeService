const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({
  addEventListener() {}, window: { fetch: async () => {} },
  document: { addEventListener() {} }, matchMedia: () => ({ matches: false }),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/core.js'), 'utf8'), context);

test('future schedule corrects stale missing inference without touching real media or watches', () => {
  const detail = { show: { total: 5 },
    airings: [{ episode: 1, airdate: '2026-10-13' }, { episode: 2, airdate: '2026-10-20' }],
    episodes: [{ episode: 1 }, { episode: 4 }], watches: [{ episode: 5, finished: 1 }],
    board: { eps: [
      { n: 1, s: 'ready', media: 'abc' }, { n: 2, s: 'missing' },
      { n: 3, s: 'aired' }, { n: 4, s: 'missing' }, { n: 5, s: 'watched' }, { n: 13, s: 'missing' }
    ] }, gaps: { missing: [2, 3, 4, 13] }
  };
  context.applyAiringSchedule(detail, '2026-10-14');
  assert.deepEqual(Array.from(detail.board.eps, e => e.s), ['ready', 'future', 'future', 'missing', 'watched']);
  assert.equal(detail.board.eps[1].date, '2026-10-20');
  assert.deepEqual(detail.gaps.missing, [4]);
  assert.equal(detail.board.eps[0].media, 'abc');
});

test('aired dates and date-free responses keep their existing evidence', () => {
  const detail = { show: { total: 3 }, episodes: [], watches: [],
    airings: [{ episode: 2, airdate: '2026-10-20' }, { episode: 3, airdate: '2026-10-13' }],
    board: { eps: [{ n: 2, s: 'missing' }, { n: 3, s: 'missing' }] }, gaps: { missing: [2, 3] } };
  context.applyAiringSchedule(detail, '2026-10-14');
  assert.equal(detail.board.eps[1].s, 'missing');
  assert.deepEqual(detail.gaps.missing, [3]);
  const unknown = { board: { eps: [{ n: 1, s: 'aired' }] } };
  assert.equal(context.applyAiringSchedule(unknown, '2026-10-14'), unknown);
  assert.equal(unknown.board.eps[0].s, 'aired');
});

test('old list response reuses the corrected detail board before computing missing summaries', async () => {
  const list = [{ id: 584847, eps: [{ n: 1, s: 'ready' }, { n: 2, s: 'missing' }], next: { n: 1 } }];
  let calls = 0;
  const detail = { show: { total: 2 }, episodes: [{ episode: 1 }], watches: [],
    airings: [{ episode: 2, airdate: '2999-10-20' }],
    board: { eps: list[0].eps, next: { n: 1, s: 'ready', media: 'abc' } } };
  await context.applyListAiringSchedules(list, async id => { calls++; assert.equal(id, 584847); return detail; });
  assert.equal(calls, 1);
  assert.equal(list[0].eps[1].s, 'future');
  assert.equal(list[0].eps[1].date, '2999-10-20');
  assert.equal(list[0].eps.filter(e => e.s === 'missing').length, 0);
  assert.equal(list[0].next.media, 'abc');
});

test('corrected backend lists and lists without missing entries need no detail requests', async () => {
  const list = [{ id: 1, airing_schedule_version: 1, eps: [{ n: 1, s: 'missing' }] },
    { id: 2, eps: [{ n: 1, s: 'ready' }] }];
  await context.applyListAiringSchedules(list, async () => { throw Error('Unexpected detail request'); });
  assert.equal(list[0].eps[0].s, 'missing');
});

test('today() is the local calendar date, not the UTC date', () => {
  const local = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  const today = vm.runInContext('today()', context);  // a top-level const is not a context property
  assert.match(today, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(today, local);
  const shifted = new Date(Date.UTC(2026, 9, 17, 18, 30));  // 18:30 UTC is already the 18th in UTC+8
  const expected = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Asia/Shanghai' }).format(shifted);
  assert.equal(expected, '2026-10-18');
});
