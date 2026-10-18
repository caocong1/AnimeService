const test = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const script = path.join(__dirname, '..', 'scripts', 'codex_dispatch_control.cjs');
const turn = '01a13f9c-66e7-7ad0-bd73-3997b6cfc773';

test('threads outside the configured dispatcher list are refused before any IPC', () => {
  const r = spawnSync(process.execPath, [script, 'not-a-dispatcher', turn], {encoding: 'utf8'});
  assert.equal(r.status, 1);
  assert.match(r.stderr, /outside dispatcher scope/);
});

test('the dispatcher list comes from the shared config file', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-'));
  const cfg = path.join(dir, 'dispatch.json');
  fs.writeFileSync(cfg, JSON.stringify({targets: {'thread-x': 'auto-x'}}));
  const r = spawnSync(process.execPath, [script, 'thread-x', turn], {encoding: 'utf8', env: {...process.env, ANIMESERVICE_DISPATCH_CONFIG: cfg}, timeout: 25000});
  assert.doesNotMatch(r.stderr, /outside dispatcher scope/);   // scope passed; only the local IPC pipe is missing here
  assert.notEqual(r.status, 0);
});
