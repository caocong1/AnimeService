const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const src='https://lain.bgm.tv/r/800/pic/cover/l/17/c7/554779_oPitO.jpg';
function fixture(){
 const context=vm.createContext({URL,window:{fetch(){}},document:{addEventListener(){}},addEventListener(){},matchMedia:()=>({matches:false})});
 vm.runInContext(fs.readFileSync('static/core.js','utf8'),context);
 return context;
}
test('supported covers use persistent local thumbnails only after the host advertises them',()=>{
 const context=fixture();context.src=src;
 assert.match(vm.runInContext("cover(src,'番')",context),/src="https:\/\/lain.bgm.tv/);
 vm.runInContext('App.coverCache=true',context);
 const html=vm.runInContext("cover(src,'番','','',{width:160,eager:true})",context);
 assert.match(html,/src="\/covers\/v1\/160\/r\/800\/pic\/cover/);
 assert.match(html,/loading="eager"/);assert.ok(!html.includes('https://'));
 const hero=vm.runInContext("cover(src,'番','','',{priority:true})",context);
 assert.match(hero,/\/480\//);assert.match(hero,/loading="eager"/);assert.match(hero,/fetchpriority="high"/);
 assert.match(vm.runInContext("cover(src,'番')",context),/loading="lazy"/);
});
test('invalid and unsupported cover addresses never become arbitrary local proxy URLs',()=>{
 const context=fixture();vm.runInContext('App.coverCache=true',context);
 for(const value of [src+'?secret=1',src.replace('lain.bgm.tv','evil.test'),src.replace('https:','http:'),'javascript:alert(1)','/private']){
  context.src=value;const html=vm.runInContext("cover(src,'番')",context);
  assert.ok(!html.includes('/covers/v1/'));
 }
});
test('every page using the shared runtime has the same versioned cover-capable script',()=>{
 const version=fs.readFileSync('static/index.html','utf8').match(/\/static\/core\.js\?v=([^"\s]+)/)?.[1];
 assert.ok(version,'the shared runtime must have a cache-busting version');
 for(const name of fs.readdirSync('static').filter(n=>n.endsWith('.html'))){
  const html=fs.readFileSync('static/'+name,'utf8');
  if(html.includes('/static/core.js'))assert.equal(html.match(/\/static\/core\.js\?v=([^"\s]+)/)?.[1],version,name);
 }
});
