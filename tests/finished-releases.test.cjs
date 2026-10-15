const test = require('node:test');
const assert = require('node:assert/strict');
const {normalize, merge} = require('../static/finished-releases.js');
const sample = (version=1) => ({id:`episode-v${version}`,status:'finished',show_id:42,season:2,episode:2,version,title:'示例',duration_seconds:1420,width:1920});

test('delivered media is available without fabricating an accepted subtitle build', () => {
  const original={show_id:42,season:2,episode:2,title:'示例',state:'waiting_review',build_id:null,progress:{reviewed:392,total:454}};
  const [row]=merge([original],normalize({releases:[sample()]}));
  assert.equal(row.release.id,'episode-v1');
  assert.equal(row.state,'waiting_review');
  assert.equal(row.build_id,null);
  assert.equal(original.release,undefined);
});
test('latest delivered version wins without duplicating episodes, regardless of manifest order', () => {
  const rows=merge([],normalize({releases:[sample(2),sample(1)]}));
  assert.equal(rows.length,1);assert.equal(rows[0].release.version,2);
});
test('invalid and unfinished entries cannot create playable routes', () => {
  const invalid=[{...sample(),id:'../secret'},{...sample(),id:'x%2fsecret'},{...sample(),status:'rendering'},{...sample(),episode:0}];
  assert.deepEqual(normalize({releases:invalid}),[]);
});
test('media URLs are derived from a safe local identifier and ignore supplied external URLs', () => {
  const [r]=normalize({releases:[{...sample(),video_url:'https://example.com/leak',notes_url:'javascript:alert(1)'}]});
  assert.equal(r.video_url,'/static/finished-media/episode-v1/episode.mp4');
  assert.equal(r.notes_url,'/static/finished-media/episode-v1/notes.txt');
});
test('an empty release list preserves current accepted builds and historical episode rows', () => {
  const rows=[{show_id:7,season:1,episode:1,title:'旧片',build_id:'existing-accepted'}];
  assert.deepEqual(merge(rows,[]),rows);
});
test('v0 remains playable and v1 is preferred by edition rather than old artifact numbering',()=>{
  const {compare,label}=require('../static/finished-releases.js');
  const releases=normalize({releases:[{...sample(8),edition:'v0'}, {...sample(1),edition:'v1'},sample(0)]});
  assert.equal(releases.length,3);
  assert.equal(merge([],releases)[0].release.edition,'v1');
  assert.equal(merge([],releases)[0].release_versions.length,3);
  assert.ok(compare(releases[1],releases[0])>0);
  assert.match(label(releases[0]),/v0.*观看/);
});

test('finished episodes replace missing/download states in viewing boards without editing source records', () => {
  const {applyBoard,applyList}=require('../static/finished-releases.js');
  const show={id:42,mapping:{season:2},downloaded:1,unwatched:0,downloading:1,
    eps:[{n:1,s:'watched',media:'original'},{n:2,s:'missing'},{n:3,s:'downloading',progress:40},{n:4,s:'future',date:'2999-01-01'}],next:null};
  const before=JSON.stringify(show);
  const releases=[{...sample(0),edition:'v0'}, {...sample(1),edition:'v1'}, {...sample(1),id:'third-v0',episode:3,edition:'v0'}];
  const [result]=applyList([show],releases);
  assert.deepEqual(result.eps.map(e=>e.s),['watched','ready','ready','future']);
  assert.equal(result.next.n,2);assert.equal(result.next.release.edition,'v1');
  assert.equal(result.next.release_versions.length,2);
  assert.equal(result.downloaded,3);assert.equal(result.unwatched,2);assert.equal(result.downloading,0);
  assert.equal(result.eps[3].date,'2999-01-01');
  assert.equal(JSON.stringify(show),before);
  assert.deepEqual(applyBoard(show,{eps:show.eps,next:null},releases).eps,result.eps);
});

