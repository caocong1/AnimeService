'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const matcher = require('../static/danmu-match.js');
const show = {title:'野生的大魔王出现了！第二季',mapping:{season:2,aliases:['A Wild Last Boss Appeared']}};
const title = '『野生的大魔王出现了 第二季』第2话(2026)【B站视频·需核对版本】from bilibili';
const ep = (label, id='bili') => ({source_key:id,source_identity:id,episodeNumber:'1',episodeTitle:'【bilibili1】 '+label+' · 27:43'});
test('source-written E2 is accepted even when the UGC API calls it P1', () => {
  const detail = {animeTitle:title,episodes:[ep('第2话')]};
  assert.equal(matcher.matchingEpisode(show,2,detail,true).source_key,'bili');
  assert.equal(matcher.matchingEpisode(show,1,detail,true),null);
});
test('wrong season, unproven sequel, commentary and other work are rejected', () => {
  for (const name of ['野生的大魔王出现了 第一季 第2话','野生的大魔王出现了 第2话','A Wild Last Boss Appeared 第2话','野生的大魔王出现了 第二季 第2话 解说','另一个大魔王 第二季 第2话']) {
    assert.equal(matcher.workMatches(show,name,true),false,name);
  }
});
test('P order, split parts, specials and duplicate labels never prove the episode', () => {
  for (const label of ['P2','1/2','第2话上半','第2话预告','第2话 OP','第2.5话','第1话 / 第2话','01-03话']) {
    assert.equal(matcher.episodeNumber(label),null,label);
  }
  assert.equal(matcher.matchingEpisode(show,2,{animeTitle:show.title,episodes:[ep('P2')]},true),null);
  assert.equal(matcher.matchingEpisode(show,2,{animeTitle:title,episodes:[ep('第2话'),ep('第2话','duplicate')]},true),null);
});
test('collection part labels can prove E2 without treating P2 as evidence', () => {
  const detail = {animeTitle:'【野生的大魔王出现了 第二季】01-03话',episodes:[ep('01','one'),ep('02','two'),ep('03','three'),ep('此合集每周更新','extra')]};
  assert.equal(matcher.matchingEpisode(show,2,detail,true).source_key,'two');
});
test('cour and catalogue season safeguards survive compatibility matching', () => {
  const cour = {title:'Test Anime Season 4',mapping:{season:4,aliases:['测试作品 第3部分']}};
  assert.equal(matcher.workMatches(cour,'Test Anime Season 4',true),false);
  const detail = {animeTitle:show.title,seasons:[{id:'s1',name:'Season 1'},{id:'s2',name:'Season 2'}],episodes:[{...ep('第2话'),seasonId:'s1'}]};
  assert.equal(matcher.matchingEpisode(show,2,detail,true),null);
});
test('automatic search fetches only unique E2 sources and prefers loaded comments', async () => {
  const calls=[];
  const candidates=[
    {animeId:1,animeTitle:title,bangumiId:'bvBV1'},
    {animeId:2,animeTitle:title.replace('第2话','第3话'),bangumiId:'bvBV2'},
    {animeId:3,animeTitle:title,bangumiId:'bvBV3'},
    {animeId:4,animeTitle:title+' 解说',bangumiId:'bvBV4'},
  ];
  const result=await matcher.match(show,2,async (path,body)=>{
    calls.push([path,body]);
    if(path.startsWith('/danmu/search'))return {animes:candidates};
    if(path.startsWith('/danmu/show/')) {
      const n=Number(path.split('/').pop());
      return {bangumi:{...candidates[n-1],episodes:[ep(n===2?'第3话':'第2话','source'+n)]}};
    }
    const id=body.episodes[0];
    return {sources:[{id,count:id==='source1'?612:0,comments:[]}]};
  });
  assert.equal(result.status,'matched');
  assert.deepEqual(result.selected.map(s=>s.id),['source1','source3']);
  assert.equal(calls.some(([p])=>p==='/danmu/show/4'),false);
  assert.equal(calls.some(([,b])=>b?.episodes.includes('source2')),false);
});
test('transient comment failures remain retryable instead of becoming no-match', async () => {
  const result=await matcher.match(show,2,async path=>{
    if(path.startsWith('/danmu/search'))return {animes:[{animeId:1,animeTitle:title,bangumiId:'bvBV1'}]};
    if(path.startsWith('/danmu/show'))return {bangumi:{animeTitle:title,episodes:[ep('第2话')]}};
    throw Error('temporary');
  });
  assert.equal(result.status,'error');
});
