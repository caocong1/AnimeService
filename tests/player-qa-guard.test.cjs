'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http');
const {checkedBase,safeRead,guardedContext}=require('../scripts/player_qa_guard.cjs');
test('fixture origin rejects production, credentials and external hosts',()=>{
  for(const base of ['http://127.0.0.1:4871','http://localhost:4871','https://example.com','http://x@127.0.0.1:4879','http://127.0.0.1:4879/path'])
    assert.throws(()=>checkedBase(base,'fixture'));
  assert.equal(safeRead('/api/check'),false);
  assert.equal(safeRead('/api/web/danmu/search'),false);
  assert.equal(safeRead('/api/finished-viewing/fixture-v0'),true);
  for(const path of ['/unknown-mutation','/%61pi/check','/api%2fcheck','/static/../api/check','/static/%2e%2e/api/check','/%ZZ'])assert.equal(safeRead(path),false);
});
test('network boundary blocks writes through fetch, Request, XHR, Beacon, form and pagehide',
  {skip:process.env.PLAYER_QA_BROWSER_TESTS!=='1'},async()=>{
  const {chromium}=require('playwright');
  const received=[],upgrades=[],outside=[];
  const other=http.createServer((req,res)=>{outside.push(req.method);req.resume();res.end('outside');});
  await new Promise(resolve=>other.listen(0,'127.0.0.1',resolve));
  const otherBase='http://127.0.0.1:'+other.address().port;
  const server=http.createServer((req,res)=>{
    received.push({method:req.method,path:req.url});req.resume();
    res.setHeader('Content-Type','text/html');res.end('<!doctype html><body>QA synthetic server</body>');
  });
  server.on('upgrade',(req,socket)=>{upgrades.push(req.url);socket.destroy();});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({channel:process.env.PLAYER_QA_BROWSER||'msedge',headless:true});
  const guard=await guardedContext(browser,{base,mode:'readonly'});let passed=false;
  try {
    const page=await guard.context.newPage();await page.goto(base);
    await guard.context.request.post(base+'/api/request-context',{data:'x'}).catch(()=>{});
    await page.evaluate(async otherBase=>{
      const attempts=[fetch('/api/progress',{method:'POST',body:'x'}),fetch('/%61pi/check'),fetch('/api%2fcheck'),
        fetch(new Request('/api/progress',{method:'PUT',body:'x'})),
        fetch('/api/progress',{method:'POST',body:'x',keepalive:true}),fetch('/api/check'),
        fetch(otherBase+'/api/finished-viewing/x/progress',{method:'POST',body:'x'})];
      navigator.sendBeacon('/api/beacon','x');
      attempts.push(new Promise(resolve=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/xhr');xhr.onloadend=resolve;xhr.send('x');}));
      const frame=document.createElement('iframe');frame.name='sink';document.body.append(frame);
      const form=document.createElement('form');form.method='POST';form.action='/api/form';form.target='sink';document.body.append(form);form.submit();
      addEventListener('pagehide',()=>{navigator.sendBeacon('/api/exit-beacon','x');fetch('/api/exit-fetch',{method:'POST',body:'x',keepalive:true}).catch(()=>{});});
      new WebSocket(location.origin.replace('http:','ws:')+'/ws');
      await Promise.allSettled(attempts);
    },otherBase);
    await page.goto('about:blank');await guard.context.close();
    assert.equal(received.filter(r=>!['GET','HEAD'].includes(r.method)).length,0);
    assert.equal(received.filter(r=>r.path.startsWith('/api/')).length,0);
    assert.ok(guard.ledger.blocked.length>=7);
    assert.equal(upgrades.length,0);assert.ok(guard.ledger.websockets_blocked>=1);
    assert.equal(outside.length,0);assert.ok(guard.ledger.transport_reads>=1);
    passed=true;
  } finally {
    await browser.close();await guard.closeNetwork();
    await new Promise(resolve=>server.close(resolve));await new Promise(resolve=>other.close(resolve));
    if(passed&&process.env.PLAYER_QA_GUARD_PROOF){
      assert.equal(received.filter(r=>!['GET','HEAD'].includes(r.method)).length,0);
      require('node:fs').writeFileSync(process.env.PLAYER_QA_GUARD_PROOF,JSON.stringify({passed:true,
        synthetic_canary:true,production_contacted:false,application_writes:0,application_api_requests:0,
        external_requests:outside.length,websocket_upgrades:upgrades.length,network:guard.ledger},null,2));
    }
  }
});
