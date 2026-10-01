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
