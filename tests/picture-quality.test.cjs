const test = require('node:test');
const assert = require('node:assert/strict');
const {geometry, eligible} = require('../static/picture-quality.js');
test('physical pixels and aspect are retained at HiDPI', () => {
  const g = geometry(1920,1080,1100,700,1.25,'pixel');
  assert.equal(g.width,1375); assert.equal(g.height,773);
  assert.equal(g.cssWidth,1100); assert.ok(g.scale < 1);
  const fit = geometry(1920,1080,1920,1080,2,'pixel');
  assert.equal(fit.width,1920); assert.equal(fit.height,1080); assert.equal(fit.cssWidth,960);
});
test('native contains portrait footage and rejects invalid measurements', () => {
  assert.equal(geometry(1080,1920,1920,1080).height,1080);
  assert.equal(geometry(0,1080,1920,1080),null);
  assert.equal(geometry(1920,1080,Infinity,1080),null);
});
test('10-bit HDR and unknown tags cannot use an 8-bit SDR scaler', () => {
  assert.equal(eligible({sdr_scaler_eligible:true,bit_depth:8,dynamic_range:'SDR'}),true);
  for (const dynamic_range of ['PQ','HLG','unknown']) assert.equal(eligible({sdr_scaler_eligible:true,bit_depth:8,dynamic_range}),false);
  assert.equal(eligible({sdr_scaler_eligible:true,bit_depth:10,dynamic_range:'SDR'}),false);
  assert.equal(eligible(null),false);
});
