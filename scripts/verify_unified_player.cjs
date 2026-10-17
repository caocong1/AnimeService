'use strict';
// Browser writes are restricted to the dedicated fixture DB on 4879.
const {chromium}=require('playwright'),fs=require('node:fs'),assert=require('node:assert/strict');
const base=process.env.PLAYER_QA_BASE || 'http://127.0.0.1:4879',out='data/unified-player-20261016';
const legacy=process.env.PLAYER_QA_LEGACY==='1';
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true,args:['--autoplay-policy=user-gesture-required']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.addInitScript(legacy=>{
   const nativePlay=HTMLMediaElement.prototype.play;
   HTMLMediaElement.prototype.play=function(){return window.qaAllowPlayback ? nativePlay.call(this) : Promise.reject(new DOMException('QA waits for explicit playback','NotAllowedError'));};
   if(legacy){const release=new URLSearchParams(location.search).get('release');if(release)localStorage.setItem('fanyu-danmu-sources:finished:'+release,JSON.stringify([{id:'fixture',source_identity:'fixture',title:'测试弹幕'}]));}
 },legacy);
 const page=await context.newPage(),errors=[],writes=[],checks=[];
 page.on('pageerror',e=>errors.push(e.stack));
 await context.route('**/api/**',async route=>{
   const request=route.request(),path=new URL(request.url()).pathname;
   if(legacy && path.startsWith('/api/web/media/finished:'))return route.fulfill({status:404,json:{detail:'未找到已盘点的媒体'}});
   if(legacy && path==='/api/web/danmu/search')return route.fulfill({json:{animes:[]}});
   if(!['GET','HEAD'].includes(request.method())) {
     assert.equal(new URL(request.url()).port,'4879','no production writes');
     writes.push({path,body:request.postDataJSON()});
   }
   if(/\/media\/[^/]+\/danmu$/.test(path))return route.fulfill({json:{status:'matched',selected:[{id:'fixture',source_identity:'fixture',title:'测试弹幕',site:'bahamut'}],sources:[{id:'fixture',count:1,comments:[{time:1,text:'同一放映室弹幕验证',mode:0,color:'#ffffff'}]}],candidates:[]}});
   if(path==='/api/web/danmu/comments')return route.fulfill({json:{sources:[{id:'fixture',count:1,comments:[{time:1,text:'同一放映室弹幕验证',mode:0,color:'#ffffff'}]}]}});
   return route.continue();
 });
 const get=async path=>(await context.request.get(base+path)).json();
 const fixture=await get('/api/testing/fixture');assert.ok(fixture.isolated);assert.match(fixture.database,/tracking-test-/);
 const index=JSON.parse(fs.readFileSync('static/finished-media/index.json','utf8')).releases;
 const v0=index.find(r=>r.show_id===639938&&r.episode===2&&r.edition==='v0'),v1=index.find(r=>r.show_id===639938&&r.episode===2&&r.edition==='v1');
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
   fs.writeFileSync(out+(legacy?'/fixture-compat-proof.json':'/fixture-proof.json'),JSON.stringify({fixture,legacy,checks,writes,errors},null,2));console.log(JSON.stringify({legacy,checks,errors,writes:writes.length}));
 }catch(e){await page.screenshot({path:out+'/failure.png',fullPage:true});console.error(JSON.stringify({url:page.url(),errors,writes,screen:await page.locator('#screen-state').textContent().catch(()=>''),sources:await page.locator('#playback-source').innerHTML().catch(()=>'' )}));throw e;}
 finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
