const assert = require('node:assert/strict');
const test = require('node:test');
const {contentBox} = require('../static/screenshot.js');

test('letterboxed video maps on-screen pixels back to the native frame', () => {
  // 1920x1080 picture inside a 1000x1000 player: 1000x562.5 band, centered vertically.
  const band = contentBox({left: 10, top: 20, width: 1000, height: 1000}, 1920, 1080);
  assert.ok(Math.abs(band.left - 10) < 1e-9); assert.equal(band.top, 238.75); assert.equal(band.scale, 1.92);
  // Pillarboxed: 4:3 picture in a 16:9 player.
  const box = contentBox({left: 0, top: 0, width: 1600, height: 900}, 1440, 1080);
  assert.equal(box.left, 200); assert.equal(box.top, 0); assert.equal(box.scale, 1.2);
});
