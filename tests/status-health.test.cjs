const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const context = vm.createContext({
  addEventListener() {},
  window: { fetch: async () => {} },
  document: { addEventListener() {} },
  matchMedia: () => ({ matches: false }),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/core.js'), 'utf8'), context);
const problems = (tasks, events) => context.statusProblems({ tasks, events }, 10000);
const error = { level: 'error', scope: 'task:abc', time: 9999, message: 'old generic error' };

test('quarantined task keeps review warning without stale network error', () => {
  const result = problems([{ hash: 'abc', status: 'review', error: 'ownership conflict' }], [error]);
  assert.equal(result.error, undefined);
  assert.equal(result.reviews.length, 1);
  assert.equal(result.reviews[0].error, 'ownership conflict');
});

test('recovered/completed tasks do not keep health in error while history remains', () => {
  for (const status of ['complete', 'cleaned', 'linked', 'downloading']) {
    assert.equal(problems([{ hash: 'abc', status, error: '' }], [error]).error, undefined);
  }
});

test('review count uses the complete review list rather than recent history', () => {
  const reviews = [{ hash: 'older', status: 'review', error: 'uncertain submit' }];
  const result = context.statusProblems({ tasks: [], events: [], task_reviews: reviews }, 10000);
  assert.equal(result.reviews.length, 1);
  assert.equal(result.reviews[0].hash, 'older');
  assert.equal(context.statusProblems({ tasks: reviews, events: [], task_reviews: [] }, 10000).reviews.length, 0);
});

test('uncertain submissions use a concise summary and preserve raw task evidence', () => {
  const reviews = [{ hash: 'a', submission_uncertain: 1, error: 'Computer Use click timeout' },
    { hash: 'b', submission_uncertain: 1, error: 'old timeout' }];
  const result = context.statusProblems({ tasks: [], events: [], task_reviews: reviews }, 10000);
  assert.equal(result.reviewMessage, '2 项提交待确认 · 自动核对台账，暂不重复下载');
  assert.equal(result.reviews[0].error, 'Computer Use click timeout');
  reviews[1].submission_uncertain = 0;
  assert.equal(context.statusProblems({ task_reviews: reviews }, 10000).reviewMessage,
    '2 项下载待核查 · 详见下方任务记录');
  assert.equal(context.statusProblems({ task_reviews: [] }, 10000).reviewMessage, '');
});

test('active task and service failures still show errors alongside reviews', () => {
  assert.equal(problems([{ hash: 'abc', status: 'intent', error: 'unavailable' }], [error]).error, error);
  const serviceError = { ...error, scope: 'service' };
  assert.equal(problems([{ hash: 'abc', status: 'review' }], [error, serviceError]).error, serviceError);
  assert.equal(problems([], [error]).error, error);
});

test('foreground API errors remain in history without changing worker health', () => {
  const requestError = { ...error, scope: 'api' };
  assert.equal(problems([], [requestError]).error, undefined);
  const serviceError = { ...error, scope: 'service' };
  assert.equal(problems([], [requestError, serviceError]).error, serviceError);
});

test('later successful history sync resolves only its own old failure', () => {
  const failure = { ...error, scope: 'dandan-history' };
  const s = { events: [failure], dandan_history: { ok: true, success: 10000 } };
  assert.equal(context.statusProblems(s, 10001).error, undefined);
  for (const health of [undefined, { ok: false, success: 10000 }, { ok: true, success: 9998 }]) {
    assert.equal(context.statusProblems({ ...s, dandan_history: health }, 10001).error, failure);
  }
  const serviceFailure = { ...failure, scope: 'service' };
  assert.equal(context.statusProblems({ ...s, events: [failure, serviceFailure] }, 10001).error, serviceFailure);
});

test('healthSummary renders the cheap /api/health answer with the same priorities as the full status', () => {
  const summary = (h) => context.healthSummary(h, 10000);
  assert.equal(JSON.stringify(summary({ heartbeat: { age: null }, problems: {} })), JSON.stringify({ state: 'down', text: '后台未响应', title: '' }));  // objects cross the vm boundary
  assert.equal(summary({ heartbeat: { age: 300 }, problems: {} }).state, 'down');
  assert.equal(summary({ heartbeat: { age: 5 }, problems: {} }).state, 'ok');
  assert.equal(summary({ heartbeat: { age: 5 }, problems: { reviews: 2 } }).text, '有待核查');
  assert.equal(summary({ heartbeat: { age: 5 }, problems: { reviews: 2, error: { scope: 'task:x' } } }).text, '有错误');
  assert.equal(summary({ heartbeat: { age: 5 }, problems: { blocker: '迅雷优先', error: { scope: 'x' } } }).text, '下载受阻');
  assert.match(summary({ heartbeat: { age: 5 }, problems: {} }).title, /^心跳 /);
});
