'use strict';
// Fault injection tests for the QA recorder, never page performance samples.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),{EventEmitter}=require('node:events');
const source=fs.readFileSync('scripts/check_page_performance.cjs','utf8');
const entry=['queue','/subtitle-queue','#queue .empty'];
const response={status:()=>200,ok:()=>true};
function fixture(options={}){
 const page=new EventEmitter();
 page.goto=options.goto||(async()=>response);
 page.evaluate=options.evaluate||(async()=>({ready_ms:120,content_ms:100,covers:[],blocked_writes:[]}));
 let closes=0;
 const context={newPage:async()=>page,addInitScript:async()=>{},close:async()=>{closes++;if(options.close)return options.close();}};
 const browser={newContext:async()=>context};
 return {page,browser,context,get closes(){return closes;}};
}
function loadMeasure(){
 const sandbox={require:name=>name==='playwright'?{}:require(name),URL,console,Date,
  process:{argv:[],env:{PAGE_QA_BASE:'http://127.0.0.1:4871'}}};
 vm.runInNewContext(source.slice(0,source.lastIndexOf('\n(async()=>{'))+'\nglobalThis.measureTest=measure;',sandbox);
 return sandbox.measureTest;
}
const measure=loadMeasure();
function request(url,method='GET'){
 return {url:()=>url,method:()=>method,resourceType:()=> 'document',
  timing:()=>({startTime:100,requestStart:2,responseStart:5,responseEnd:8}),failure:()=>({errorText:'net::ERR_FAILED'})};
}

test('navigation errors retain network, JS, write and cover failures and stay failed',async()=>{
 const f=fixture({goto:async()=>{
  const doc=request('http://127.0.0.1:4871/subtitle-queue');
  f.page.emit('request',doc);f.page.emit('requestfinished',doc);
  const cover=request('http://127.0.0.1:4871/covers/v1/160/broken.webp');
  f.page.emit('request',cover);f.page.emit('response',{request:()=>cover,url:cover.url,status:()=>503});
  f.page.emit('requestfailed',cover);
  f.page.emit('request',request('http://127.0.0.1:4871/api/write','POST'));
  f.page.emit('pageerror',Error('page script failed'));
  throw Error('navigation timeout');
 }});
 const result=await measure(f.browser,entry);
 assert.equal(result.pass,false);assert.equal(result.ready_ms,null);
 assert.equal(result.error,'navigation timeout');assert.equal(result.failure_phase,'navigation');
 assert.equal(result.errors[0],'page script failed');assert.equal(result.writes[0].method,'POST');
 assert.equal(result.failed_covers[0],503);assert.equal(result.failed_covers[1],'net::ERR_FAILED');
 assert.equal(result.requests.length,3);assert.equal(result.requests[0].timing.responseEnd,8);
 assert.equal(result.requests[1].status,503);assert.equal(result.requests[1].failure.errorText,'net::ERR_FAILED');
 assert.equal(f.closes,1);
});

test('evaluation failure keeps the real navigation status and collected evidence',async()=>{
 const f=fixture({evaluate:async()=>{throw Error('execution context gone');}});
 const result=await measure(f.browser,entry);
 assert.equal(result.pass,false);assert.equal(result.status,200);
 assert.equal(result.failure_phase,'evaluation');assert.equal(result.error,'execution context gone');
 assert.equal(f.closes,1);
});

test('context and page setup failures produce failed rows and close only created contexts',async()=>{
 const result=await measure({newContext:async()=>{throw Error('context unavailable');}},entry);
 assert.equal(result.failure_phase,'context');assert.equal(result.ready_ms,null);assert.equal(result.pass,false);
 const f=fixture();f.context.newPage=async()=>{throw Error('page unavailable');};
 const second=await measure(f.browser,entry);
 assert.equal(second.failure_phase,'page');assert.equal(second.pass,false);assert.equal(f.closes,1);
});

test('cleanup errors preserve the original failure and cannot turn a sample green',async()=>{
 const f=fixture({goto:async()=>{throw Error('original navigation error');},close:async()=>{throw Error('close failed');}});
 const result=await measure(f.browser,entry);
 assert.equal(result.error,'original navigation error');assert.equal(result.cleanup_error,'close failed');
 assert.equal(result.failure_phase,'navigation');assert.equal(result.pass,false);
 const success=await measure(fixture({close:async()=>{throw Error('close failed');}}).browser,entry);
 assert.equal(success.ready_ms,120);assert.equal(success.pass,false);
 assert.equal(success.failure_phase,'context.close');assert.equal(success.cleanup_error,'close failed');
});

test('the measurement checkpoint runs before a pending context close',async()=>{
 let release,checkpoint,started=false;
 const pending=new Promise(resolve=>release=resolve);
 const f=fixture({close:()=>{started=true;return pending;}});
 const completion=measure(f.browser,entry,row=>checkpoint=row);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(started,true);assert.equal(checkpoint.ready_ms,120);
 release();assert.equal(await completion,checkpoint);assert.equal(f.closes,1);
});

test('the CLI saves the sample before closing and records browser cleanup failure',async()=>{
 const snapshots=[],f=fixture();
 const sandbox={URL,Date,console:{log(){},error(){}},process:{argv:[],env:{PAGE_QA_BASE:'http://127.0.0.1:4871',PAGE_QA_SAMPLES:'1',PAGE_QA_SHOWS:'1'}},
  require:name=>name==='playwright'?{chromium:{launch:async()=>({...f.browser,version:()=> 'test',close:async()=>{throw Error('browser close failed');}})}}:
   name==='node:fs'?{mkdirSync(){},writeFileSync(_path,value){snapshots.push(JSON.parse(value));}}:require(name)};
 await vm.runInNewContext(source,sandbox);
 assert.ok(snapshots.some(r=>r.active?.phase==='closing_context'&&r.rows.length));
 assert.ok(snapshots.some(r=>r.phase==='closing_browser'));
 const final=snapshots.at(-1);
 assert.equal(final.measurement_complete,true);assert.equal(final.driver_complete,false);
 assert.equal(final.phase,'browser_close_failed');assert.equal(final.driver_error,'browser close failed');
 assert.equal(final.pass,false);assert.equal(sandbox.process.exitCode,1);
});

test('cover/readiness errors retain requests and remain failed without a thrown navigation',async()=>{
 const f=fixture({evaluate:async()=>({ready_ms:null,error:'Visible cover did not load'})});
 const result=await measure(f.browser,entry);
 assert.equal(result.pass,false);assert.equal(result.ready_ms,null);
 assert.equal(result.error,'Visible cover did not load');assert.ok(Array.isArray(result.requests));
});
