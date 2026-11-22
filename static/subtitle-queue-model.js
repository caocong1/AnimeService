'use strict';
(function (root) {
  const groups = {ready: '可以观看', active: '正在制作字幕', queued: '字幕排队中',
    preparing: '准备字幕', waiting: '等待片源', unreleased: '等待播出', stopped: '已暂停', protected: '暂未安排字幕', checking_release: '正在读取播放版本', release_unavailable: '字幕版暂不可用'};
  function section(item) {
    if (['v0_ready', 'done'].includes(item.status)) return 'ready';
    if (['user_stopped', 'protected'].includes(item.stop_class)) return 'stopped';
    if (['unreleased', 'protected', 'checking_release', 'release_unavailable'].includes(item.status)) return item.status;
    if (item.status === 'waiting_video' || item.recovery?.status === 'waiting_media') return 'waiting';
    if (item.status === 'queued') return 'queued';
    if (item.status === 'producing' && !item.stalled && !['needs_user','reconciling','overdue','repair_blocked','retry_wait'].includes(item.recovery?.status)) return 'active';
    return 'preparing';
  }
  function describe(item) {
    const group = section(item);
    let label = groups[group], detail = '';
    if (group === 'ready') label = item.status === 'done' ? '精校版可看' : '观看版可看';
    else if (group === 'stopped' && item.stop_class === 'user_stopped') label = '已按要求停止';
    else if (group === 'queued' && item.position > 0) detail = '排队第 ' + item.position + ' 位';
    else if (group === 'unreleased' && item.release_date) detail = item.release_date;
    else if (group === 'preparing') {
      if (item.status === 'downloading') label = '正在下载视频';
      else if (item.recovery?.status === 'source_wait') label = '等待字幕素材';
      else if (item.stalled || item.status === 'blocked' || ['needs_user','reconciling','retry_wait','overdue','repair_blocked'].includes(item.recovery?.status)) label = '字幕暂未完成';
    }
    return {label, detail, tone: group === 'ready' ? 'ok' : group === 'active' ? 'signal' : 'muted'};
  }
  function sections(queue) {
    const buckets = new Map();
    for (const item of queue.items || []) {
      const key = section(item);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(item);
    }
    return Object.entries(groups).filter(([key]) => buckets.has(key))
      .map(([key,title]) => ({key,title,items:buckets.get(key)}));
  }
  function line(item) {
    const {label, detail} = describe(item);
    return [label, detail].filter(Boolean).join(' · ');
  }
  const api = {describe, section, sections, line};
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.SubtitleQueueModel = api;
})(typeof window === 'undefined' ? globalThis : window);
