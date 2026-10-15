const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/login.js'), 'utf8');

function page(fetch) {
  const redirects = [], message = { textContent: '' };
  const button = { setAttribute() {}, removeAttribute() {} };
  const form = { code: { value: 'test-code' }, querySelector: () => button };
  let submit;
  form.addEventListener = (_, fn) => { submit = fn; };
  const context = vm.createContext({
    fetch,
    location: { replace: value => redirects.push(value) },
    document: { querySelector: s => s === '#login-form' ? form : message },
  });
  vm.runInContext(source, context);
  return { redirects, message, button, submit: () => submit({ preventDefault() {}, target: form }) };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('login reuses the saved device session without submitting a pairing code', async () => {
  const calls = [];
  const p = page(async (url, init) => {
    calls.push([url, init]);
    return { ok: true, json: async () => ({ token: 'temporary-test-token' }) };
  });
  await settle();
  assert.deepEqual(p.redirects, ['/']);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/bootstrap');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(calls[0][1].cache, 'no-store');
});

test('absent, expired and unavailable sessions keep manual pairing usable', async () => {
  for (const status of [401, 403, 502]) {
    const p = page(async () => ({ ok: false, status }));
    await settle();
    assert.deepEqual(p.redirects, []);
    assert.equal(p.message.textContent, '');
    assert.notEqual(p.button.disabled, true);
  }
  const p = page(async () => { throw Error('offline'); });
  await settle();
  assert.deepEqual(p.redirects, []);
  assert.equal(p.message.textContent, '');
});

test('a malformed session response never redirects', async () => {
  for (const value of [{}, { token: '' }, { token: 42 }]) {
    const p = page(async () => ({ ok: true, json: async () => value }));
    await settle();
    assert.deepEqual(p.redirects, []);
  }
});

test('a late session check cannot replace a manual pairing error', async () => {
  let resolve;
  const p = page((url, init) => init.method === 'POST'
    ? Promise.resolve({ ok: false, json: async () => ({ detail: '配对码无效' }) })
    : new Promise(done => { resolve = done; }));
  await p.submit();
  resolve({ ok: true, json: async () => ({ token: 'temporary-test-token' }) });
  await settle();
  assert.deepEqual(p.redirects, []);
  assert.equal(p.message.textContent, '配对码无效');
  assert.equal(p.button.disabled, false);
});

test('manual pairing succeeds when there is no saved session', async () => {
  const p = page(async (_, init) => init.method === 'POST'
    ? { ok: true, json: async () => ({ ok: true }) }
    : { ok: false, status: 401 });
  await p.submit();
  await settle();
  assert.deepEqual(p.redirects, ['/']);
});
