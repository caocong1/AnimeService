'use strict';
/* 放映室: the picture first; mark watched and go to the next episode without leaving. */
(() => {
  shell('');
  const params = new URLSearchParams(location.search), id = params.get('media');
  if (!id) return location.replace('/library');
  let played = false, art = null, media = null, show = null, lastPosition = 0, lastSave = 0, selected = [], comments = [], activeDanmu = null, danmuRev = 0, subRev = 0, tracks = [];
  const web = (path, body) => api('/web' + path, body);

  function screenState(html) {
    $('#screen-state').innerHTML = html;
    $('#screen-state').hidden = !html;
  }

  /* ---------- episodes of this show (or files of this folder) ---------- */

  const resumable = () => { const p = media?.progress; return !!p && p.position > 5 && p.position < (p.duration || 0) - 30; };
  const current = () => show?.board.eps.find(e => e.media === id);
  const nextEp = () => { const c = current(); return c && show.board.eps.find(e => e.n > c.n && e.media); };

  function renderActs() {
    const c = current(), n = nextEp(), pos = media?.progress?.position || 0;
    const parts = [];
    if (c) parts.push(c.s === 'watched'
      ? `<button class="btn" type="button" data-mark="false">${icon('check')}已看</button>`
      : `<button class="btn" type="button" data-mark="true">${icon('check')}标记看完</button>`);
    if (n) parts.push(`<a class="btn primary" href="${watchHref(n)}">${icon('next')}第 ${n.n} 集</a>`);
    if (resumable()) parts.push(`<button class="btn quiet" type="button" id="restart">从头播放</button>`);
    if (App.local) parts.push(`<button class="btn quiet" type="button" id="desktop" title="用本机播放器打开">${icon('desktop')}<span>本机播放器</span></button>`);
    $('#acts').innerHTML = parts.join('');
  }

  function renderList(items) {
    $('#playlist').innerHTML = `<h2>${show ? '分集' : '同一文件夹'}</h2>` + items;
    const list = $('#playlist'), here = $('#playlist [aria-current]');
    if (here && list.scrollHeight > list.clientHeight) list.scrollTop = here.offsetTop - list.clientHeight / 2;
  }

  async function loadShow() {
    show = await api('/shows/' + media.show_id);
    $('#parent').innerHTML = icon('back') + esc(show.show.title);
    $('#parent').href = '/show/' + show.show.id;
    const c = current();
    $('#title').textContent = c ? `第 ${c.n} 集` : media.name;
    document.title = `${show.show.title} 第 ${c?.n ?? media.episode} 集 · 番屿`;
    renderActs();
    const eps = show.board.eps.filter(e => e.s !== 'future');
    let run = 0;
    while (run < eps.length && eps[run].s === 'watched' && eps[run].media !== id) run++;
    const row = e => {
      const here = e.media === id, seen = e.s === 'watched';
      const sub = here ? '正在播放' : e.s === 'resume' ? '看到 ' + clock(e.position) : e.s === 'downloading' ? `下载中 ${e.progress}%` : EP_LABEL[e.s];
      const body = `<b>第 ${e.n} 集</b><span>${sub}</span>`;
      const mark = seen
        ? `<button class="btn small quiet mark" type="button" data-ep="${e.n}" data-finished="false" aria-pressed="true" aria-label="第 ${e.n} 集已看，改为未看" title="改为未看">${icon('check')}</button>`
        : `<button class="btn small mark" type="button" data-ep="${e.n}" data-finished="true" aria-label="第 ${e.n} 集标记看完">看完</button>`;
      return `<div class="pl-item" data-s="${e.s}"${here ? ' aria-current="true"' : ''}><span class="no">${here ? icon('play') : ''}</span>
        ${e.media && !here ? `<a class="go" href="${watchHref(e)}">${body}</a>` : `<span class="go">${body}</span>`}${mark}</div>`;
    };
    renderList((run > 1 ? `<details class="pl-fold"><summary class="pl-item"><span class="no">${icon('check')}</span><span class="go"><b>第 1–${run} 集</b><span>已看</span></span>${icon('chevron')}</summary>${eps.slice(0, run).map(row).join('')}</details>` : eps.slice(0, run).map(row).join(''))
      + eps.slice(run).map(row).join(''));
  }

  async function loadFolder() {
    $('#parent').innerHTML = icon('back') + esc(media.folder);
    $('#parent').href = '/library?q=' + encodeURIComponent(media.folder);
    $('#title').textContent = media.name;
    document.title = media.name + ' · 番屿';
    renderActs();
    const files = (await web('/media?limit=200&q=' + encodeURIComponent(media.folder))).filter(x => x.folder === media.folder);
    renderList(files.map(x => `<div class="pl-item"${x.id === id ? ' aria-current="true"' : ''}><span class="no">${x.episode ?? '·'}</span><a class="go" href="/watch?media=${x.id}"><span class="file">${esc(x.name)}</span><span>${gib(x.size)}</span></a><span></span></div>`).join(''));
  }

  async function mark(n, finished) {
    await api(`/shows/${show.show.id}/watch`, { episode: n, finished });
    await loadShow();
    const nx = nextEp();
    toast(`第 ${n} 集${finished ? '已看' : '改为未看'}`, finished && nx && current()?.n === n ? { label: `播放第 ${nx.n} 集`, run: () => location.assign('/watch?media=' + nx.media) } : null);
  }

  /* ---------- player ---------- */

  /* Only real playback moves the stored position; opening or leaving the page never does. */
  async function saveProgress(keepalive = false) {
    if (!played || !art || !Number.isFinite(art.duration) || art.duration <= 0) return;
    const delta = art.currentTime - lastPosition, playing = !art.paused && delta > 0 && delta < 35;
    lastPosition = art.currentTime;
    await fetch(`/api/web/media/${id}/progress`, {
      method: 'POST', keepalive,
      headers: { 'Content-Type': 'application/json', 'X-Anime-Token': App.token },
      body: JSON.stringify({ position: art.currentTime, duration: art.duration, playing }),
    });
  }

  function desktopButton() {
    return App.local ? `<button class="btn" type="button" id="desktop">${icon('desktop')}用本机播放器打开</button>` : '';
  }

  function openPlayer() {
    if (typeof window.Artplayer !== 'function') {
      screenState(`<b>网页播放组件未安装</b>${desktopButton()}`);
      return;
    }
    screenState('');
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--signal').trim();
    art = new Artplayer({
      container: '#screen',
      url: `/api/web/media/${id}/stream`,
      type: media.name.toLowerCase().endsWith('.mp4') ? 'mp4' : 'mkv',
      volume: 0.7, theme: accent, lang: 'zh-cn', setting: true, playbackRate: true, fullscreen: true, fullscreenWeb: true, hotkey: true,
      subtitle: { escape: true, style: { color: '#fff', fontSize: 'clamp(16px, 2.2vw, 28px)', textShadow: '0 1px 3px #000, 1px 0 2px #000' } },
      plugins: [artplayerPluginDanmuku({ danmuku: [], emitter: false, fontSize: 22, opacity: 0.85, antiOverlap: true, margin: [10, '25%'], beforeEmit: () => false })],
    });
    art.on('video:loadedmetadata', () => {
      if (resumable()) { art.currentTime = media.progress.position; lastPosition = art.currentTime; }
      art.play().catch(() => {
        art.muted = true;
        art.play().then(() => {
          $('#acts').insertAdjacentHTML('afterbegin', `<button class="btn" type="button" id="unmute">开声音</button>`);
        }).catch(() => {});
      });
    });
    art.on('video:playing', () => { played = true; });
    art.on('video:error', () => screenState(`<b>浏览器无法播放这个文件</b>${desktopButton()}`));
    art.on('video:timeupdate', () => { if (Date.now() - lastSave > 15000) { lastSave = Date.now(); saveProgress().catch(() => {}); } });
    art.on('video:pause', () => saveProgress().catch(() => {}));
    art.on('video:ended', () => {
      saveProgress().catch(() => {});
      const c = current(), n = nextEp();
      if (!c || c.s === 'watched') return;
      screenState(`<b>第 ${c.n} 集放完了</b><div class="links" style="justify-content:center">
        ${n ? `<button class="btn primary" type="button" data-end-next="${n.media}">${icon('check')}看完，播放第 ${n.n} 集</button>` : ''}
        <button class="btn" type="button" data-mark="true">${icon('check')}标记看完</button><button class="btn quiet" type="button" id="dismiss">不标记</button></div>`);
    });
    addEventListener('pagehide', () => saveProgress(true).catch(() => {}));
    loadSubtitles();
    autoDanmu();
  }

  /* ---------- subtitles ---------- */

  async function loadSubtitles() {
    const note = $('#sub-note'), sel = $('#subtitle');
    try {
      const d = await web(`/media/${id}/subtitles`);
      tracks = d.tracks;
      sel.innerHTML = '<option value="">关闭</option>' + d.tracks.map(t => `<option value="${t.index}"${t.supported ? '' : ' disabled'}>${esc(t.label)}${t.supported ? '' : '（图形字幕）'}</option>`).join('');
      sel.disabled = !d.tracks.some(t => t.supported);
      note.textContent = d.tracks.length ? '' : '无内嵌字幕';
      if (d.default !== null) { sel.value = String(d.default); await changeSubtitle(); }
    } catch (e) { note.textContent = e.message; }
  }
  async function changeSubtitle() {
    const rev = ++subRev, value = $('#subtitle').value, note = $('#sub-note');
    if (!art) return;
    if (value === '') { art.subtitle.show = false; note.textContent = ''; return; }
    const t = tracks.find(x => String(x.index) === value);
    note.textContent = '加载中…';
    try {
      const r = await fetch(t.url);
      if (!r.ok) throw Error((await r.json()).error || '字幕加载失败');
      await art.subtitle.switch(t.url, { type: 'vtt', name: t.label });
      if (rev !== subRev) return;
      art.subtitle.show = true;
      note.textContent = ['ass', 'ssa'].includes(t.codec) ? '特效排版未保留' : '';
    } catch (e) { if (rev === subRev) note.textContent = e.message; }
  }

  /* ---------- danmu ---------- */

  async function applyDanmu() {
    if (!art) return;
    const shift = Number($('#offset').value) || 0;
    await art.plugins.artplayerPluginDanmuku.load(comments.map(c => ({ ...c, time: Math.max(0, c.time + shift) })));
  }
  function renderSelected() {
    $('#danmu-selected').innerHTML = selected.map((x, i) => `<div class="list-row"><span>${esc(x.title)}</span><button class="btn small quiet" type="button" data-remove="${i}">移除</button></div>`).join('');
  }
  async function autoDanmu() {
    const rev = ++danmuRev, note = $('#danmu-note');
    $('#retry-danmu').disabled = false;
    note.textContent = '匹配中…';
    try {
      const d = await web(`/media/${id}/danmu`);
      if (rev !== danmuRev) return;
      if (d.status === 'matched') { selected = d.selected; comments = d.comments; renderSelected(); await applyDanmu(); note.textContent = `${comments.length} 条`; note.title = d.title || ''; }
      else { note.textContent = '未匹配'; note.title = d.message || ''; }
    } catch (e) { if (rev === danmuRev) note.textContent = '未连接'; note.title = e.message; }
  }

  /* ---------- events ---------- */

  document.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.mark) return act(b, async () => { screenState(''); await mark(current().n, b.dataset.mark === 'true'); });
    if (b.dataset.ep) return act(b, () => mark(Number(b.dataset.ep), b.dataset.finished === 'true'));
    if (b.dataset.endNext) return act(b, async () => { await api(`/shows/${show.show.id}/watch`, { episode: current().n, finished: true }); location.assign('/watch?media=' + b.dataset.endNext); });
    if (b.dataset.remove) { selected.splice(Number(b.dataset.remove), 1); renderSelected(); return; }
    if (b.dataset.anime) return act(b, async () => {
      const d = await web('/danmu/show/' + b.dataset.anime);
      activeDanmu = d.bangumi;
      $('#danmu-episodes').innerHTML = `<select id="danmu-ep" aria-label="集">${activeDanmu.episodes.map((x, i) => `<option value="${i}">${esc(x.episodeTitle)}</option>`).join('')}</select><button class="btn" type="button" id="add-source">添加</button>`;
    });
    switch (b.id) {
      case 'dismiss': return screenState('');
      case 'restart': if (art) { art.currentTime = 0; art.play(); b.remove(); } return;
      case 'unmute': if (art) { art.muted = false; b.remove(); } return;
      case 'desktop': return act(b, async () => { await web(`/media/${id}/desktop`, {}); toast('已在本机播放器打开'); });
      case 'retry-danmu': return autoDanmu();
      case 'add-source': {
        const ep = activeDanmu.episodes[Number($('#danmu-ep').value)];
        if (!ep.source_key) return toast('这个来源不能直接加载');
        if (selected.length >= 5) return toast('最多 5 个来源');
        if (!selected.some(x => x.id === ep.source_key)) selected.push({ id: ep.source_key, title: activeDanmu.animeTitle + ' · ' + ep.episodeTitle });
        return renderSelected();
      }
      case 'load-danmu': return act(b, async () => {
        const rev = ++danmuRev, sources = [...selected];
        const d = await web('/danmu/comments', { episodes: sources.map(x => x.id) });
        if (rev !== danmuRev) return;
        comments = d.comments;
        await applyDanmu();
        $('#danmu-note').textContent = `${comments.length} 条`;
        const failed = d.sources.filter(s => s.error);
        if (failed.length) toast(`${failed.length} 个来源失败：${failed[0].error}`);
      });
    }
  });
  $('#subtitle').addEventListener('change', changeSubtitle);
  $('#offset').addEventListener('change', () => applyDanmu().catch(e => toast(e.message)));
  $('#danmu-search').addEventListener('submit', e => {
    e.preventDefault();
    act(e.target.querySelector('button'), async () => {
      const d = await web('/danmu/search?q=' + encodeURIComponent($('#danmu-q').value.trim()));
      $('#danmu-results').innerHTML = (d.animes || []).map(x => `<button class="btn small" type="button" data-anime="${x.animeId}">${esc(x.animeTitle)} · ${x.episodeCount} 集</button>`).join('') || '<p class="muted">没有结果</p>';
    });
  });

  (async () => {
    try {
      await boot();
      media = await web('/media/' + id);
      $('#danmu-q').value = media.show_id ? '' : media.folder;
      openPlayer();
      if (media.show_id) { await loadShow(); $('#danmu-q').value = show.show.title; } else await loadFolder();
    } catch (e) {
      screenState(`<b>${/404|未找到/.test(e.message) ? '找不到这个文件' : '打不开这个文件'}</b><span>${esc(e.message)}</span><a class="btn" href="/library">去片库</a>`);
    }
  })();
})();
