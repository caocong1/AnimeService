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
  for (const status of ['complete', 'cleaned', 'downloading']) {
    assert.equal(problems([{ hash: 'abc', status, error: '' }], [error]).error, undefined);
  }
});

test('active task and service failures still show errors alongside reviews', () => {
  assert.equal(problems([{ hash: 'abc', status: 'intent', error: 'unavailable' }], [error]).error, error);
  const serviceError = { ...error, scope: 'service' };
  assert.equal(problems([{ hash: 'abc', status: 'review' }], [error, serviceError]).error, serviceError);
  assert.equal(problems([], [error]).error, error);
});
