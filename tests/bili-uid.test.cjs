const test = require('node:test');
const assert = require('node:assert/strict');
const {crc32, uidCandidates} = require('../static/bili-uid.js');

test('crc32 matches the standard check value', () => {
  assert.equal(crc32('123456789'), 0xcbf43926);
});

test('every UID up to 10 digits is recovered and every candidate hashes back', () => {
  for (const uid of ['1', '7', '42', '208', '1234', '9999', '10000', '54321', '546195', '8047632', '44744006', '123456789', '3493110839']) {
    const hash = crc32(uid).toString(16);
    const found = uidCandidates(hash);
    assert.ok(found.includes(uid), `${uid} not found for ${hash}`);
    for (const x of found) assert.equal(crc32(x).toString(16), hash);
    assert.deepEqual(found.map(Number), [...found.map(Number)].sort((a, b) => a - b));
  }
});

test('candidates never start with zero and junk input is rejected', () => {
  const found = uidCandidates(crc32('1000').toString(16));
  assert.ok(found.every(x => x[0] !== '0'));
  assert.deepEqual(uidCandidates('not-a-hash'), []);
  assert.deepEqual(uidCandidates('dark221530'), []);
  assert.deepEqual(uidCandidates(undefined), []);
});
