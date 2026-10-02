import test from 'node:test';
import assert from 'node:assert/strict';
import { ugcLinks, ugcQuery, searchUgc } from './bilibili-ugc.js';
import { existsSync } from 'node:fs';

test('video title keeps original query, bounds fallbacks and removes generic noise', () => {
  const q=ugcQuery('「冲晕过去了」第12话 官中（周更）无删减');
  assert.equal(q.terms[0], '「冲晕过去了」第12话 官中（周更）无删减');
  assert.deepEqual(q.terms.slice(1), ['冲晕过去了 第12话', '冲晕过去了']);
  assert.equal(q.core, '冲晕过去了');
  assert.equal(ugcQuery('魔兽之王第2季 12话').core, '魔兽之王');
  assert.equal(ugcQuery('重骑士 13').core, '重骑士');
  assert.equal(ugcQuery('86').core, '86');
});

test('title search recalls decorated upload titles and rejects unrelated recommendations', async () => {
  const calls=[], source={async _searchByType(term,type,key) {
    calls.push([term,type,key]);
    if(term==='冲晕过去了') return [{mediaId:'bv-target',title:'「冲晕过去了」第12话 官中（周更）无删减'}];
    return [{mediaId:'bv-unrelated',title:'全12话 超清中字（未删减版）周更'}];
  }};
  const rows=await searchUgc(source,'「冲晕过去了」第12话 官中（周更）无删减','test-key');
  assert.deepEqual(rows.map(x=>x.mediaId),['bv-target']);
  assert.equal(calls.length,3);
  assert.ok(calls.every(x=>x[1]==='video'&&x[2]==='test-key'));
});

test('search deduplicates by video ID without inventing title-to-work associations', async () => {
  const source={async _searchByType() {return [{mediaId:'a',title:'【魔兽之王】第2季 第12话'},
    {mediaId:'b',title:'【不死者之王】第12话'}, {mediaId:'c',title:'魔兽之王 第12话预告'}]}};
  assert.deepEqual((await searchUgc(source,'魔兽之王第2季 12话','key')).map(x=>x.mediaId),['a','c']);
});

test('explicit episode and full upload title rank ahead of clips and broad work results', async () => {
  const source={async _searchByType() {return [
    {mediaId:'other',title:'魔兽之王 第13话'},
    {mediaId:'preview',title:'魔兽之王 第12话预告'},
    {mediaId:'target',title:'魔兽之王 第12话'},
  ]}};
  assert.deepEqual((await searchUgc(source,'魔兽之王 12','key')).map(x=>x.mediaId),['target','preview','other']);
});

test('installed season prefilter keeps manual UGC candidates with no season in title', {
  skip: !existsSync(new URL('../vendor/danmuapi/danmu_api/sources/bilibili.js', import.meta.url)),
}, async () => {
  const {default: Bilibili} = await import('../vendor/danmuapi/danmu_api/sources/bilibili.js');
  const b=new Bilibili();
  b.getEpisodes=async()=>[{id:1,title:'12',duration:2020,link:'https://www.bilibili.com/video/BV1fHh86tEug?p=1'}];
  const rows=[], details=new Map();
  await b.handleAnimes([
    {mediaId:'bvBV1fHh86tEug',title:'「冲晕过去了」第12话',type:UGC_TYPE_TEST},
    {mediaId:'bvBV1EVap6yE6B',title:'魔兽之王 第2季 第12话',type:UGC_TYPE_TEST},
  ],'冲晕过去了',rows,details,2);
  assert.equal(rows.length,2);
  assert.ok(rows.some(x=>x.animeTitle.includes('冲晕过去了')));
});
const UGC_TYPE_TEST='B站视频·需核对版本';

test('UGC preserves stable page URL, duration and does not infer episode from page', () => {
  const links = ugcLinks([{id: 123, title: '第13话', duration: 2639,
    link: 'https://www.bilibili.com/video/BV1EVap6yE6B?p=2'}]);
  assert.equal(links[0].url, 'https://www.bilibili.com/video/BV1EVap6yE6B?p=2');
  assert.equal(links[0].name, '');
  assert.match(links[0].title, /第13话 · 43:59$/);
});

test('installed adapter registers UGC details with page and duration', {
  skip: !existsSync(new URL('../vendor/danmuapi/danmu_api/sources/bilibili.js', import.meta.url)),
}, async () => {
  const {default: Bilibili} = await import('../vendor/danmuapi/danmu_api/sources/bilibili.js');
  const b = new Bilibili();
  b.getEpisodes = async () => [{id: 123, title: '第13话', duration: 1420,
    link: 'https://www.bilibili.com/video/BV1EVap6yE6B?p=2'}];
  const rows = [], details = new Map();
  await b.handleAnimes([{provider: 'bilibili', mediaId: 'bvBV1EVap6yE6B',
    title: '【重骑士】第13话', type: 'B站视频·需核对版本', year: 2026,
    imageUrl: '', episodeCount: 1}], '遭到流放的转生重骑士 13', rows, details);
  assert.equal(rows.length, 1);
  assert.equal(details.size, 1);
  const ep = [...details.values()][0].links[0];
  assert.equal(ep.url, 'https://www.bilibili.com/video/BV1EVap6yE6B?p=2');
  assert.match(ep.title, /23:40$/);
});
