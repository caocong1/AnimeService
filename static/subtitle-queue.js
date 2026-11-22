'use strict';
(() => {
  shell('subtitles');
  let queue = null, releases = new Map(), loading = false, loadRevision = 0;

  let versionsLoaded = false, versionsError = false, queueError = false;
  function notice() {
    $('#notice').hidden = !versionsError && !queueError;
    $('#notice').textContent = queueError ? '暂时读不到字幕进度，请稍后刷新。'
      : versionsError ? '暂时读不到播放版本，请稍后刷新。' : '';
  }
  const episodeKey = item => [item.show_id, item.season, item.episode].join('/');
  function row(item) {
    const d = SubtitleQueueModel.describe(item), r = item.release;
    return `<article class="queue-row" data-tone="${d.tone}"><div class="queue-ep"><a href="/show/${item.show_id}">${esc(item.title)}</a><span>第 ${item.episode} 集</span></div>` +
      `<div class="queue-state"><b>${esc(d.label)}</b>${d.detail ? `<span>${esc(d.detail)}</span>` : ''}</div>` +
      `<div class="queue-acts">${r ? `<a class="btn small" href="${esc(r.player_url)}">${icon('play')}<span class="lbl">播放 ${esc(FinishedReleases.label(r))}</span></a>` : ''}</div></article>`;
  }
  function render() {
    const items = queue.items.map(item => {
      const r = releases.get(episodeKey(item));
      if (r) return {...item, release:r, status:r.edition === 'v1' ? 'done' : 'v0_ready'};
      return ['done','v0_ready'].includes(item.status) ? {...item,status:versionsError || versionsLoaded ? 'release_unavailable' : 'checking_release'} : item;
    });
    const groups = SubtitleQueueModel.sections({items});
    $('#summary').textContent = groups.map(g => g.title + ' ' + g.items.length + ' 集').join(' · ');
    $('#queue').innerHTML = groups.map(sec =>
      `<section class="queue-section" data-key="${sec.key}"><h2 class="group-head"><b>${sec.title}</b><span class="count">${sec.items.length}</span></h2>${sec.items.map(row).join('')}</section>`).join('')
      || '<p class="empty">在追的作品没有待制作的集。</p>';
    $('#updated').textContent = `更新于 ${new Date(queue.generated_at * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'})} · 每分钟自动刷新`;
  }
  async function load() {
    if (loading) return;
    loading = true; const revision = ++loadRevision;
    try {
      Promise.resolve(boot()).catch(() => {});
      FinishedReleases.load({verifyMedia: true}).then(value => {
        if (revision !== loadRevision) return;
        const latest = new Map();
        for (const version of value) {
          const key = episodeKey(version), prior = latest.get(key);
          if (!prior || FinishedReleases.compare(version, prior) > 0) latest.set(key, version);
        }
        releases = latest; versionsLoaded = true; versionsError = false; notice();
        if (queue) render();
      }).catch(() => { if (revision === loadRevision) { versionsError = true; notice(); if (queue) render(); } });
      queue = await api('/subtitle-queue');
      queueError = false; notice();
      render();
    } catch (e) {
      queueError = true; notice();
    } finally { loading = false; }
  }
  $('#refresh').addEventListener('click', load);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); }, 60000);
  load();
})();