test('watched episodes stay watched, original media is retained and separate MP4 does not inherit resume time',()=>{
  const {applyBoard,applyList}=require('../static/finished-releases.js');
  const show={id:42,mapping:{season:2},downloaded:1,eps:[{n:2,s:'watched',media:'original'}]};
  const [result]=applyList([show],[sample()]);
  assert.equal(result.eps[0].s,'watched');assert.equal(result.eps[0].media,'original');
  assert.equal(result.downloaded,1);assert.equal(result.unwatched,0);assert.equal(result.next,null);
  const resumed=applyBoard(show,{eps:[{n:2,s:'resume',media:'original',position:123,duration:1420}]},[sample()]);
  assert.equal(resumed.next.s,'ready');assert.equal(resumed.next.position,undefined);
});

test('different show/season, invalid or unfinished files cannot change viewing availability',()=>{
  const {applyBoard}=require('../static/finished-releases.js');
  const show={id:42,mapping:{season:2}}, board={eps:[{n:2,s:'missing'}],next:null};
  for(const r of [{...sample(),show_id:1},{...sample(),season:1},{...sample(),status:'rendering'},{...sample(),id:'../escape'}])
    assert.equal(applyBoard(show,board,[r]),board);
  assert.equal(applyBoard(show,board,[]),board);
});

test('verification uses HEAD only and falls back to v0 when the v1 output is absent',async()=>{
  const {load,applyBoard}=require('../static/finished-releases.js');
  const original=global.fetch,calls=[];
  global.fetch=async(url,init)=>{
    if(url==='/api/finished-viewing')return {status:404};
    calls.push([url,init?.method || 'GET']);
    if(url.endsWith('index.json'))return {ok:true,json:async()=>({releases:[{...sample(1),edition:'v1'},{...sample(0),edition:'v0'}]})};
    return {ok:!url.includes('episode-v1/'),headers:new Headers({'content-length':'100','content-type':'video/mp4'})};
  };
  try{
    const releases=await load({verifyMedia:true});
    assert.equal(releases.length,1);assert.equal(releases[0].edition,'v0');
    assert.equal(applyBoard({id:42,mapping:{season:2}},{eps:[{n:2,s:'missing'}]},releases).next.release.id,'episode-v0');
    assert.deepEqual(calls.map(c=>c[1]),['GET','HEAD','HEAD']);
  }finally{global.fetch=original;}
});

test('home tickets and episode links open the delivered player before original media',()=>{
  const fs=require('node:fs'),vm=require('node:vm');
  const context=vm.createContext({addEventListener(){},window:{fetch:async()=>{}},document:{addEventListener(){}},matchMedia:()=>({matches:false})});
  vm.runInContext(fs.readFileSync('static/core.js','utf8'),context);
  context.episode={n:2,s:'ready',media:'original',release:normalize({releases:[sample()]})[0]};
  assert.equal(vm.runInContext('watchHref(episode)',context),'/static/finished-player.html?release=episode-v1');
  assert.match(vm.runInContext('epStrip({eps:[episode],next:episode})',context),/href="\/static\/finished-player.html\?release=episode-v1"/);
});

test('a future newly published episode needs no per-show code or episode download row',()=>{
  const {applyList}=require('../static/finished-releases.js');
  const show={id:987654,mapping:{season:7},eps:[{n:1,s:'watched'},{n:2,s:'missing'}],next:null};
  const future={...sample(0),id:'future-show-e2-v0',show_id:987654,season:7,edition:'v0'};
  assert.equal(applyList([show],[])[0].eps[1].s,'missing');
  const [published]=applyList([show],[future]);
  assert.equal(published.next.n,2);assert.equal(published.next.release.id,future.id);
  assert.equal(published.unwatched,1);assert.equal(show.eps[1].s,'missing');
});

