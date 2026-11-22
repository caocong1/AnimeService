// The subtitle studio archive polls only while visible, never overlaps reads, and keeps opened records open.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const SRC=require('node:path').join(__dirname,'../static/subtitle-studio.js');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function fixture({hidden=false,hash=''}={}){
 const calls=[],pending=[],timers=[],listeners={};let children=[];
 const node=id=>({id,open:false,style:{},scrollIntoView(){},querySelector:s=>s==='.body'?{append(){}}:null});
 const records={set innerHTML(v){children=[...v.matchAll(/<details[^>]*\bid="([^"]+)"/g)].map(m=>node(m[1]));},get children(){return children;}};
 const elements={'#summary':{textContent:''},'#records':records};
 const document={hidden,getElementById:id=>children.find(c=>c.id===id)||null,createElement:()=>({}),
  addEventListener:(k,fn)=>(listeners[k]??=[]).push(fn)};
 vm.runInContext(fs.readFileSync(SRC,'utf8'),vm.createContext({console,Promise,Set,location:{hash},document,
  setInterval:(fn,ms)=>timers.push({fn,ms}),$:s=>elements[s],$$:s=>s==='#records details[open]'?children.filter(c=>c.open):[],
  esc:v=>String(v??''),shell(){},api:p=>{calls.push(p);return new Promise(resolve=>pending.push(resolve));}}));
 const data={summary:{episodes:2,bases:2,accepted:0},jobs:[1,2].map(episode=>({show_id:7,episode,title:'T',state:'waiting_review',
  payload:{season:1,base_count:1,chinese_texts:1,japanese_drafts:0,source_coverage:[]}}))};
 return {calls,timers,document,record:i=>children[i],
  answer:async()=>{while(pending.length)pending.shift()(data);await settle();},
  tick:async()=>{for(const t of timers)t.fn();await settle();},
  show:async()=>{document.hidden=false;for(const fn of listeners.visibilitychange||[])fn();await settle();}};
}
test('a hidden archive page stops re-reading and refreshes when shown again',async()=>{
 const f=fixture({hidden:true});await f.answer();
 await f.tick();await f.tick();assert.equal(f.calls.length,1);
 await f.show();assert.equal(f.calls.length,2);
});
test('a slow archive read is never overlapped by the next refresh',async()=>{
 const f=fixture();await f.tick();await f.tick();assert.equal(f.calls.length,1);
 await f.answer();await f.tick();assert.equal(f.calls.length,2);
});
test('records the viewer opened, including the linked one, stay open across refreshes',async()=>{
 const f=fixture({hash:'#episode-7-2'});await f.answer();
 assert.equal(f.record(1).open,true);f.record(0).open=true;
 await f.tick();await f.answer();
 assert.equal(f.record(0).open,true);assert.equal(f.record(1).open,true);
});
test('the refresh cadence matches the other pages',()=>{assert.deepEqual(fixture().timers.map(t=>t.ms),[60000]);});
