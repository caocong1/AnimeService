const assert = require('node:assert/strict');
const test = require('node:test');
const {create} = require('../static/danmu-retry.js');

test('temporary 404 retries, then returns recovered source without further requests', async () => {
  const waits = [], retry = create({wait: async ms => waits.push(ms)});
  let calls = 0;
  const d = await retry.request(async () => { if (++calls < 3) throw Object.assign(Error('missing'), {status:404}); return {bangumi:{episodes:[1]}}; });
  assert.equal(calls,3);assert.deepEqual(waits,[5000,15000]);assert.equal(d.bangumi.episodes.length,1);
});
test('exhausted retries stay bounded and keep the original failure', async () => {
  let calls = 0;const waits = [], failure = Object.assign(Error('unavailable'),{status:503});
  await assert.rejects(create({wait:async ms=>waits.push(ms)}).request(async()=>{calls++;throw failure}), e=>e===failure);
  assert.equal(calls,4);assert.deepEqual(waits,[5000,15000,45000]);
});
test('authentication, mapping ambiguity and empty successful results are not retried', async () => {
  const retry=create({wait:()=>assert.fail('unexpected retry')});
  for (const status of [400,401,403,422]) await assert.rejects(retry.request(async()=>{throw Object.assign(Error('blocked'),{status})}));
  for (const d of [{status:'needs_selection'},{comments:[],sources:[{count:0}]}]) assert.equal(await retry.request(async()=>d),d);
});
test('structured matching and partial comment failures retry only failed requests', async () => {
  for (const d of [{status:'error',retryable:true},{status:'matched',sources:[{error:'timeout'}]}]) {
    let calls=0;const good={status:'matched',sources:[{count:10}]};
    assert.equal(await create({wait:async()=>{}}).request(async()=>++calls===1?d:good),good);assert.equal(calls,2);
  }
});
test('manual change cancels pending retry and cannot revive removed sources', async () => {
  let current=true,calls=0;
  await assert.rejects(create({wait:async()=>{current=false}}).request(async()=>{calls++;throw Error('offline')},{current:()=>current}),{name:'AbortError'});
  assert.equal(calls,1);
});
test('leaving cancels old work, returning allows fresh work', async () => {
  let release;const retry=create({wait:()=>new Promise(r=>release=r)});
  const old=retry.request(async()=>{throw Error('offline')});
  await Promise.resolve();retry.close();retry.resume();release();
  await assert.rejects(old,{name:'AbortError'});
  assert.deepEqual(await retry.request(async()=>({ok:true})),{ok:true});
});
