'use strict';
/* GET-only browser gate: real content plus decoded, painted visible covers. */
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PAGE_QA_PLAYWRIGHT||'playwright');
const base=new URL(process.env.PAGE_QA_BASE||'http://localhost:4871');
if(!['localhost','127.0.0.1','[::1]'].includes(base.hostname))throw Error('Use a local, authorized service');
const samples=Number(process.env.PAGE_QA_SAMPLES||5);
if(!Number.isInteger(samples)||samples<1||samples>30)throw Error('Invalid sample count');
const out=path.resolve(process.env.PAGE_QA_OUT||'data/diagnostics/page-performance-latest.json');
const cases=[['home','/','#screens .screening,#screens .empty'],
 ['following','/?view=all','#shelf .shelf-row,#shelf .empty'],
 ...String(process.env.PAGE_QA_SHOWS||'554779,558064').split(',').map(id=>{
  if(!/^\d+$/.test(id))throw Error('Invalid show ID');return ['show-'+id,'/show/'+id,'.show-hero'];
 })];
if(process.argv.includes('--extended'))cases.push(
 ['season','/season','#list .poster,#list .empty'],
 ['library','/library','#file-list .folder,#file-list .empty'],
 ['manage','/manage','#sec-status .tiles'],
 ['subtitle-queue','/subtitle-queue','#queue .queue-section,#queue .empty'],
 ['subtitle-library','/subtitle-library','#subtitle-records .subtitle-entry,#subtitle-records .empty']);
