const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const context = vm.createContext({ addEventListener() {}, window: { fetch: async () => {} }, document: { addEventListener() {} }, matchMedia: () => ({ matches: false }) });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/core.js'), 'utf8'), context);
const {negotiated} = require('../static/playback-writer.js');

test('the bootstrap capability is read through a whitelist and nothing else is merged', () => {
  const kept = context.capabilityWhitelist({ playback_writer: { protocol: 'playback-writer/v2', enabled: true, secret: 'x' }, other: { enabled: true } });
  assert.equal(JSON.stringify(kept), JSON.stringify({ playback_writer: { protocol: 'playback-writer/v2', enabled: true } }));   // vm realm: compare by value
  assert.equal(negotiated(kept), true);
  assert.equal(JSON.stringify(context.capabilityWhitelist(undefined)), '{"playback_writer":null}');
  assert.equal(JSON.stringify(context.capabilityWhitelist({ playback_writer: 'yes' })), '{"playback_writer":null}');
  assert.equal(negotiated(context.capabilityWhitelist({ playback_writer: { protocol: 'playback-writer/v2', enabled: 'true' } })), false);
});