function observerFixture() {
  const fs=require('node:fs'),vm=require('node:vm');
  const events={},timers=new Map();let rows=[],fail=false,id=0,reads=0;
  const host={document:{hidden:false,addEventListener:(name,fn)=>events[name]=fn,
    removeEventListener:name=>delete events[name]},
    setInterval(fn,ms){timers.set(++id,{fn,ms});return id;},clearInterval:key=>timers.delete(key),
    addEventListener:(name,fn)=>events[name]=fn,removeEventListener:name=>delete events[name]};
  const ctx=vm.createContext({window:host,AbortController,setTimeout,clearTimeout,
    fetch:async url=>{
      if(url==='/api/finished-viewing')return {status:404};
      if(url.endsWith('index.json')){reads++;if(fail)throw Error('offline');return {ok:true,json:async()=>({releases:rows})};}
      return {ok:true,headers:new Headers({'content-length':'100','content-type':'video/mp4'})};
    }});
  vm.runInContext(fs.readFileSync('static/finished-releases.js','utf8'),ctx);
  return {api:host.FinishedReleases,host,events,timers,setRows:r=>rows=r,setFail:f=>fail=f,reads:()=>reads};
}

test('open viewing lists discover newly published v0/v1 on visible timer or focus, without repeated renders',async()=>{
  const f=observerFixture(),changes=[];
  const stop=f.api.observe(rows=>changes.push(rows),{initial:[]});
  const timer=[...f.timers.values()][0];assert.equal(timer.ms,60000);
  await timer.fn();assert.equal(changes.length,0);
  f.setRows([{...sample(0),edition:'v0'}]);await timer.fn();assert.equal(changes.length,1);
  await timer.fn();assert.equal(changes.length,1);
  f.setRows([{...sample(0),edition:'v0'},{...sample(1),edition:'v1'}]);
  f.host.document.hidden=true;const reads=f.reads();await timer.fn();assert.equal(f.reads(),reads);
  f.host.document.hidden=false;await f.events.visibilitychange();assert.equal(changes.length,2);
  f.setFail(true);await f.events.focus();assert.equal(changes.length,2);
  stop();assert.equal(f.timers.size,0);assert.deepEqual(Object.keys(f.events),[]);
  await timer.fn();assert.equal(changes.length,2);
});

test('finished progress resumes its exact version while watched state stays shared',()=>{
  const {applyBoard,applyList}=require('../static/finished-releases.js');
  const show={id:42,state:'watching',mapping:{season:2},eps:[{n:1,s:'ready'},{n:2,s:'missing'}]};
  const v0={...sample(0),edition:'v0',finished:false,progress:{position:123,duration:1420,last_played:90}};
  const v1={...sample(1),edition:'v1',finished:false,progress:null};
  const r=applyBoard(show,show,[v0,v1]);
  assert.equal(r.next.n,2);assert.equal(r.next.release.id,v0.id);assert.equal(r.next.s,'resume');assert.equal(r.next.position,123);
  assert.equal(r.next.release_versions[0].id,v1.id);
  const [seen]=applyList([show],[{...v0,finished:true},{...v1,finished:true}]);
  assert.equal(seen.eps[1].s,'watched');assert.equal(seen.watched,1);assert.equal(seen.eps[1].release.id,v1.id);
  const unset=applyBoard(show,{eps:[{n:2,s:'watched'}]},[v0,v1]);
  assert.equal(unset.eps[0].s,'resume');
});

test('tracking attaches only to matching release id and content hash',async()=>{
  const {load}=require('../static/finished-releases.js'),original=global.fetch;
  const release={...sample(),video_sha256:'a'.repeat(64)};
  let hash='b'.repeat(64);
  global.fetch=async url=>({ok:true,json:async()=>url.endsWith('index.json')?{releases:[release]}:{releases:[{id:release.id,video_sha256:hash,finished:true,progress:{position:90,duration:100}}]}});
  try{
    assert.equal((await load())[0].progress,undefined);
    hash=release.video_sha256;
    const [loaded]=await load();assert.equal(loaded.finished,true);assert.equal(loaded.progress.position,90);
  }finally{global.fetch=original;}
});
