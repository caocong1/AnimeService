const test = require('node:test');
const assert = require('node:assert/strict');
const m = require('../static/subtitle-queue-model.js');
const now = 1800000000;
const base = {show_id: 42, episode: 3, season: 1, title: '示例', status: 'queued', edition: 'v0', stage: null, progress: null,
  since: now - 600, last_activity: now - 600, stalled: false, stalled_seconds: 0, position: null, ahead: null, reasons: [],
  gates: null, gap: null, download_progress: null, release_id: null, refinement: null};

test('every status has a readable label and next step', () => {
  assert.equal(m.describe({...base, position: 2, ahead: 2}, now).label, '排队第 2 位');
  assert.match(m.describe({...base, position: 1, ahead: 0}, now).detail, /制作槽空闲/);
  const producing = m.describe({...base, status: 'producing', stage: 'layout', progress: {done: 3, total: 9}}, now);
  assert.equal(producing.tone, 'signal');
  assert.equal(producing.detail, '排版与画面检查 3/9 · 10 分钟前回报');
  assert.match(m.describe({...base, status: 'producing'}, now).detail, /负责人已开始 · 开始于 10 分钟前/);
  const stalled = m.describe({...base, status: 'producing', stalled: true, stalled_seconds: 5 * 3600}, now);
  assert.equal(stalled.tone, 'warn');
  assert.match(stalled.label, /可能卡住/);
  assert.match(stalled.detail, /^5\.0 小时没有进度回报；若桌面制作会话已停止，需要你授权恢复$/);
  assert.match(m.describe({...base, status: 'starting', stalled: true, stalled_seconds: 2400}, now).detail, /40 分钟没有确认/);
  assert.match(m.describe({...base, status: 'waiting_video', gap: {code: 'download_retry', reason: '', retry_at: now + 600}}, now).detail, /等待重试 · \d\d:\d\d 重试/);
  assert.equal(m.describe({...base, status: 'waiting_video'}, now).detail, '后台尚未发现本集资源');
  assert.equal(m.describe({...base, status: 'downloading', download_progress: 40}, now).label, '视频下载中 40%');
  assert.match(m.describe({...base, status: 'preparing', reasons: ['needs_reliable_chinese_dialogue_base']}, now).detail, /中文字幕原稿/);
  assert.match(m.describe({...base, status: 'blocked', reasons: ['encoder unavailable']}, now).detail, /encoder unavailable/);
  const v0 = m.describe({...base, status: 'v0_ready', refinement: {...base, status: 'producing', edition: 'v1', stage: 'lyrics'}}, now);
  assert.equal(v0.label, 'v0 可看');
  assert.equal(v0.tone, 'ok');
  assert.match(v0.detail, /^v1 制作中 · v1 · OP \/ ED 歌词 · 10 分钟前回报$/);
  assert.equal(m.describe({...base, status: 'v0_ready', refinement: {...base, status: 'queued'}}, now).detail, 'v1 精校排队中');
  assert.equal(m.describe({...base, status: 'v0_ready', refinement: {...base, status: 'queued', position: 2, ahead: 2}}, now).detail, 'v1 精校排队第 2 位，前面还有 2 集');
  assert.equal(m.describe({...base, status: 'producing', owner_state: 'working', owner_last_activity: now - 120}, now).detail, '负责会话工作中 · 最近活动 2 分钟前');
  assert.match(m.describe({...base, status: 'producing', stalled: true, stalled_seconds: 1200, owner_state: 'idle'}, now).detail, /^负责会话已空闲 20 分钟；/);
  assert.match(m.describe({...base, status: 'producing', stalled: true, stalled_seconds: 1500, owner_state: 'stuck'}, now).detail, /守卫已请求中断/);
  assert.equal(m.describe({...base, status: 'v0_ready', refinement: {...base, status: 'producing', stalled: true, stalled_seconds: 7200}}, now).tone, 'warn');
  assert.equal(m.describe({...base, status: 'done'}, now).tone, 'ok');
});

test('sections put attention first and watchable last', () => {
  const items = [{...base, position: 1, ahead: 1}, {...base, episode: 2, status: 'producing', stalled: true, stalled_seconds: 7200},
    {...base, episode: 1, status: 'v0_ready'}, {...base, show_id: 43, episode: 1, status: 'waiting_video'},
    {...base, show_id: 43, episode: 2, status: 'v0_ready', refinement: {...base, status: 'producing', edition: 'v1'}}];
  const sections = m.sections({items}, now);
  assert.deepEqual(sections.map(s => [s.key, s.items.length]), [['attention', 1], ['active', 1], ['queued', 1], ['waiting', 1], ['ready', 1]]);
  assert.equal(sections[1].items[0].episode, 2);
  assert.deepEqual(m.sections({items: []}, now), []);
});

test('show page line and overview health text', () => {
  assert.equal(m.line({...base, position: 3, ahead: 3}, now), '字幕 · 排队第 3 位 · 前面还有 3 集；一次只做一集');
  assert.equal(m.waited({...base, waiting_seconds: 3 * 86400}), '视频到位 3.0 天');
  assert.equal(m.waited({...base, status: 'done', waiting_seconds: 3 * 86400}), '');
  assert.equal(m.waited(base), '');
  const busy = m.health({slot: {occupied: true, item: {...base, status: 'producing', stalled: true}},
    scheduler: {ok: true, checked_at: now - 120, last_activity: now - 300, stale: false}}, now);
  assert.equal(busy, '制作槽：示例 第 3 集（v0，可能卡住） · 调度检查 2 分钟前 · 调度会话最近活动 5 分钟前');
  assert.equal(m.health({slot: {occupied: false, item: null}, scheduler: null}, now), '制作槽空闲');
  assert.match(m.health({slot: {occupied: false, item: null}, scheduler: {ok: false}}, now), /守卫读取异常/);
});
