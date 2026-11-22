import test from 'node:test';
import assert from 'node:assert/strict';
import {parseDanmakuBase64 as parse, mergeBilibiliComments} from './bilibili-danmaku.js';
import {existsSync} from 'node:fs';
const b64 = bytes => Buffer.from(bytes).toString('base64');
const v = value => {let n=BigInt(value), b=[];do {b.push(Number(n&127n)|(n>127n?128:0));n>>=7n;}while(n);return b;};
const field = (n,bytes) => [...v(n*8+2),...v(bytes.length),...bytes];
test('HTTP 200 JSON, HTML, truncation and malformed varints fail instead of returning zero',()=>{
  for(const data of [Buffer.from('{"code":-352}'),Buffer.from('<!DOCTYPE html>'),[10,20,8],[128],[0],[10,1,128],[10,2,58,5]]) {
    assert.throws(()=>parse(b64(data)));
  }
  assert.throws(()=>parse(b64([16,1]))); // closed
  assert.deepEqual(parse(''),[]);
  assert.deepEqual(parse(b64([16,0])),[]);
});
test('unknown multibyte tags and fixed fields do not lose later comments',()=>{
  const content=Buffer.from('数码宝贝3……');
  const elem=[16,...v(1000),24,1,40,...v(16777215),...field(7,content),...v(25*8),1,...field(12,Buffer.from('2244545085323415552'))];
  const rows=parse(b64([...v(30*8+5),0,0,0,0,...field(1,elem),...field(1,elem)]));
  assert.equal(rows.length,2);
  assert.equal(rows[0].content,'数码宝贝3……');
  assert.equal(rows[0].progress,1000);
  assert.equal(rows[0].idStr,'2244545085323415552');
});
test('installed adapter propagates segment failures instead of claiming empty or partial success',async t=>{
  if (!existsSync('vendor/danmuapi/danmu_api/sources/bilibili.js')) return t.skip('adapter not installed');
  const {default:Source}=await import('../vendor/danmuapi/danmu_api/sources/bilibili.js');
  const source=new Source();
  source.getEpisodeDanmuSegments=async()=>({segmentList:[{n:0},{n:1}]});
  source.getEpisodeSegmentDanmu=async s=>{if(s.n) throw Error('restricted');return [{content:'真实弹幕'}];};
  await assert.rejects(source.getEpisodeDanmu('fixture'),/restricted/);
  source.getEpisodeSegmentDanmu=async()=>[];
  assert.deepEqual(await source.getEpisodeDanmu('fixture'),[]);
});

test('UGC always reads both endpoints and preserves available comments on one endpoint failure', async t => {
  if (!existsSync('vendor/danmuapi/danmu_api/sources/bilibili.js')) return t.skip('adapter not installed');
  const {default: Source} = await import('../vendor/danmuapi/danmu_api/sources/bilibili.js');
  const {Globals} = await import('../vendor/danmuapi/danmu_api/configs/globals.js');
  Globals.init({ GROUP_MINUTE: '1', DANMU_LIMIT: '0' });
  const source = new Source();
  source.getEpisodeDanmuSegments = async () => ({segmentList: [1, 2].map(index => ({
    url: `https://api.bilibili.com/x/v2/dm/web/seg.so?oid=43603788135&segment_index=${index}`,
  }))});
  const id = '2244545085323415552';
  const binary = Uint8Array.from(field(1, [
    8, ...v(id), 16, ...v(1230), 24, 5, 120, 9, ...field(7, Buffer.from('0 & 1')),
  ]));
  let response = 'not an XML list', secondSegmentFails = true;
  let xmlFetches = 0;
  t.mock.method(globalThis, 'fetch', async url => {
    if (url.includes('segment_index=1')) return new Response(binary);
    if (url.includes('segment_index=2')) return new Response(secondSegmentFails ? '{"code":-352}' : '');
    assert.equal(url, 'https://comment.bilibili.com/43603788135.xml');
    xmlFetches++;
    return new Response(response);
  });
  const url = 'https://www.bilibili.com/video/BV1gYsV6sEd1?p=2';
  await assert.rejects(source.getEpisodeDanmu(url), /XML/);
  response = '<i><chatid>43603788098</chatid></i>';
  await assert.rejects(source.getEpisodeDanmu(url), /CID mismatch/);
  response = '<i><chatid>43603788135</chatid><d p="1.23,5,25,16777215,1,0,abcdef,2244545085323415552,1">0 &amp; 1</d><d p="15,5,25,16777215,1,0,abcdef,2244545085323415553,1">0 &amp; 1</d></i>';
  const assertMerged = async () => {
    const rows = await source.getEpisodeDanmu(url);
    assert.deepEqual(rows.map(r => [r.idStr, r.progress, r.content]), [[id, 1230, '0 & 1'], ['2244545085323415553', 15000, '0 & 1']]);
    assert.equal(rows[0].like_num, 9);
    assert.equal(rows[1].midHash, 'abcdef');
    const {convertToDanmakuJson} = await import('../vendor/danmuapi/danmu_api/utils/danmu-util.js');
    assert.equal(convertToDanmakuJson(rows, 'bilibili1').length, 2);
  };
  await assertMerged();
  secondSegmentFails = false;
  await assertMerged();
  assert.equal(xmlFetches, 4);
  response = 'not an XML list';
  assert.equal((await source.getEpisodeDanmu(url)).length, 1);
});

test('distinct 64-bit IDs with the same text and time survive the endpoint merge', () => {
  const first = {idStr: '2244545085323415552', progress: 1000, content: 'same'};
  const second = {...first, idStr: '2244545085323415553'};
  assert.deepEqual(mergeBilibiliComments([first], [first, second]).map(row => row.idStr), [first.idStr, second.idStr]);
});
