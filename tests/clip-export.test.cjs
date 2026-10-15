const assert = require('node:assert/strict');
const test = require('node:test');
const {parseTime,timeText,dimensions} = require('../static/clip-export.js');

test('clip time parsing accepts precise seconds and rejects malformed ranges', () => {
  assert.equal(parseTime('123.125'),123.125);
  assert.equal(parseTime('02:03.125'),123.125);
  assert.equal(parseTime('01:02:03.125'),3723.125);
  for (const t of ['-1','NaN','1:60','1:60:00','1::2','','1.2:00','1e9']) assert.throws(()=>parseTime(t));
  assert.equal(timeText(59.9999),'00:01:00.000');
  assert.equal(parseTime(timeText(3723.125)),3723.125);
});
test('output size matches backend even-pixel MP4 encoding and proportional GIF', () => {
  assert.deepEqual(dimensions(301,101,201,'mp4'),{width:200,height:66});
  assert.deepEqual(dimensions(1920,1080,640,'gif'),{width:640,height:360});
  assert.deepEqual(dimensions(1920,1080,0,'mp4'),{width:1920,height:1080});
});
