const test = require('node:test');
const assert = require('node:assert/strict');
const {mix, settings} = require('../static/danmu-timing.js');
const comment = (time, text='same') => ({time, text, mode:0, color:'#ffffff'});
test('independent shifts align before cross-source deduplication', () => {
  const sources=[{offset:20,comments:[comment(10)]},{offset:-3,comments:[comment(33)]}];
  assert.deepEqual(mix(sources).map(c=>c.time),[30]);
  assert.equal(sources[0].comments[0].time,10);
  sources[1].offset=0;
  assert.deepEqual(mix(sources).map(c=>c.time),[30,33]);
});
test('negative times are discarded, not piled at zero; beyond local end discarded', () => {
  assert.deepEqual(mix([{offset:-20,comments:[comment(5),comment(20),comment(50)]}],0,25).map(c=>c.time),[0]);
});
test('a previously saved tail cutoff no longer drops comments', () => {
  assert.deepEqual(mix([{offset:20,end:1420,comments:[comment(100),comment(1420)]}],2,2000).map(c=>c.time),[122,1442]);
});
test('invalid persisted settings cannot poison the clock', () => {
  assert.deepEqual(settings({offset:'bad',end:-1}),{offset:0});
  assert.deepEqual(settings({offset:-20.5,end:''}),{offset:-20.5});
  assert.deepEqual(mix([{offset:0,comments:[comment(10)]}],NaN,NaN).map(c=>c.time),[10]);
});

test('saved source list keeps settings and empty selections, strips transient data', () => {
  const {savedSources, playerSettings} = require('../static/danmu-timing.js');
  const source={id:'key',source_identity:'stable',title:'Bilibili',offset:-2,end:1420,comments:[comment(10)],loading:true};
  assert.deepEqual(savedSources([source,source,null]),[{id:'key',source_identity:'stable',title:'Bilibili',offset:-2}]);
  assert.deepEqual(savedSources([]),[]);
  assert.equal(savedSources(null),null);
  assert.deepEqual(playerSettings({visible:false,opacity:0.5,fontSize:24,speed:6,danmuku:['large']}),{opacity:0.5,fontSize:24,speed:6,visible:false});
  assert.deepEqual(playerSettings({fontSize:NaN,speed:99,margin:['bad',-1]}),{});
});
