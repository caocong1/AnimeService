'use strict';
// One network boundary for every page/frame/worker, including pagehide requests.
const assert=require('node:assert/strict'),crypto=require('node:crypto'),http=require('node:http');
const readMethods=new Set(['GET','HEAD']);
function checkedBase(value,mode) {
  const url=new URL(value);
  assert.ok(url.protocol==='http:'&&['127.0.0.1','localhost'].includes(url.hostname)&&!url.username&&!url.password);
  assert.ok(url.pathname==='/'&&!url.search&&!url.hash,'QA base must be a loopback origin');
  if(mode==='fixture')assert.ok(url.port&&url.port!=='4871','fixture must not use the production port');
  else assert.equal(mode,'readonly');
  return url.origin;
}
function safeRead(path,fixture=false) {
  try{path=decodeURIComponent(path);}catch{return false;}
  if(!path.startsWith('/')||path.includes('\\')||path.includes('\0')||path.split('/').some(p=>p==='.'||p==='..'))return false;
  if(!path.startsWith('/api/'))return path==='/'||path==='/watch'||/^\/show\/\d+$/.test(path)||path.startsWith('/static/');
  if(/^\/api\/(?:bootstrap|home|health|status|finished-viewing)(?:\/[^/]+)?$/.test(path))return true;
  if(/^\/api\/shows(?:\/\d+)?$/.test(path))return true;
  if(path==='/api/web/alignment-capabilities')return true;
  if(/^\/api\/web\/media\/[^/]+$/.test(path))return true;
  if(fixture&&(path==='/api/testing/fixture'||path==='/api/web/danmu/search'||/^\/api\/web\/media(?:\/|$)/.test(path)))return true;
  return false;
}
function fixtureWrite(path) {
  return /^\/api\/finished-viewing\/[^/]+\/(?:progress|watch|viewing-session)$/.test(path)
    ||/^\/api\/web\/media\/[^/]+\/(?:progress|viewing-session)$/.test(path)
    ||/^\/api\/shows\/\d+\/watch$/.test(path)
    ||path==='/api/web/danmu/comments';
}
async function guardedContext(browser,{base,mode,token='',mock,viewport={width:1440,height:1000}}) {
  base=checkedBase(base,mode);
  if(mode==='fixture')assert.ok(token.length>=24,'fixture token is required before opening a browser page');
  let verified=mode==='readonly';
  const ledger={allowed_reads:0,allowed_fixture_writes:0,blocked:[],websockets_blocked:0,transport_blocked:[],transport_reads:0,transport_fixture_writes:0};
  function rejection(url,method) {
    if(url.origin!==base)return 'different_origin';
    if(!readMethods.has(method)&&!(method==='POST'&&mode==='fixture'&&verified&&fixtureWrite(url.pathname)))return 'write_blocked';
    if(readMethods.has(method)&&!safeRead(url.pathname,mode==='fixture'))return 'read_not_allowlisted';
    return '';
  }
  // Routes stop with the context. Keepalive/Beacon can outlive them; this proxy
  // remains active until the browser exits and checks the same network policy.
  const firewall=http.createServer((request,response)=>{
    let url;try{url=new URL(request.url);}catch{response.writeHead(403);response.end();return;}
    const method=request.method.toUpperCase();
    const reason=rejection(url,method);
    if(reason){ledger.transport_blocked.push({method,reason,path:url.pathname});request.resume();response.writeHead(403);response.end();return;}
    if(readMethods.has(method))ledger.transport_reads++;else ledger.transport_fixture_writes++;
    const headers={...request.headers,host:url.host};delete headers['proxy-connection'];
    // A late keepalive can outlive Playwright's extra-header interception.
    // Only this already nonce-verified fixture may receive the nonce here.
    if(!readMethods.has(method))headers['x-player-qa-token']=token;
    const upstream=http.request(url,{method,headers,agent:false},incoming=>{
      response.writeHead(incoming.statusCode,incoming.headers);incoming.pipe(response);
    });
    upstream.on('error',()=>{if(!response.headersSent)response.writeHead(502);response.end();});
    request.on('aborted',()=>upstream.destroy());request.pipe(upstream);
  });
  firewall.on('connect',(_request,socket)=>socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'));
  firewall.on('upgrade',(_request,socket)=>{ledger.websockets_blocked++;socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');});
  await new Promise(resolve=>firewall.listen(0,'127.0.0.1',resolve));
  const context=await browser.newContext({viewport,serviceWorkers:'block',
    proxy:{server:'http://127.0.0.1:'+firewall.address().port,bypass:'<-loopback>'},
    extraHTTPHeaders:mode==='fixture'?{'X-Player-QA-Token':token}:{}});
  await context.routeWebSocket('**/*',ws=>{ledger.websockets_blocked++;ws.close();});
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),method=request.method().toUpperCase(),path=url.pathname;
    const reason=rejection(url,method);
    if(reason){ledger.blocked.push({method,reason,path});return route.abort('blockedbyclient');}
    if(readMethods.has(method))ledger.allowed_reads++;else ledger.allowed_fixture_writes++;
    if(mock&&await mock(route))return;
    return route.continue();
  });
  // APIRequestContext does not share browser routing: only expose this checked GET.
  async function get(path) {
    const url=new URL(path,base);
    assert.equal(url.origin,base);assert.ok(safeRead(url.pathname,mode==='fixture'));
    return new Promise((resolve,reject)=>{
      const request=http.get(url,{timeout:5000},response=>{
        const chunks=[];let size=0;
        response.on('data',chunk=>{size+=chunk.length;if(size>2*1024*1024)request.destroy(new Error('QA response too large'));else chunks.push(chunk);});
        response.on('end',()=>{try{assert.equal(response.statusCode,200,'QA GET must not redirect or fail');resolve(JSON.parse(Buffer.concat(chunks)));}catch(error){reject(error);}});
      });
      request.on('error',reject);request.on('timeout',()=>request.destroy(new Error('QA GET timeout')));
    });
  }
  async function fixture() {
    assert.equal(mode,'fixture');
    const data=await get('/api/testing/fixture');
    assert.equal(data.isolated,true);assert.equal(data.worker_enabled,false);
    assert.equal(data.token_sha256,crypto.createHash('sha256').update(token).digest('hex'));
    verified=true;return data;
  }
  async function closeNetwork(){firewall.closeAllConnections();await new Promise(resolve=>firewall.close(resolve));}
  return {context,ledger,get,fixture,closeNetwork};
}
module.exports={checkedBase,safeRead,fixtureWrite,guardedContext};
