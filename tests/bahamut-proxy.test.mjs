import test from 'node:test';
import assert from 'node:assert/strict';
import { bahamutProxyAgent } from '../vendor/danmuapi/danmu_api/utils/animeservice-bahamut-proxy.js';

test('proxy is limited to the exact Bahamut API hostname', () => {
  const local='http://127.0.0.1:7897';
  assert.ok(bahamutProxyAgent('https://api.gamer.com.tw/anime/v1/video.php',local));
  for(const host of ['api.bilibili.com','127.0.0.1','api.gamer.com.tw.attacker.test'])
    assert.equal(bahamutProxyAgent('https://'+host+'/',local),null);
  assert.equal(bahamutProxyAgent('https://api.gamer.com.tw/',''),null);
  for(const proxy of ['http://example.com:7897','http://user:pass@127.0.0.1:7897','socks5://127.0.0.1:7897'])
    assert.throws(()=>bahamutProxyAgent('https://api.gamer.com.tw/',proxy));
});
