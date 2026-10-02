const test = require('node:test');
const assert = require('node:assert/strict');
const {create, preferences} = require('../static/danmu-alignment.js');
const {savedSources} = require('../static/danmu-timing.js');
const deferred = () => { let resolve; const promise = new Promise(r => resolve=r); return {promise,resolve}; };
function fixture(request, wait) {
  const source={id:'registered',source_identity:'stable',title:'Bilibili',offset:0};
  const selected=[source], saved=[], applied=[];
  const controller=create({request,wait,present:s=>selected.includes(s),save:s=>saved.push(s.offset),render:()=>{},apply:async()=>applied.push(source.offset)});
  return {source,selected,saved,applied,controller};
}
test('reliable result changes only the offset and persists it', async () => {
  const f=fixture(async()=>({status:'matched',offset:-20.06})); f.source.site='bilibili';
  await f.controller.start(f.source);
  assert.equal(f.source.offset,-20.1); assert.equal(f.source.site,'bilibili');
  assert.deepEqual(f.saved,[-20.1]); assert.deepEqual(f.applied,[-20.1]);
});
test('manual edit while initial request is pending wins and cancels eventual job', async () => {
  const d=deferred(),calls=[];
  const f=fixture((path)=>{calls.push(path);return path ? Promise.resolve({status:'cancelled'}) : d.promise;});
  const running=f.controller.start(f.source);
  f.controller.manual(f.source); f.source.offset=12;
  d.resolve({status:'matched',offset:-20,job_id:'job'}); await running;
  assert.equal(f.source.offset,12); assert.deepEqual(f.applied,[]); assert.ok(calls.includes('/job/cancel'));
});
test('removed source and disabled source ignore an in-flight polling result', async () => {
  for (const remove of [true,false]) {
    const d=deferred(),entered=deferred();
    const f=fixture(async path=>{if(!path)return {status:'running',job_id:'job'};entered.resolve();return d.promise;},async()=>{});
    const running=f.controller.start(f.source); await entered.promise;
    if(remove)f.selected.length=0;else f.source.alignmentEnabled=false;
    d.resolve({status:'matched',offset:20});await running;
    assert.equal(f.source.offset,0);assert.deepEqual(f.saved,[]);
  }
});
test('ambiguous match, dependency failure and invalid offset never change timing', async () => {
  for (const result of [{status:'unreliable'},{status:'unavailable'},{status:'matched',offset:Infinity}]) {
    const f=fixture(async()=>result); f.source.offset=3;
    await f.controller.start(f.source);assert.equal(f.source.offset,3);assert.deepEqual(f.saved,[]);
  }
});
test('manual/disabled settings survive reload and prevent automatic requests', async () => {
  for(const prefs of [{alignmentManual:true},{alignmentEnabled:false}]) {
    const f=fixture(async()=>{throw Error('must not request');});Object.assign(f.source,prefs);
    const persisted=savedSources([f.source])[0];assert.deepEqual(preferences(persisted),prefs);
    await f.controller.start(f.source);assert.equal(f.source.alignmentStatus,undefined);
  }
});
test('duplicate start is coalesced and a later manual result cannot overwrite a new run', async () => {
  const d=deferred(); let calls=0;
  const f=fixture(async()=>{calls++;return d.promise;});
  const running=f.controller.start(f.source);await f.controller.start(f.source);
  assert.equal(calls,1);d.resolve({status:'matched',offset:20});await running;
});
test('temporary worker failure retries with a fresh forced job and applies recovery', async () => {
  const calls=[], waits=[]; let attempts=0;
  const f=fixture(async(path,body)=>{
    calls.push({path,body});
    if (!path) return {status:'queued',job_id:`job${++attempts}`};
    return attempts === 1 ? {status:'unavailable',retryable:true,message:'来源音轨暂时失败'} : {status:'matched',offset:0.06};
  },async ms=>waits.push(ms));
  await f.controller.start(f.source,{confirmed:true});
  assert.deepEqual(waits,[1500,5000,1500]);
  assert.equal(attempts,2); assert.equal(calls[2].body.force,true); assert.equal(calls[2].body.confirmed,true);
  assert.deepEqual(f.saved,[0.1]); assert.equal(f.source.alignmentStatus,'matched');
});
test('continued transient errors stop after two extra attempts; timing stays unchanged', async () => {
  let attempts=0; const waits=[];
  const f=fixture(async()=>{attempts++;return {status:'unavailable',retryable:true,message:'来源读取失败'};},async ms=>waits.push(ms));
  f.source.offset=7; await f.controller.start(f.source);
  assert.equal(attempts,3); assert.deepEqual(waits,[5000,15000]);
  assert.equal(f.source.offset,7);assert.deepEqual(f.saved,[]);assert.equal(f.source.alignmentMessage,'来源读取失败');
});
test('permanent failures and non-transient HTTP errors never retry', async () => {
  for (const status of [400,401,403,404,422]) {
    let calls=0;const f=fixture(async()=>{calls++;throw Object.assign(Error('private detail'),{status});},async()=>{throw Error('must not wait');});
    await f.controller.start(f.source);assert.equal(calls,1);assert.ok(!f.source.alignmentMessage.includes('private'));
  }
  for (const result of [{status:'unavailable',reason:'tool',retryable:false},{status:'unreliable'},{status:'unsupported'}]) {
    let calls=0;const f=fixture(async()=>{calls++;return result;},async()=>{throw Error('must not wait');});
    await f.controller.start(f.source);assert.equal(calls,1);
  }
});
test('network and temporary HTTP errors recover automatically', async () => {
  for (const status of [undefined,429,503,504]) {
    let calls=0;const f=fixture(async()=>{
      if(++calls===1)throw Object.assign(Error('private'),{status});
      return {status:'matched',offset:3};
    },async()=>{});
    await f.controller.start(f.source);assert.equal(calls,2);assert.deepEqual(f.saved,[3]);
  }
});
test('lost in-flight job after a service restart retries; initial media 404 does not', async () => {
  let attempts=0;
  const f=fixture(async path=>{
    if(!path)return {status:'running',job_id:`job${++attempts}`};
    if(attempts===1)throw Object.assign(Error('expired'),{status:404});
    if(path.endsWith('/cancel'))return {};
    return {status:'matched',offset:2};
  },async()=>{});
  await f.controller.start(f.source);assert.equal(attempts,2);assert.deepEqual(f.saved,[2]);
});
test('manual edit, disable, remove, or lifecycle cancellation during backoff stops retries', async () => {
  for (const action of ['manual','disable','remove','cancel']) {
    const delay=deferred(),entered=deferred();let calls=0;
    const f=fixture(async()=>{calls++;return {status:'unavailable',retryable:true};},async()=>{entered.resolve();await delay.promise;});
    const running=f.controller.start(f.source);await entered.promise;
    if(action==='manual'){f.controller.manual(f.source);f.source.offset=8;}
    else if(action==='disable')f.source.alignmentEnabled=false;
    else if(action==='remove')f.selected.length=0;
    else f.controller.cancel(f.source);
    delay.resolve();await running;assert.equal(calls,1);assert.deepEqual(f.saved,[]);
    if(action==='manual')assert.equal(f.source.offset,8);
  }
});
