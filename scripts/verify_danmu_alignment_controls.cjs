'use strict';
// All requests are fulfilled locally; no production media, API or viewing writes.
const {chromium} = require('playwright');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const out = 'data/diagnostics/danmu-alignment-controls-20261016';
(async () => {
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const checks = [], errors = [], unexpected = [];
  try {
    for (const mode of ['site', 'url', 'title', 'bahamut', 'access-blocked', 'iqiyi', 'old-backend', 'finished', 'legacy']) {
      const context = await browser.newContext({viewport:{width:1440,height:1000}});
      await context.addInitScript(() => { HTMLMediaElement.prototype.play = function () { return Promise.resolve(); }; });
      const finished = ['finished','legacy'].includes(mode), key = finished ? 'finished:fixture-release' : 'fixture';
      const source = {id:'bili',source_identity:'stable',title:'第15集',site:'bilibili'};
      if (mode === 'title') {delete source.site;source.title='第15集 from bilibili';}
      if (mode === 'url') {delete source.site;source.source_url='https://www.bilibili.com/video/BV1EVap6yE6B?p=1';}
      if (['bahamut','access-blocked','old-backend'].includes(mode)) source.site='bahamut';
      if (mode === 'iqiyi') source.site='iqiyi';
      const release = {id:'fixture-release',status:'finished',show_id:1,season:1,episode:15,version:1,edition:'v1',duration_seconds:1420,video_sha256:'fixture-sha'};
      let starts = 0, forced = 0;
      await context.route('**/*', async route => {
        const req=route.request(), p=new URL(req.url()).pathname;
        const json = data => route.fulfill({json:data});
        if (p === '/watch') return route.fulfill({contentType:'text/html',body:fs.readFileSync('static/watch.html','utf8')});
        if (p === '/static/finished-media/index.json') return json({releases:finished ? [release] : []});
        if (p.startsWith('/static/finished-media/')) return route.fulfill({status:200,contentType:'video/mp4',headers:{'content-length':'100'},body:Buffer.alloc(req.method()==='HEAD'?0:100)});
        if (p.startsWith('/static/')) return route.fulfill({path:path.join(process.cwd(),p)});
        if (p === '/api/bootstrap') return json({local:false,settings:{}});
        if (p === '/api/web/alignment-capabilities') return mode==='old-backend' ? route.fulfill({status:404,json:{detail:'old backend'}}) : json({sites:['bilibili','bahamut']});
        if (p === '/api/status') return json({events:[],heartbeat:{time:Date.now()/1000}});
        if (p === '/api/finished-viewing') return json({releases:[]});
        if (p === '/api/finished-viewing/fixture-release') return json({show_id:1,episode:15,video_sha256:'fixture-sha',progress:null,finished:false});
        if (p.endsWith('/viewing-session')) return json({ok:true});
        if (p === '/api/web/media/'+key) return mode==='legacy' ? route.fulfill({status:404,json:{detail:'not loaded'}}) : json({id:key,name:'fixture.mp4',show_id:1,episode:15,folder:'测试',...(finished?{release_id:release.id,release}:{})});
        if (p === '/api/shows/1') return json({show:{id:1,title:'测试作品'},board:{eps:[{n:15,s:'ready',media:'fixture'}]}});
        if (p.endsWith('/subtitles')) return json({tracks:[],default:null});
        if (p.endsWith('/audio-tracks')) return json({tracks:[]});
        if (p.endsWith('/picture')) return json({width:1920,height:1080});
        if (p.endsWith('/alignment')) {
          starts++;
          if(mode==='access-blocked') {
            if(req.postDataJSON().force) forced++;
            return json({status:'unavailable',reason:'bahamut_limited',retryable:false,message:'巴哈姆特限制了当前音轨访问（1018），已暂停尝试30分钟，保留原偏移'});
          }
          if(req.postDataJSON().force) {forced++;return json({status:'matched',offset:12.5});}
          return json({status:'unreliable'});
        }
        if (p.endsWith('/danmu')) return json({status:'matched',selected:[source],sources:[{id:'bili',count:1,comments:[{time:10,text:'测试弹幕',mode:0,color:'#ffffff'}]}]});
        if (p === '/api/web/danmu/comments') return json({sources:[{...source,count:1,comments:[{time:10,text:'测试弹幕',mode:0,color:'#ffffff'}]}]});
        // Compatibility mode restores an explicitly chosen source, no identity search needed.
        if (p === '/api/web/danmu/search') return json({animes:[]});
        if (/\/stream$/.test(p)) return route.abort();
        unexpected.push({mode,path:p,method:req.method()});return route.abort();
      });
      if (mode === 'legacy') await context.addInitScript(source => localStorage.setItem('fanyu-danmu-sources:finished:fixture-release', JSON.stringify([source])), source);
      const page=await context.newPage();page.on('pageerror',e=>errors.push({mode,message:e.message}));
      await page.goto('http://fixture.test/watch?'+(finished?'release=fixture-release':'media=fixture'));
      await page.waitForSelector('[data-align-source]',{state:'attached'});
      const button=page.locator('[data-align-source="0"]'), offset=page.locator('[data-timing="0"][data-field="offset"]');
      assert.equal(await button.isVisible(),true,mode);
      if (mode==='access-blocked') {
        await page.waitForFunction(()=>document.querySelector('.dm-alignment')?.textContent.includes('1018'));
        assert.equal(await button.isEnabled(),true);assert.equal(starts,1);
        assert.equal(await offset.inputValue(),'0');
        await offset.fill('9');await offset.dispatchEvent('change');await offset.blur();
        await button.click();await page.waitForFunction(()=>document.querySelector('.dm-alignment')?.textContent.includes('1018'));
        assert.equal(await offset.inputValue(),'9');assert.equal(starts,2);
      } else if (['iqiyi','old-backend','legacy'].includes(mode)) {
        assert.equal(await button.isDisabled(),true);
        assert.match(await page.locator('.dm-alignment').innerText(),mode==='legacy'?/等待播放空闲/:mode==='old-backend'?/等待服务空闲更新/:/目前支持 B站视频和巴哈/);
        assert.equal(starts,0);
        await offset.fill('3');await offset.dispatchEvent('change');assert.equal(await offset.inputValue(),'3');
      } else {
        await page.waitForFunction(()=>document.querySelector('.dm-alignment')?.textContent.includes('未能可靠匹配'));
        await button.click();await page.waitForFunction(()=>document.querySelector('[data-field="offset"]').value==='12.5');
        assert.equal(forced,1);
        assert.match(await page.locator('.dm-alignment').innerText(),/已自动对齐 \+12.5/);
        await offset.fill('7');await offset.dispatchEvent('change');
        await offset.blur();await page.waitForFunction(()=>document.querySelector('[data-align-source]').textContent==='自动对轴');
        assert.equal(await button.innerText(),'自动对轴');
        await button.click();await page.waitForFunction(()=>document.querySelector('[data-field="offset"]').value==='12.5');
        await page.locator('[data-disable-alignment="0"]').click();
        assert.match(await page.locator('.dm-alignment').innerText(),/已关闭/);
        await button.click();await page.waitForFunction(()=>document.querySelector('[data-field="offset"]').value==='12.5' && document.querySelector('.dm-alignment').textContent.includes('已自动对齐'));
        const saved=await page.evaluate(key=>JSON.parse(localStorage.getItem('fanyu-danmu-sources:'+key)),key);
        if (mode === 'site') assert.equal(saved[0].site,'bilibili');
      }
      await page.locator('#danmu-selected').screenshot({path:`${out}/${mode}-desktop.png`});
      await page.setViewportSize({width:390,height:844});
      assert.equal(await button.isVisible(),true);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.locator('#danmu-selected').screenshot({path:`${out}/${mode}-mobile.png`});
      checks.push({mode,starts,forced,visible:true,mobileOverflow:false});await context.close();
    }
    assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
    fs.writeFileSync(`${out}/browser.json`,JSON.stringify({checks,errors,unexpected,productionRequests:0},null,2));
    console.log(JSON.stringify({checks,errors,unexpected}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
