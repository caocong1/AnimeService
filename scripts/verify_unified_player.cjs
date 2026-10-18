'use strict';
// All playback writes require the nonce-bound synthetic fixture.
const {chromium}=require('playwright'),fs=require('node:fs'),assert=require('node:assert/strict');
const {guardedContext,checkedBase}=require('./player_qa_guard.cjs');
const base=checkedBase(process.env.PLAYER_QA_BASE || 'http://127.0.0.1:4879','fixture'),out=process.env.PLAYER_QA_OUT;
assert.ok(out&&process.env.PLAYER_QA_TOKEN?.length>=24,'Run through run_player_qa.py');
const legacy=process.env.PLAYER_QA_LEGACY==='1';
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true,args:['--autoplay-policy=user-gesture-required']});
 const errors=[],writes=[],checks=[];let passed=false;
 const guard=await guardedContext(browser,{base,mode:'fixture',token:process.env.PLAYER_QA_TOKEN,mock:async route=>{
   const request=route.request(),path=new URL(request.url()).pathname;
   if(legacy && path.startsWith('/api/web/media/finished:')){await route.fulfill({status:404,json:{detail:'未找到已盘点的媒体'}});return true;}
   if(legacy && path==='/api/web/danmu/search'){await route.fulfill({json:{animes:[]}});return true;}
   if(!['GET','HEAD'].includes(request.method()))writes.push({path,body:request.postDataJSON()});
   if(/\/media\/[^/]+\/danmu$/.test(path)){
     await route.fulfill({json:{status:'matched',selected:[{id:'fixture',source_identity:'fixture',title:'测试弹幕',site:'bahamut'}],sources:[{id:'fixture',count:1,comments:[{time:1,text:'同一放映室弹幕验证',mode:0,color:'#ffffff'}]}],candidates:[]}});return true;
   }
   if(path==='/api/web/danmu/comments'){
     await route.fulfill({json:{sources:[{id:'fixture',count:1,comments:[{time:1,text:'同一放映室弹幕验证',mode:0,color:'#ffffff'}]}]}});return true;
   }
   return false;
 }});
 const context=guard.context;
 await context.addInitScript(legacy=>{
   const nativePlay=HTMLMediaElement.prototype.play;
   HTMLMediaElement.prototype.play=function(){return window.qaAllowPlayback ? nativePlay.call(this) : Promise.reject(new DOMException('QA waits for explicit playback','NotAllowedError'));};
   if(legacy){const release=new URLSearchParams(location.search).get('release');if(release)localStorage.setItem('fanyu-danmu-sources:finished:'+release,JSON.stringify([{id:'fixture',source_identity:'fixture',title:'测试弹幕'}]));}
 },legacy);
 const page=await context.newPage();
 page.on('pageerror',e=>errors.push(e.stack));
 const get=guard.get,fixture=await guard.fixture().catch(async error=>{
   await context.close();await browser.close();await guard.closeNetwork();throw error;
 }),index=fixture.releases;
 const v0=index.find(r=>r.show_id===fixture.show_id&&r.episode===2&&r.edition==='v0'),v1=index.find(r=>r.show_id===fixture.show_id&&r.episode===2&&r.edition==='v1');
 const record=id=>get('/api/finished-viewing/'+id);
 const ready=async()=>{await page.waitForFunction(()=>document.querySelector('#screen video')?.readyState>=2);};
 const open=async id=>{await page.goto(base+'/watch?release='+id);await ready();};
 const source=async href=>{await page.selectOption('#playback-source',href);await page.waitForURL(base+href);await ready();};
 const pausedSeek=async time=>{await page.locator('#screen video').evaluate(async(v,t)=>{v.pause();v.currentTime=t;await new Promise(r=>v.addEventListener('seeked',r,{once:true}));},time);};
 const play=async()=>{await page.locator('#screen video').evaluate(async v=>{window.qaAllowPlayback=true;v.muted=true;await v.play();});await page.waitForFunction(()=>!document.querySelector('#screen video').paused);};
 const wait=async fn=>{for(let i=0;i<80;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw Error('condition timeout');};
 try{
   await page.goto(base+'/');await page.waitForSelector(`a.ticket[href="/watch?release=${v1.id}"]`);
   await page.locator(`a.ticket[href="/watch?release=${v1.id}"]`).first().click();await ready();
   assert.equal(new URL(page.url()).pathname,'/watch');assert.ok(await page.locator('#open-danmu-search').isVisible());
   await page.waitForFunction(()=>document.querySelector('#danmu-list-count').textContent==='1');
   await page.click('#tab-danmu-list');assert.match(await page.locator('#danmu-list').textContent(),/同一放映室弹幕验证/);
   checks.push('home opens normal Artplayer with shared danmu list');
   await source('/watch?release='+v0.id);
   assert.equal((await record(v0.id)).progress,null);
   await pausedSeek(300);assert.equal((await record(v0.id)).progress,null);checks.push('open and unplayed seek do not save');
   await pausedSeek(0);await play();await page.waitForFunction(()=>document.querySelector('#screen video').currentTime>.7);
   await page.locator('#screen video').evaluate(async v=>{v.currentTime=123;await new Promise(r=>v.addEventListener('seeked',r,{once:true}));v.pause();});
   await wait(async()=>Math.abs((await record(v0.id)).progress?.position-123)<2);
   assert.equal((await record(v1.id)).progress,null);checks.push('pause saves exact version independently');
   await page.goto(base+'/');await page.waitForSelector(`a.ticket[href="/watch?release=${v0.id}"]`);
   await page.locator(`a.ticket[href="/watch?release=${v0.id}"]`).click();await ready();
   await page.waitForFunction(()=>document.querySelector('#screen video').currentTime>122);checks.push('home resumes original watched version');
   await source('/watch?release='+v1.id);
   assert.ok(await page.locator('#screen video').evaluate(v=>v.currentTime<2));
   const original=await page.locator('#playback-source option').evaluateAll(rows=>rows.find(r=>r.textContent==='原视频')?.value);assert.ok(original);
   await source(original);
   assert.match(page.url(),/watch\?media=/);assert.ok(await page.locator('#open-danmu-search').isVisible());
   await source('/watch?release='+v0.id);
   await page.waitForFunction(()=>document.querySelector('#screen video').currentTime>122);checks.push('v0/v1/original source switching uses same page and independent progress');
   await page.click('#acts [data-mark="true"]');await page.waitForSelector('#acts [data-mark="false"]');
   assert.ok((await record(v1.id)).finished);
   await open(v0.id);assert.ok(await page.locator('#screen video').evaluate(v=>v.currentTime<2));
   await page.click('#acts [data-mark="false"]');await page.waitForSelector('#acts [data-mark="true"]');
   await page.reload();await ready();await page.waitForFunction(()=>document.querySelector('#screen video').currentTime>122);
   checks.push('mark/unmark share watches; watched replay starts from zero');
   await play();await page.locator('#screen video').evaluate(v=>{v.currentTime=v.duration-.25;});
   await page.waitForFunction(()=>document.querySelector('#screen video').ended);
   assert.equal((await record(v0.id)).finished,false);assert.match(await page.locator('#screen-state').textContent(),/标记看完/);checks.push('ended requires explicit watch confirmation');
   await page.click('#dismiss');await pausedSeek(0);await play();await page.waitForFunction(()=>document.querySelector('#screen video').currentTime>.7);
   await page.locator('#screen video').evaluate(async v=>{v.currentTime=456;await new Promise(r=>v.addEventListener('seeked',r,{once:true}));});
   await page.goto(base+'/');await wait(async()=>Math.abs((await record(v0.id)).progress?.position-456)<2);checks.push('leaving saves playing position');
   await page.goto(base+'/static/finished-player.html?release='+v0.id);await ready();assert.match(page.url(),/\/watch\?release=/);checks.push('legacy links redirect to normal room');
   await page.locator('#screen video').evaluate(async v=>{window.qaAllowPlayback=true;v.muted=true;const now=Date.now;Date.now=()=>now()+16000;await v.play();});
   await wait(async()=>(await record(v0.id)).progress?.position>456);await page.locator('#screen video').evaluate(v=>v.pause());checks.push('periodic playback saves');
   await page.screenshot({path:out+'/desktop.png',fullPage:true});await page.setViewportSize({width:390,height:900});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:out+'/mobile.png',fullPage:true});checks.push('mobile has no horizontal overflow');
   assert.deepEqual(errors,[]);
   assert.equal(guard.ledger.blocked.filter(r=>/\/(?:progress|watch|viewing-session)$/.test(r.path)).length,0,'critical fixture writes must reach the real isolated API');
   passed=true;
 }catch(e){await page.screenshot({path:out+'/failure.png',fullPage:true});console.error(JSON.stringify({url:page.url(),errors,writes,network:guard.ledger,screen:await page.locator('#screen-state').textContent({timeout:1000}).catch(()=>''),sources:await page.locator('#playback-source').innerHTML({timeout:1000}).catch(()=>'' )}));throw e;}
 finally{
   await page.goto('about:blank').catch(()=>{});await context.close();await browser.close();await guard.closeNetwork();
   if(!passed)fs.writeFileSync(out+'/failure-proof.json',JSON.stringify({passed:false,checks,writes,errors,network:guard.ledger},null,2));
   if(passed){
     assert.deepEqual(errors,[]);
     assert.equal([...guard.ledger.blocked,...guard.ledger.transport_blocked].filter(r=>/\/(?:progress|watch|viewing-session)$/.test(r.path)).length,0,'late critical fixture writes must reach the isolated API');
     fs.writeFileSync(out+(legacy?'/fixture-compat-proof.json':'/fixture-proof.json'),JSON.stringify({isolated:true,synthetic_media:true,legacy,checks,writes,errors,network:guard.ledger},null,2));
     console.log(JSON.stringify({legacy,checks,errors,writes:writes.length,network:guard.ledger}));
   }
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
