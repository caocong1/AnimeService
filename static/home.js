'use strict';
/* 追番: tonight's screenings, what is on its way, and every tracked show. */
(() => {
  const legacy = { autumn: 1, summer: 0 };
  const hash = location.hash.slice(1), sid = new URLSearchParams(location.search).get('show');
  if (sid && /^\d+$/.test(sid)) return location.replace('/show/' + sid);
  if (hash === 'status' || hash === 'settings') return location.replace('/manage#' + hash);
  if (hash in legacy) {
    boot().then(() => location.replace('/season?q=' + (App.quarters[legacy[hash]] || '')));
    return;
  }

  shell('home');
  const ACTIVE = new Set(['watching', 'trial', 'wish']);
  const params = new URLSearchParams(location.search);
  let shows = [], filter = params.get('state') || '', query = params.get('q') || '';

  function facts(s) {
    const out = [stateChip(s)];
    out.push(`<span><b>${s.watched}</b>/${s.total || '?'}</span>`);
    if (s.unwatched) out.push(`<span>待看 <b>${s.unwatched}</b></span>`);
    return out.join('');
  }

  function screening(s, lead) {
    const n = s.next;
    const prog = n?.s === 'resume' && n.duration
      ? `<div class="progress" style="--p:${Math.round(n.position / n.duration * 100)}%"><span class="track"><i></i></span><span>${clock(n.position)} / ${clock(n.duration)}</span></div>` : '';
    const t = ticket(s).replace('class="ticket', `class="ticket${lead ? ' hero' : ''}`);
    return `<article class="screening${lead ? ' lead' : ''}${s.state === 'paused' ? ' dim' : ''}" style="${lineColour(s.id)}">
      ${cover(s.image, s.title, '', '/show/' + s.id)}
      <div class="body"><div class="head"><a class="title" href="/show/${s.id}">${esc(s.title)}</a><div class="facts">${facts(s)}</div></div>${prog}${epStrip(s)}</div>
      ${t}</article>`;
  }

  function sideItem(s, lead, sub, extra = '') {
    return `<a class="side-item" href="/show/${s.id}">${lead}<span><span class="name">${esc(s.title)}</span><span class="sub">${sub}</span>${extra}</span></a>`;
  }

  function render(lastPlayed) {
    const active = shows.filter(s => ACTIVE.has(s.state));
    const playing = active.filter(s => s.next).sort((a, b) =>
      (lastPlayed[b.id] || 0) - (lastPlayed[a.id] || 0) || b.download_updated - a.download_updated);
    const unwatched = active.reduce((n, s) => n + s.unwatched, 0);
    $('#summary').innerHTML = shows.length ? `待看 <b class="num">${unwatched}</b> 集 · 在追与试看 <b class="num">${shows.filter(s => s.state === 'watching' || s.state === 'trial').length}</b> 部` : '';

    if (!shows.length) {
      $('#screens').innerHTML = `<div class="empty"><h2>还没有追番</h2><a class="btn primary" href="/season">${icon('season')}去看新番</a></div>`;
      $('#side').innerHTML = '';
      $('#shelf-section').hidden = true;
      return;
    }
    $('#screens').innerHTML = playing.length
      ? `<h2 class="group-head"><b>可以看</b><span class="count">${playing.length}</span></h2>` + playing.map((s, i) => screening(s, i === 0)).join('')
      : `<div class="empty"><h2>待看的都看完了</h2><a class="btn" href="/season">${icon('season')}看看新番</a></div>`;

    const coming = [], waiting = [], upcoming = [];
    for (const s of active) {
      const dl = (s.eps || []).filter(e => e.s === 'downloading'), miss = (s.eps || []).filter(e => e.s === 'missing').map(e => e.n);
      if (dl.length || miss.length) {
        const parts = dl.map(e => `第 ${e.n} 集 ${e.progress}%`);
        if (miss.length) parts.push(`<span class="warn-line">缺 ${ranges(miss)}</span>`);
        coming.push(sideItem(s, cover(s.image, s.title), parts.join(' · '), dl.length ? `<span class="bar-p" style="--p:${dl[0].progress}%"><i></i></span>` : ''));
      }
      const aired = (s.eps || []).some(e => e.s !== 'future');
      if (!aired && s.air_date > today()) upcoming.push(s);
      else if (!s.next && (s.eps || []).some(e => e.s === 'future') && !(s.eps || []).some(e => e.s === 'missing' || e.s === 'downloading')) waiting.push(s);
    }
    upcoming.sort((a, b) => a.air_date.localeCompare(b.air_date));
    const paused = shows.filter(s => s.state === 'paused');
    const group = (title, items) => items.length ? `<section><h2 class="group-head"><b>${title}</b><span class="count">${items.length}</span></h2><div class="side-list">${items.join('')}</div></section>` : '';
    const nextFuture = s => (s.eps || []).find(e => e.s === 'future');
    $('#side').innerHTML =
      group('下载 · 缺集', coming.slice(0, 8)) +
      group('即将首播', upcoming.map(s => sideItem(s, `<span class="date">${esc(airDate(s.air_date, false))}</span>`, `${WEEK[new Date(s.air_date + 'T00:00').getDay()]} · ${STATES[s.state]}${s.authorized ? ' · 自动下载' : ''}`))) +
      group('等更新', waiting.map(s => { const f = nextFuture(s); return sideItem(s, cover(s.image, s.title), `${s.watched}/${s.total || '?'} · 第 ${f.n} 集${f.date ? ' ' + airDate(f.date) : ''}`); })) +
      group('暂搁', paused.map(s => sideItem(s, cover(s.image, s.title), `${s.watched}/${s.total || '?'}${s.unwatched ? ' · 待看 ' + s.unwatched : ''}`)));
    renderShelf();
  }

  function renderShelf() {
    const counts = {};
    for (const s of shows) counts[s.state] = (counts[s.state] || 0) + 1;
    $('#state-filter').innerHTML = [['', '全部', shows.length], ...Object.entries(STATES).map(([k, v]) => [k, v, counts[k] || 0])]
      .filter(([k, , n]) => !k || n).map(([k, v, n]) => `<button type="button" data-f="${k}" aria-pressed="${filter === k}">${v} <span class="num">${n}</span></button>`).join('');
    const q = query.trim().toLowerCase();
    const rows = shows.filter(s => (!filter || s.state === filter) && (!q || (s.title + ' ' + s.original).toLowerCase().includes(q)));
    $('#shelf-count').textContent = rows.length;
    $('#shelf').innerHTML = rows.length ? rows.map(s => `<div class="shelf-row" style="${lineColour(s.id)}">${cover(s.image, s.title, '', '/show/' + s.id)}
      <div><a class="name" href="/show/${s.id}">${esc(s.title)}</a><div class="facts">${facts(s)}</div></div>${epStrip(s)}<div class="end">${ticket(s, false)}</div></div>`).join('')
      : `<div class="empty"><p>没有符合「${esc(query || STATES[filter] || '')}」的作品</p><button class="btn small" type="button" id="clear">清除筛选</button></div>`;
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (filter) p.set('state', filter);
    if (query) p.set('q', query);
    history.replaceState(null, '', location.pathname + (p.size ? '?' + p : ''));
  }

  $('#q').value = query;
  $('#q').addEventListener('input', e => { if (e.isComposing) return; query = e.target.value; syncUrl(); renderShelf(); });
  $('#q').addEventListener('compositionend', e => { query = e.target.value; syncUrl(); renderShelf(); });
  document.addEventListener('click', e => {
    const f = e.target.closest('[data-f]');
    if (f) { filter = f.dataset.f; syncUrl(); renderShelf(); }
    if (e.target.closest('#clear')) { filter = ''; query = ''; $('#q').value = ''; syncUrl(); renderShelf(); }
    if (e.target.closest('#retry')) load();
  });

  async function load() {
    try {
      await boot();
      const forced = params.get('state');
      if (forced === 'error') throw Error('后台没有响应');
      const [list, home] = await Promise.all([api('/shows?scope=history'), api('/home')]);
      if (forced === 'empty-first') { list.length = 0; home.resume = []; }
      shows = list;
      $('#shelf-section').hidden = false;
      const lastPlayed = {};
      for (const r of home.resume) lastPlayed[r.show_id] = Math.max(lastPlayed[r.show_id] || 0, r.progress.last_played);
      render(lastPlayed);
    } catch (e) {
      $('#shelf-section').hidden = true;
      $('#side').innerHTML = '';
      $('#screens').innerHTML = `<div class="empty"><h2>连不上后台</h2><p>${esc(e.message)}</p><button class="btn" type="button" id="retry">重试</button></div>`;
    }
  }
  load();
  addEventListener('pageshow', e => e.persisted && load());
})();
