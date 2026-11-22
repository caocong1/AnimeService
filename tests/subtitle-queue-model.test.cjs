const test = require('node:test');
const assert = require('node:assert/strict');
const m = require('../static/subtitle-queue-model.js');

test('every playable episode has one viewing group, regardless of refinement faults', () => {
  for (const status of ['needs_user','reconciling','overdue','retry_wait','repair_blocked','actionable']) {
    const failure = {status:'blocked', recovery:{status, user_action:'等待原负责人恢复回执（r22）', explanation:'受控恢复'}};
    for (const [status,label] of [['done','精校版可看'],['v0_ready','观看版可看']]) {
      const item = {status,refinement:failure,waiting_seconds:390000};
      assert.deepEqual(m.sections({items:[item]}),[{key:'ready',title:'可以观看',items:[item]}]);
      assert.equal(m.line(item), label);
    }
    assert.doesNotMatch(m.line(failure),/派发|负责人|受控|留意|r22/);
  }
});

test('waiting, unreleased, stopped and live production stay distinct', () => {
  for (const [item,key,label] of [
    [{status:'blocked',stop_class:'user_stopped'},'stopped','已按要求停止'],
    [{status:'producing',stalled:true},'preparing','字幕暂未完成'],
    [{status:'producing'},'active','正在制作字幕'],
    [{status:'unreleased',release_date:'2026-12-01'},'unreleased','等待播出 · 2026-12-01'],
    [{status:'protected'},'protected','暂未安排字幕'],
    [{status:'blocked',recovery:{status:'source_wait'}},'preparing','等待字幕素材'],
    [{status:'waiting_video'},'waiting','等待片源'],
    [{status:'queued',position:2},'queued','字幕排队中 · 排队第 2 位'],
    [{status:'downloading'},'preparing','正在下载视频'],
  ]) {
    assert.equal(m.section(item), key);
    assert.equal(m.line(item), label);
  }
});
