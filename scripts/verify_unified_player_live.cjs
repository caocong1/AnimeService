'use strict';
// Read-only live smoke: fetch-level blocking also catches pagehide keepalive writes.
const {chromium}=require('playwright'),fs=require('node:fs'),assert=require('node:assert/strict');
const base=process.env.PLAYER_QA_BASE || 'http://127.0.0.1:4871',out='data/unified-player-20261016';
(async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.addInitScript(()=>{
   const original=window.fetch.bind(window);
   window.fetch=(input,init={})=>{
     const url=new URL(typeof input==='string'?input:input.url,location.href),method=(init.method || input?.method || 'GET').toUpperCase();
     if(url.pathname.startsWith('/api/') && !['GET','HEAD'].includes(method)) {
       if(url.pathname==='/api/web/danmu/comments')return original(input,init);
       return Promise.resolve(new Response('{"ok":true}',{status:200,headers:{'Content-Type':'application/json'}}));
     }
     return original(input,init);
   };
 });
 const page=await context.newPage(),errors=[],versions=[];page.on('pageerror',e=>errors.push(e.message));
 const available = new Set((await (await context.request.get(base+'/api/finished-viewing')).json()).releases.map(r=>r.id));
 const index=JSON.parse(fs.readFileSync('static/finished-media/index.json','utf8')).releases.filter(r=>r.status==='finished'&&available.has(r.id));
 assert.ok(index.length);
 try {
   await page.goto(base+'/');await page.waitForSelector('.screening a.ticket[href*="/watch?release="]');
   const home=await page.locator('.screening a.ticket[href*="/watch?release="]').evaluateAll(rows=>rows.map(r=>r.getAttribute('href')));assert.ok(home.length);
   for(const sid of [...new Set(index.map(r=>r.show_id))]) {
     await page.goto(base+'/show/'+sid);await page.waitForSelector('.ep-row a[href*="/watch?release="]');
     for(const r of index.filter(r=>r.show_id===sid)) {
       const href='/watch?release='+r.id;
       const row=page.locator('.ep-row').filter({has:page.locator('.no',{hasText:new RegExp('^'+r.episode+'$')})});
       assert.equal(await row.locator('.acts a').count(),1,'one playback button per episode');
       await row.locator('a[href*="/watch?release="]').first().click();
       await page.waitForFunction(()=>document.querySelector('#screen video')?.readyState>=2);
       if(new URL(page.url()).searchParams.get('release')!==r.id) {
         await page.selectOption('#playback-source',href);await page.waitForURL(base+href);
         await page.waitForFunction(()=>document.querySelector('#screen video')?.readyState>=2);
       }
       assert.equal(new URL(page.url()).pathname,'/watch');assert.ok(await page.locator('#open-danmu-search').isVisible());
       await page.locator('#screen video').evaluate(async v=>{v.muted=true;await v.play();});
       await page.waitForFunction(()=>!document.querySelector('#screen video').paused);
       await page.locator('#screen video').evaluate(v=>{v.pause();v.currentTime=60;});
       await page.waitForFunction(()=>Math.abs(document.querySelector('#screen video').currentTime-60)<1&&!document.querySelector('#screen video').seeking);
       const video=await page.locator('#screen video').evaluate(v=>({src:v.currentSrc,duration:v.duration,error:v.error?.code || null}));
       assert.ok(video.duration>1200&&!video.error);assert.equal(await page.locator('#playback-source').inputValue(),href);
       versions.push({id:r.id,show_id:sid,href,video});
       if(versions.length===1) {
         await page.screenshot({path:out+'/live-desktop.png',fullPage:true});await page.setViewportSize({width:390,height:900});
         assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
         await page.screenshot({path:out+'/live-mobile.png',fullPage:true});await page.setViewportSize({width:1440,height:1000});
       }
       await page.goto(base+'/show/'+sid);await page.waitForSelector('.ep-row a[href*="/watch?release="]');
     }
   }
   // The default shelf page may contain no finished releases; search a real show.
   await page.goto(base+'/?view=all&q='+encodeURIComponent(index[0].title));
   await page.waitForSelector('.shelf-row a.ticket[href*="/watch?release="]');
   await page.goto(base+'/static/finished-player.html?release='+index[0].id);await page.waitForURL(base+'/watch?release='+index[0].id);
   assert.deepEqual(errors,[]);
   fs.writeFileSync(out+(new URL(base).port==='4871'?'/live-proof.json':'/fixture-readonly-proof.json'),JSON.stringify({base,home,versions,errors,watch_writes:'blocked in fetch before application code'},null,2));
   console.log(JSON.stringify({versions:versions.length,errors}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
