import test from 'node:test';
import assert from 'node:assert/strict';
import { ugcLinks } from './bilibili-ugc.js';
import { existsSync } from 'node:fs';

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
