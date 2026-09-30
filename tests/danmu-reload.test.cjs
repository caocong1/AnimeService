const test = require('node:test');
const assert = require('node:assert/strict');
const {createReplacer} = require('../static/danmu-timing.js');

test('reloads replace, serialize, snapshot input and recover from failure', async () => {
  let configured, queue = [], active = 0, fail = false;
  const plugin = {
    config(options) { configured = options.danmuku; },
    async load(...args) {
      assert.equal(args.length, 0, 'passing rows appends instead of replacing');
      assert.equal(active++, 0, 'loads must not interleave');
      try {
        queue = [];
        for (const row of configured) { await Promise.resolve(); queue.push(row); }
        if (fail) { fail = false; throw Error('fixture failure'); }
      } finally { active--; }
    },
  };
  const replace = createReplacer(plugin);
  const first = [{time:495, text:'聊了八分钟', mode:0}];
  for (let i=0; i<4; i++) await replace(first);
  assert.equal(queue.length, 1);
  const shifted = [{...first[0], time:496}];
  const jobs = [replace(first), replace(shifted)];
  shifted[0].time = 999;
  await Promise.all(jobs);
  assert.equal(queue[0].time, 496);
  await replace([]);
  assert.deepEqual(queue, []);
  fail = true;
  await assert.rejects(replace(first), /fixture failure/);
  await replace(first);
  assert.equal(queue.length, 1);
});
