'use strict';
// Production smoke has no browser and exposes no write-capable request helper.
// Real play/seek/watch/pagehide acceptance belongs to the synthetic fixture.
const http=require('node:http'),fs=require('node:fs'),assert=require('node:assert/strict');
const {checkedBase,safeRead}=require('./player_qa_guard.cjs');
const base=checkedBase(process.env.PLAYER_QA_BASE||'http://127.0.0.1:4871','readonly');
const out=process.env.PLAYER_QA_OUT||'data/player-qa-readonly';
function get(path) {
  const url=new URL(path,base);assert.equal(url.origin,base);assert.ok(safeRead(url.pathname));
  return new Promise((resolve,reject)=>{
    const request=http.get(url,{timeout:5000},response=>{
      const chunks=[];let bytes=0;
      response.on('data',chunk=>{bytes+=chunk.length;if(bytes>2*1024*1024)request.destroy(new Error('Read-only response exceeds bound'));else chunks.push(chunk);});
      response.on('end',()=>resolve({status:response.statusCode,body:Buffer.concat(chunks).toString('utf8')}));
    });
    request.on('timeout',()=>request.destroy(new Error('Read-only smoke timed out')));request.on('error',reject);
  });
}
(async()=>{
  const status=await get('/api/status');assert.equal(status.status,200);assert.equal(JSON.parse(status.body).service,'AnimeService');
  const catalog=await get('/api/finished-viewing');assert.equal(catalog.status,200);
  const releases=JSON.parse(catalog.body).releases;assert.ok(Array.isArray(releases));
  const page=await get('/watch');assert.equal(page.status,200);assert.match(page.body,/artplayer\.js/);
  const capability=await get('/api/web/alignment-capabilities');assert.ok([200,404].includes(capability.status));
  let checked=0;
  for(const release of releases){
    const response=await get('/api/finished-viewing/'+encodeURIComponent(release.id));assert.equal(response.status,200);checked++;
  }
  const proof={scope:'read-only HTTP metadata and player entry; no browser playback',releases_checked:checked,
    alignment_capability_status:capability.status,write_requests:0,browser_started:false};
  fs.mkdirSync(out,{recursive:true});fs.writeFileSync(out+'/readonly-proof.json',JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
