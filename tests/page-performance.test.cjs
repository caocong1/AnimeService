const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function fixture(){
 const boot=deferred(),queue=deferred(),calls=[],elements=new Map();
 const $=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',open:false});return elements.get(s);};
 const detail={show:{id:42,title:'Example',selected:false,metadata:{},mapping:{},tags:[]},board:{eps:[],next:null},episodes:[],sources:[],candidates:[]};
 const context=vm.createContext({console,Promise,Map,URL,location:{pathname:'/show/42',hash:''},
  document:{addEventListener(){}},addEventListener(){},$,$$:()=>[],shell(){},toast(){},
  boot:()=>boot.promise,api:p=>{calls.push(p);return p.startsWith('/subtitle-queue')?queue.promise:Promise.resolve(detail);},
  FinishedReleases:{load:()=>{calls.push('releases');return Promise.resolve([]);},applyBoard:(_s,b)=>b,observe:()=>()=>{}},
  App:{settings:{}},STATES:{wish:'想看'},EP_LABEL:{},TASKS:{},applyAiringSchedule(){},
  esc:v=>String(v??''),icon:()=>'',cover:()=>'<span class="cover"></span>',autoDl:()=>'',
 });
 vm.runInContext(fs.readFileSync('static/show.js','utf8'),context);
 return {boot,queue,calls,$};
}
test('detail and release reads start while bootstrap is still pending',async()=>{
 const f=fixture();assert.ok(f.calls.includes('/shows/42'));assert.ok(f.calls.includes('releases'));
 f.boot.resolve();await settle();assert.match(f.$('#main').innerHTML,/show-hero/);
 f.queue.resolve({items:[]});await settle();
});
test('slow or failed optional subtitle diagnostics do not block or replace the detail page',async()=>{
 const f=fixture();f.boot.resolve();await settle();const content=f.$('#main').innerHTML;
 assert.match(content,/show-hero/);assert.ok(f.calls.includes('/subtitle-queue?show=42'));
 f.queue.reject(Error('optional queue offline'));await settle();assert.equal(f.$('#main').innerHTML,content);
});

function libraryFixture(options={}){
 const archive=deferred(),elements=new Map(),events={},requests=[];
 const $=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',textContent:'',value:'',hidden:false,addEventListener(){}});return elements.get(s);};
 const release={id:'ready',show_id:42,season:1,episode:2,title:'Example',version:1,edition:'v1',
  duration_seconds:1400,player_url:'/watch?release=ready',finished_at:'2026-11-01'};
 const context=vm.createContext({console,Promise,Map,URL,URLSearchParams,AbortController,Date,
  location:{search:'',href:'http://localhost:4871/subtitle-library'},history:{replaceState(){}},
  document:{addEventListener(){}},addEventListener:(k,fn)=>events[k]=fn,setInterval(){},setTimeout,clearTimeout,$,$$:()=>[],shell(){},
  api:()=>archive.promise,fetch:async url=>{requests.push(url);return options.fetch?options.fetch(url):{ok:true,json:async()=>({schema:1,jobs:[],live:true,generated_at:Date.now()/1000})};},
  FinishedReleases:{load:async()=>[release],compare:(a,b)=>a.version-b.version,label:()=> 'v1'},
  esc:v=>String(v??''),ago:()=>'',
 });
 context.window=context;
 vm.runInContext(fs.readFileSync('static/subtitle-queue-model.js','utf8'),context);
 vm.runInContext(fs.readFileSync('static/subtitle-delivery-model.js','utf8'),context);
 vm.runInContext(fs.readFileSync('static/subtitle-library.js','utf8'),context);
 return {archive,$,events,requests};
}
test('subtitle library shows delivered media while historical archives are still loading',async()=>{
 const f=libraryFixture();await settle();
 assert.match(f.$('#subtitle-records').innerHTML,/\/watch\?release=ready/);
 assert.match(f.$('#history-count').textContent,/读取中/);
 f.$('#subtitle-search').value='Example';
 f.archive.resolve({entries:[{show_id:43,season:1,episode:1,title:'Old',base_count:1}]});await settle();
 assert.match(f.$('#subtitle-records').innerHTML,/\/watch\?release=ready/);
 assert.equal(f.$('#subtitle-search').value,'Example');
 assert.ok(!f.$('#history-count').textContent.includes('读取中'));
});
test('an unavailable historical archive keeps the playable subtitle library visible',async()=>{
 const f=libraryFixture();await settle();const content=f.$('#subtitle-records').innerHTML;
 f.archive.reject(Error('historical archive offline'));await settle();
 assert.equal(f.$('#subtitle-records').innerHTML,content);
 assert.match(f.$('#delivery-notice').textContent,/历史材料暂不可读取/);
});

test('verified releases appear before live progress, and refresh failures keep the last good display',async()=>{
 const progress=deferred();let offline=false;
 const f=libraryFixture({fetch:()=>offline?Promise.reject(Error('progress offline')):progress.promise});
 await settle();assert.match(f.$('#subtitle-records').innerHTML,/watch\?release=ready/);
 assert.deepEqual(f.requests,['/api/subtitle-progress']);
 progress.resolve({ok:true,json:async()=>({schema:1,live:true,jobs:[],generated_at:Date.now()/1000})});await settle();
 offline=true;f.events.focus();await settle();
 assert.match(f.$('#subtitle-records').innerHTML,/watch\?release=ready/);
 assert.match(f.$('#delivery-notice').textContent,/进度暂不可读取/);
 assert.ok(!f.requests.some(u=>u.includes('subtitle-delivery-status.json')));
});

test('an old server uses an explicitly historical snapshot only when the live route is missing',async()=>{
 const f=libraryFixture({fetch:url=>url.startsWith('/api/')?{status:404,ok:false}:{ok:true,json:async()=>({schema:1,jobs:[],generated_at:Date.now()/1000})}});
 await settle();assert.deepEqual(f.requests,['/api/subtitle-progress','/static/subtitle-delivery-status.json']);
 assert.match(f.$('#delivery-notice').textContent,/历史准备快照/);
});
test('production queue reads start before bootstrap finishes',async()=>{
 const bootstrap=deferred(),calls=[],elements=new Map();
 const $=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',textContent:'',addEventListener(){}});return elements.get(s);};
 const context=vm.createContext({Promise,Date,$,shell(){},setInterval(){},document:{addEventListener(){}},
  boot:()=>bootstrap.promise,api:p=>{calls.push(p);return Promise.resolve({items:[],summary:{producing:0,starting:0,queued:0,preparing:0,downloading:0,waiting_video:0},generated_at:Date.now()/1000});},
  FinishedReleases:{load:async()=>[]},SubtitleQueueModel:{sections:()=>[],health:()=>''},
 });
 vm.runInContext(fs.readFileSync('static/subtitle-queue.js','utf8'),context);
 assert.deepEqual(calls,['/subtitle-queue']);await settle();
 assert.match($('#queue').innerHTML,/empty/);
 bootstrap.resolve();await settle();
});
