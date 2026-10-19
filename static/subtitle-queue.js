'use strict';
(() => {
  shell('subtitles');
  let queue = null, releases = [], loading = false;
  const now = () => Date.now() / 1000;
  function release(item) {
    return releases.filter(r => r.show_id === item.show_id && r.season === item.season && r.episode === item.episode)
      .sort((a, b) => FinishedReleases.compare(b, a))[0];
  }
  function row(item) {
    const d = SubtitleQueueModel.describe(item, now()), r = ['v0_ready', 'done'].includes(item.status) ? release(item) : null;
    return `<article class="queue-row" data-tone="${d.tone}"><div class="queue-ep"><a href="/show/${item.show_id}">${esc(item.title)}</a><span>第 ${item.episode} 集</span></div>` +
      `<div class="queue-state"><b>${esc(d.label)}</b>${d.detail ? `<span>${esc(d.detail)}</span>` : ''}</div>` +
      `<div class="queue-acts">${r ? `<a class="btn small" href="${esc(r.player_url)}">${icon('play')}<span class="lbl">播放 ${esc(FinishedReleases.label(r))}</span></a>` : ''}</div></article>`;
  }
  function render() {
    const t = now(), s = queue.summary;
    $('#summary').textContent = `${s.producing + s.starting} 集制作中 · ${s.queued} 集排队 · ${s.preparing + s.downloading} 集准备中 · ${s.waiting_video} 集等视频${s.stalled ? ` · ${s.stalled} 集可能卡住` : ''}`;
    $('#health').textContent = SubtitleQueueModel.health(queue, t);
    $('#queue').innerHTML = SubtitleQueueModel.sections(queue, t).map(sec =>
      `<section class="queue-section" data-key="${sec.key}"><h2 class="group-head"><b>${sec.title}</b><span class="count">${sec.items.length}</span></h2>${sec.items.map(row).join('')}</section>`).join('')
      || '<p class="empty">在追的作品没有待制作的集。</p>';
    $('#updated').textContent = `更新于 ${new Date(queue.generated_at * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'})} · 每分钟自动刷新`;
  }
  async function load() {
    if (loading) return;
    loading = true;
    try {
      await boot();
      const [q, r] = await Promise.allSettled([api('/subtitle-queue'), FinishedReleases.load()]);
      if (q.status === 'rejected') throw q.reason;
      queue = q.value;
      if (r.status === 'fulfilled') releases = r.value;
      $('#notice').hidden = true;
      render();
    } catch (e) {
      $('#notice').hidden = false;
      $('#notice').textContent = e.message;
    } finally { loading = false; }
  }
  $('#refresh').addEventListener('click', load);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); }, 60000);
  load();
})();
