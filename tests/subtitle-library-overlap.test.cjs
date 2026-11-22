'use strict';
// The subtitle library keeps one archive read in flight; refreshes reuse it instead of discarding its answer.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ROOT=path.join(__dirname,'..');
const settle=async(n=3)=>{for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r));};
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
/* Every archive, release and progress read is a separate deferred, so each test decides the order answers arrive in. */
function fixture(){
 const archives=[],releases=[],progress=[],docEvents={},winEvents={},intervals=[],elements=new Map();
 const $=s=>{if(!elements.has(s)){const handlers={};elements.set(s,{innerHTML:'',textContent:'',value:'',hidden:false,handlers,
   addEventListener:(k,f)=>(handlers[k]??=[]).push(f)});}return elements.get(s);};
 const track=(list,meta)=>{const d=deferred();const entry={...meta,d,settled:false};list.push(entry);
   d.promise.then(()=>{entry.settled=true;},()=>{entry.settled=true;});return d.promise;};
 const progressBody=()=>({ok:true,status:200,json:async()=>({schema:1,jobs:[],live:true,generated_at:Date.now()/1000})});
 const ctx={console,Promise,Map,Set,URL,URLSearchParams,AbortController,Date,JSON,Math,Number,String,Array,Object,Error,
  location:{search:'',href:'http://localhost:4871/subtitle-library'},history:{replaceState(){}},
  document:{hidden:false,addEventListener:(k,f)=>(docEvents[k]??=[]).push(f)},
  addEventListener:(k,f)=>(winEvents[k]??=[]).push(f),
  setInterval:(f,ms)=>intervals.push({f,ms}),setTimeout,clearTimeout,$,$$:()=>[],shell(){},
  api:p=>track(archives,{p}),
  fetch:url=>track(progress,{url}),
  FinishedReleases:{load:()=>track(releases,{}),compare:(a,b)=>a.version-b.version,label:()=>'v1'},
  esc:v=>String(v??''),ago:()=>''};
 ctx.window=ctx;vm.createContext(ctx);
 for(const f of ['subtitle-queue-model.js','subtitle-delivery-model.js','subtitle-library.js'])vm.runInContext(fs.readFileSync(path.join(ROOT,'static',f),'utf8'),ctx);
 const fire={
  visibility:()=>{for(const f of docEvents.visibilitychange||[])f();},
  focus:()=>{for(const f of winEvents.focus||[])f();},
  interval:()=>{for(const {f} of intervals)f();},
  refresh:()=>{for(const f of $('#delivery-refresh').handlers.click||[])f();},
  openHistory:()=>{for(const f of docEvents.toggle||[])f({target:{open:true,matches:s=>s.includes('.history-archive')}});},
 };
 // Answer every release/progress read still open (the fast reads of one load).
 const finishFast=async()=>{for(const r of releases)if(!r.settled)r.d.resolve([]);for(const p of progress)if(!p.settled)p.d.resolve(progressBody());await settle();};
 const outstanding=()=>archives.filter(a=>!a.settled).length;
 const history=()=>$('#history-count').textContent;
 return {archives,releases,progress,fire,finishFast,outstanding,history,$};
}
const entries=(n,tag='x')=>Array.from({length:n},(_,i)=>({show_id:100+i,season:1,episode:1,title:tag+i,base_count:1}));

test('refresh triggers while the archive read runs keep exactly one archive read in flight',async()=>{
 const f=fixture();await settle();await f.finishFast();
 for(const k of ['visibility','focus','interval','refresh']){f.fire[k]();await settle();await f.finishFast();}
 assert.equal(f.outstanding(),1);assert.equal(f.archives.length,1);
 assert.equal(f.releases.length,5,'releases are still re-read on every trigger');
 assert.equal(f.progress.length,5,'progress is still re-read on every trigger');
 f.archives[0].d.resolve({entries:entries(3)});await settle();
 assert.equal(f.history(),'历史材料档案（3 集）');
});
test('opening the history during a compact read issues exactly one full read and ignores the late compact answer',async()=>{
 const f=fixture();await settle();await f.finishFast();
 f.fire.openHistory();await settle();await f.finishFast();
 f.fire.focus();await settle();await f.finishFast();
 assert.deepEqual(f.archives.map(a=>a.p),['/subtitle-library?compact=true','/subtitle-library']);
 assert.equal(f.releases.length,2,'opening the history does not repeat the release read');
 f.archives[1].d.resolve({entries:entries(4,'full')});await settle();
 assert.equal(f.history(),'历史材料档案（4 集）');
 f.archives[0].d.resolve({entries:entries(1,'compact')});await settle();
 assert.equal(f.history(),'历史材料档案（4 集）','a superseded compact answer never replaces the full one');
});
test('after an archive answer the next refresh reads the archive again',async()=>{
 const f=fixture();await settle();await f.finishFast();
 f.archives[0].d.resolve({entries:entries(2)});await settle();
 f.fire.refresh();await settle();await f.finishFast();
 assert.equal(f.archives.length,2);assert.match(f.history(),/读取中/);
 f.archives[1].d.resolve({entries:entries(5)});await settle();
 assert.equal(f.history(),'历史材料档案（5 集）');
});
test('a failed archive read is retried by the next refresh',async()=>{
 const f=fixture();await settle();await f.finishFast();
 f.archives[0].d.reject(Error('busy'));await settle();
 assert.match(f.$('#delivery-notice').textContent,/历史材料暂不可读取/);
 f.fire.focus();await settle();await f.finishFast();
 assert.equal(f.archives.length,2);
 f.archives[1].d.resolve({entries:entries(2)});await settle();
 assert.equal(f.history(),'历史材料档案（2 集）');
 assert.ok(!f.$('#delivery-notice').textContent.includes('历史材料暂不可读取'));
});
test('an accepted authored row appears when the archive read finishes, even if a later load started',async()=>{
 const f=fixture();await settle();await f.finishFast();
 f.fire.focus();await settle();await f.finishFast();
 f.archives[0].d.resolve({entries:[{show_id:7,season:1,episode:3,title:'Built',base_count:2,build_id:'b1',media_id:'m1'}]});await settle();
 assert.match(f.$('#subtitle-records').innerHTML,/watch\?media=m1/);
});