const percentile=(values,p)=>[...values].sort((a,b)=>a-b)[Math.max(0,Math.ceil(values.length*p)-1)];
async function measure(browser,[name,route,selector],onMeasured=()=>{}){
 let context,response,result,stage='context';
 const errors=[],writes=[],failedCovers=[],requests=[],pending=new Map();
 const updateRequest=(r,details)=>{
  const row=pending.get(r);if(!row)return;
  Object.assign(row,details);
  try{row.timing=r.timing();}catch(e){row.recording_error=e.message;}
 };
 try{
 context=await browser.newContext({viewport:{width:1440,height:1000}});
 stage='page';const page=await context.newPage();
 // Avoid routing/intercepting every asset: it disables browser caching and
 // adds automation latency. Block application writes before they are sent.
 page.on('request',r=>{
  const row={url:r.url(),method:r.method(),type:r.resourceType(),issued_at_ms:Date.now()};
  pending.set(r,row);requests.push(row);
  if(!['GET','HEAD'].includes(r.method()))writes.push({method:r.method(),url:new URL(r.url()).pathname});
 });
 page.on('requestfinished',r=>updateRequest(r,{finished_at_ms:Date.now()}));
 stage='setup';
 if(process.env.PAGE_QA_BLOCK_EXTERNAL_IMAGES==='1'){
  const cdp=await context.newCDPSession(page);await cdp.send('Network.enable');
  await cdp.send('Network.setBlockedURLs',{urls:['*://lain.bgm.tv/*']});
 }
 page.on('pageerror',e=>errors.push(e.message));
 page.on('requestfailed',r=>{
  updateRequest(r,{failed_at_ms:Date.now(),failure:r.failure()});
  if(r.url().includes('/covers/')||r.url().startsWith('https://lain.bgm.tv/'))failedCovers.push(r.failure()?.errorText||'failed');
 });
 page.on('response',r=>{
  updateRequest(r.request(),{status:r.status(),response_at_ms:Date.now()});
  if(r.url().includes('/covers/')&&r.status()>=400)failedCovers.push(r.status());
 });
 await context.addInitScript(({selector})=>{
  window.__blockedWrites=[];
  const deny=method=>{window.__blockedWrites.push(String(method));throw Error('Performance check is read-only');};
  const originalFetch=window.fetch.bind(window);
  window.fetch=(url,options={})=>{
   const method=(options.method||(url instanceof Request?url.method:'GET')).toUpperCase();
   return ['GET','HEAD'].includes(method)?originalFetch(url,options):Promise.reject(deny(method));
  };
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,...args){this.__qaMethod=String(method).toUpperCase();return open.call(this,method,...args);};
  XMLHttpRequest.prototype.send=function(...args){if(!['GET','HEAD'].includes(this.__qaMethod))return deny(this.__qaMethod);return send.apply(this,args);};
  navigator.sendBeacon=()=>deny('BEACON');
  document.addEventListener('submit',e=>{e.preventDefault();deny('FORM');},true);
  window.__pagePerformance=new Promise(resolve=>{
   const longTasks=[];
   try{new PerformanceObserver(list=>longTasks.push(...list.getEntries().map(e=>({start_ms:e.startTime,duration_ms:e.duration}))))
    .observe({type:'longtask',buffered:true});}catch{}
   let started=false;
   const observer=new MutationObserver(()=>{
    if(started||!document.querySelector(selector))return;
    started=true;observer.disconnect();
    requestAnimationFrame(()=>requestAnimationFrame(async()=>{
     const contentMs=performance.now();
     const visible=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;};
     // Retain references: removing a failed image cannot turn failure into success.
     const covers=[...document.querySelectorAll('.cover img')].filter(visible),limit=performance.now()+10000;
     while(covers.some(i=>!i.complete||!i.naturalWidth)){
      if(performance.now()>limit){resolve({content_ms:contentMs,ready_ms:null,error:'Visible cover did not load'});return;}
      await new Promise(requestAnimationFrame);
     }
     try{await Promise.all(covers.map(i=>i.decode()));}catch{
      resolve({content_ms:contentMs,ready_ms:null,error:'Cover decode failed'});return;
     }
     const finish=()=>resolve({
      started_at:performance.timeOrigin,content_ms:contentMs,ready_ms:performance.now(),visible_covers:covers.length,
      covers:covers.map(i=>({src:i.currentSrc,width:i.naturalWidth,height:i.naturalHeight})),
      content_error:/读取失败|载入失败|加载失败/.test(document.querySelector(selector)?.textContent||''),
      blocked_writes:window.__blockedWrites,
      navigation:performance.getEntriesByType('navigation').map(r=>({
       dns_ms:r.domainLookupEnd-r.domainLookupStart,connect_ms:r.connectEnd-r.connectStart,
       request_ms:r.requestStart,first_byte_ms:r.responseStart,end_ms:r.responseEnd})),long_tasks:longTasks,
      resources:performance.getEntriesByType('resource')
       .map(r=>({url:r.name,start_ms:r.startTime,duration_ms:r.duration,bytes:r.transferSize,
        server_timing:(r.serverTiming||[]).map(t=>({name:t.name,ms:t.duration})),
        dns_ms:r.domainLookupEnd-r.domainLookupStart,connect_ms:r.connectEnd-r.connectStart,
        request_ms:r.requestStart,first_byte_ms:r.responseStart,end_ms:r.responseEnd})),
     });
     // The content was already observed after two frames. Text-only pages
     // need no second image-paint wait.
     if(covers.length)requestAnimationFrame(()=>requestAnimationFrame(finish));else finish();
    }));
   });
   observer.observe(document,{childList:true,subtree:true});
   setTimeout(()=>resolve({ready_ms:null,error:'Real page content did not render'}),15000);
  });
 },{selector});
  stage='navigation';response=await page.goto(new URL(route,base).href,{waitUntil:'domcontentloaded',timeout:20000});
  stage='evaluation';
  const d=await page.evaluate(()=>window.__pagePerformance);
  const external=(d.covers||[]).filter(i=>new URL(i.src).origin!==base.origin);
  result={name,route,status:response.status(),...d,errors,writes,failed_covers:failedCovers,
   pass:response.ok()&&d.ready_ms!==null&&!d.content_error&&!d.error&&!errors.length&&!writes.length
    &&!(d.blocked_writes||[]).length&&!failedCovers.length&&!external.length};
  if(!result.pass)result.requests=requests;
 }catch(e){
  result={name,route,status:response?.status()??null,ready_ms:null,pass:false,error:e.message,
   failure_phase:stage,errors,writes,failed_covers:failedCovers,requests};
 }finally{
  // Save the actual result before cleanup: a stuck close must not erase it.
  try{onMeasured(result);}finally{
   if(context){
    const started=Date.now();
    try{await context.close();}catch(e){
     result.pass=false;result.cleanup_error=e.message;result.failure_phase??='context.close';
     result.requests??=requests;
    }
    result.cleanup_ms=Date.now()-started;
   }
  }
 }
 return result;
}
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.PAGE_QA_BROWSER?{channel:process.env.PAGE_QA_BROWSER}:{})});
 const report={generated_at:new Date().toISOString(),base:base.origin,viewport:'1440x1000',browser_cache:'fresh context per sample',
  browser:process.env.PAGE_QA_BROWSER||'chromium-headless-shell',browser_version:browser.version(),
  external_images_blocked:process.env.PAGE_QA_BLOCK_EXTERNAL_IMAGES==='1',budgets:{median_ms:1000,p95_ms:1500},
  driver_complete:false,measurement_complete:false,phase:'measuring',active:null,rows:[],summary:[]};
 const save=()=>{fs.mkdirSync(path.dirname(out),{recursive:true});fs.writeFileSync(out,JSON.stringify(report,null,2));};
 try{
  for(const entry of cases){
   for(let i=0;i<samples;i++){
    report.active={name:entry[0],route:entry[1],sample:i,phase:'measuring'};save();
    await measure(browser,entry,row=>{
     report.rows.push(row);report.active.phase='closing_context';save();
    });
    report.active=null;save();
   }
   const rows=report.rows.filter(r=>r.name===entry[0]),ready=rows.map(r=>r.ready_ms??Infinity);
   const summary={name:entry[0],samples,content_median_ms:Math.round(percentile(rows.map(r=>r.content_ms??Infinity),.5)),
    median_ms:Math.round(percentile(ready,.5)),p95_ms:Math.round(percentile(ready,.95)),
    pass:rows.every(r=>r.pass)&&percentile(ready,.5)<=1000&&percentile(ready,.95)<=1500};
   report.summary.push(summary);save();console.log(JSON.stringify(summary));
  }
  report.measurement_complete=true;report.pass=report.summary.every(r=>r.pass);save();if(!report.pass)process.exitCode=1;
 }catch(e){report.pass=false;report.driver_error=e.message;throw e;}
 finally{
  report.phase='closing_browser';save();
  try{await browser.close();report.driver_complete=true;report.phase='complete';}
  catch(e){report.pass=false;report.driver_error=e.message;report.phase='browser_close_failed';throw e;}
  finally{save();}
 }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
