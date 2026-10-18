'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const Writer=require('../static/playback-writer.js');
const guard=require('../scripts/player_qa_guard.cjs');
const source=fs.readFileSync(require.resolve('../static/watch.js'),'utf8');
const KEY='a'.repeat(32),gate=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};
function body(name) {
 const start=source.indexOf(`  async function ${name}(`);assert.ok(start>=0);
 return source.slice(start,source.indexOf('\n  }',start)+4);
}
function setup() {
 const nodes={};let fetches=0;
 const context=vm.createContext({App:{capabilities:{playback_writer:{protocol:'playback-writer/v2',enabled:true}},token:'synthetic'},
  window:{PlaybackWriter:Writer},PlaybackWriter:Writer,writerChosen:false,writerSetupEpoch:0,writer:null,switching:false,
  writerLabel:()=> 'Synthetic',viewingSession:'synthetic-session-1234',writerKey:async()=>null,withTimeout:p=>p,
  $:name=>nodes[name]??(nodes[name]={hidden:true,textContent:''}),renderWriter(){},
  audioSwitching:false,played:true,art:{duration:100,currentTime:10,video:{paused:false}},
  finishedEndpoint:null,moved:false,lastPosition:0,legacyFenced:false,id:KEY,progressQueue:Promise.resolve(),sequence:0,
  tracking(){},console,Date,Number,JSON,Promise,
  fetch:async()=>{fetches++;return {status:200,ok:true,json:async()=>({})};}});
 vm.runInContext(body('openWriter')+'\n'+body('saveProgress'),context);
 return {context,nodes,get fetches(){return fetches;}};
}
test('v2 choice is sticky during lookup and after failed version verification: no legacy writes',async()=>{
 const x=setup(),pending=gate();x.context.writerKey=()=>pending.promise;
 const opening=vm.runInContext('openWriter()',x.context);
 assert.equal(x.context.writerChosen,true);
 await vm.runInContext('saveProgress()',x.context);assert.equal(x.fetches,0);
 pending.resolve(null);await opening;
 await vm.runInContext('saveProgress(true)',x.context);assert.equal(x.fetches,0);
 assert.match(x.nodes['#writer-status'].textContent,/无法核验/);
});
test('capability off makes no writer lookup and preserves legacy progress',async()=>{
 const x=setup();x.context.App.capabilities={};let lookups=0;x.context.writerKey=async()=>{lookups++;return KEY;};
 await vm.runInContext('openWriter()',x.context);await vm.runInContext('saveProgress()',x.context);
 assert.equal(lookups,0);assert.equal(x.fetches,1);
});
test('older asynchronous setup cannot overwrite a newer writer instance',async()=>{
 const x=setup(),a=gate(),b=gate();let calls=0,created=0;
 x.context.writerKey=()=>++calls===1?a.promise:b.promise;
 x.context.PlaybackWriter={create(){created++;return {subscribe(){},state:async()=>({})};}};
 x.context.window.PlaybackWriter=x.context.PlaybackWriter;
 const first=vm.runInContext('openWriter()',x.context),second=vm.runInContext('openWriter()',x.context);
 b.resolve(KEY);await second;const current=x.context.writer;
 a.resolve(KEY);await first;assert.equal(x.context.writer,current);assert.equal(created,1);
});
test('legacy 409 stops packets that were already queued, not just future calls',async()=>{
 const x=setup(),response=gate();let calls=0;
 x.context.fetch=async()=>{calls++;return response.promise;};
 const a=vm.runInContext('saveProgress()',x.context),b=vm.runInContext('saveProgress()',x.context);
 response.resolve({status:409,ok:false,json:async()=>({reason:'session_required'})});
 await Promise.all([a,b]);assert.equal(calls,1);assert.equal(x.context.legacyFenced,true);
});
test('missing echo is a protocol failure and a halted instance cannot claim again',async()=>{
 let calls=0;const w=Writer.create({mediaKey:KEY,session:'synthetic-session-1234',device:'Synthetic',random:n=>'a'.repeat(n*2),
  get:async()=>({status:200,ok:true,body:{media_key:KEY,generation:0,active:false,expires:0}}),
  post:async()=>{calls++;return {status:200,ok:true,body:{ok:true,media_key:KEY,generation:1,expires:99}};}});
 assert.equal((await w.claim()).reason,'protocol_200');assert.equal((await w.claim()).reason,'halted');assert.equal(calls,1);
 const s=await w.state();assert.equal(s.reason,'invalid_state');assert.equal(w.status,'halted');
});
test('v2 QA paths remain confined to the nonce verified nonproduction fixture',()=>{
 assert.equal(guard.safeRead('/api/playback-sessions/state',false),false);
 assert.equal(guard.safeRead('/api/playback-sessions/state',true),true);
 for(const action of ['claim','progress','heartbeat','release']) assert.equal(guard.fixtureWrite('/api/playback-sessions/'+action),true);
 assert.equal(guard.fixtureWrite('/api/playback-sessions/enable'),false);
 assert.throws(()=>guard.checkedBase('http://127.0.0.1:4871','fixture'));
});
test('legacy fence does not depend on successful loading of the optional writer asset',async()=>{
 const x=setup();x.context.PlaybackWriter=undefined;let calls=0;
 x.context.fetch=async()=>{calls++;return {status:409,ok:false,json:async()=>({reason:'session_required'})};};
 await vm.runInContext('saveProgress()',x.context);await vm.runInContext('saveProgress()',x.context);
 assert.equal(calls,1);assert.equal(x.context.legacyFenced,true);
});
test('replayed claim verification cannot erase a protocol halt and permit another claim',async()=>{
 let posts=0;const w=Writer.create({mediaKey:KEY,session:'synthetic-session-1234',device:'Synthetic',random:n=>'a'.repeat(n*2),
  post:async()=>{posts++;return {status:200,ok:true,body:{ok:true,requested_media_key:KEY,media_key:KEY,generation:1,expires:99,replayed:true}};},
  get:async()=>({status:503,ok:false,body:{reason:'disabled'}})});
 assert.equal((await w.claim()).reason,'verify_failed');assert.equal(w.status,'unsupported');
 assert.equal((await w.claim()).reason,'unsupported');assert.equal(posts,1);
});
test('loaded version binding rejects missing SHA on both sides and malformed canonical keys',async()=>{
 const x=setup();x.context.releaseId='synthetic-release';x.context.media={release:{}};
 let record={media_id:'finished:'+KEY};x.context.api=async()=>record;
 vm.runInContext(body('writerKey'),x.context);
 assert.equal(await vm.runInContext('writerKey()',x.context),null);
 x.context.media.release.video_sha256='a'.repeat(64);record.video_sha256='a'.repeat(64);
 assert.equal(await vm.runInContext('writerKey()',x.context),'finished:'+KEY);
 record.media_id='not-a-canonical-key';assert.equal(await vm.runInContext('writerKey()',x.context),null);
});
