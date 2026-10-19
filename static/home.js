'use strict';
/* Separate navigation views: screenings and the paginated personal list. */
(() => {
  const legacy = { autumn: 1, summer: 0 };
  const hash = location.hash.slice(1), sid = new URLSearchParams(location.search).get('show');
  if (sid && /^\d+$/.test(sid)) return location.replace('/show/' + sid);
  if (hash === 'status' || hash === 'settings') return location.replace('/manage#' + hash);
  if (hash in legacy) {
    boot().then(() => location.replace('/season?q=' + (App.quarters[legacy[hash]] || '')));
    return;
  }

  const params = new URLSearchParams(location.search);
  // Preserve old bookmarks that opened the shelf with a search/state filter.
  const all = params.get('view') === 'all' || params.has('q') || params.has('quarter') || params.getAll('state').some(s => Object.hasOwn(STATES, s));
  shell(all ? 'following' : 'home');
  const ACTIVE = new Set(['watching', 'trial', 'wish']);
  let shows = [], filters = new Set(), quarter = '', query = '', page = 1, pageSize = 20, stopObserving, loading = false, lastLoad = 0;
  function readUrl() {
    const p = new URLSearchParams(location.search);
    filters = new Set(p.getAll('state').filter(s => Object.hasOwn(STATES, s)));
    quarter = /^\d{4}-(01|04|07|10)$/.test(p.get('quarter') || '') ? p.get('quarter') : '';
    query = p.get('q') || '';
    const n = Number(p.get('page'));
    page = Number.isSafeInteger(n) && n > 0 ? n : 1;
    pageSize = [20, 40, 60].includes(Number(p.get('size'))) ? Number(p.get('size')) : 20;
    $('#q').value = query;
  }
  readUrl();
  $('#watch-board').hidden = all;
  $('#shelf-section').hidden = !all;
  $('h1').textContent = all ? '全部追番' : '追番';
  $('h1').tabIndex = -1;
  document.title = (all ? '全部追番' : '追番') + ' · 番屿';

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
    lastPlayed = {...lastPlayed};
    for (const s of shows) for (const e of s.eps || []) if (e.s === 'resume' && e.release?.progress?.last_played)
      lastPlayed[s.id] = Math.max(lastPlayed[s.id] || 0, e.release.progress.last_played);
    const active = shows.filter(s => ACTIVE.has(s.state));
    const playing = active.filter(s => s.next).sort((a, b) =>
      (lastPlayed[b.id] || 0) - (lastPlayed[a.id] || 0) || b.download_updated - a.download_updated);
    const unwatched = active.reduce((n, s) => n + s.unwatched, 0);
    $('#summary').innerHTML = shows.length ? `待看 <b class="num">${unwatched}</b> 集 · 在追与试看 <b class="num">${shows.filter(s => s.state === 'watching' || s.state === 'trial').length}</b> 部` : '';

    if (!shows.length) {
      $('#screens').innerHTML = `<div class="empty"><h2>还没有追番</h2><a class="btn primary" href="/season">${icon('season')}去看新番</a></div>`;
      $('#side').innerHTML = '';
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
        if (miss.length) parts.push(`<span class="warn-line">缺第 ${ranges(miss)}</span>`);
        const notes = (s.eps || []).filter(e => ['missing', 'downloading'].includes(e.s) && e.download).map(e => {
          const x = e.download, checked = x.checked_at ? new Date(x.checked_at * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'}) : '';
          const retry = x.retry_at ? new Date(x.retry_at * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '';
          return `<span class="download-reason">第 ${e.n} 集：${esc(x.reason)}${x.source_error ? '；部分查源失败：' + esc(x.source_error) : ''}${retry ? ' · ' + retry + ' 自动重试' : ''}${checked ? ' · ' + checked + ' 检查' : ''}</span>`;
        }).join('');
        coming.push(sideItem(s, cover(s.image, s.title), parts.join(' · ') + notes, dl.length ? `<span class="bar-p" style="--p:${dl[0].progress}%"><i></i></span>` : ''));
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
  }

  function renderShelf() {
    const quarters = [...new Set([...App.quarters, ...shows.flatMap(s => s.quarters || [s.quarter]), quarter].filter(q => /^\d{4}-(01|04|07|10)$/.test(q)))].sort().reverse();
    $('#following-quarter').innerHTML = '<option value="">不限季度</option>' + quarters.map(q => `<option value="${q}">${quarterName(q)}</option>`).join('');
    $('#following-quarter').value = quarter;
    const seasonal = shows.filter(s => !quarter || (s.quarters || [s.quarter]).includes(quarter));
    const counts = {};
    for (const s of seasonal) counts[s.state] = (counts[s.state] || 0) + 1;
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.f : undefined;
    $('#state-filter').innerHTML = [['', '全部', seasonal.length], ...Object.entries(STATES).map(([k, v]) => [k, v, counts[k] || 0])]
      .map(([k, v, n]) => `<button type="button" data-f="${k}" aria-pressed="${k ? filters.has(k) : !filters.size}">${v} <span class="num">${n}</span></button>`).join('');
    if (focused !== undefined) { const again = document.querySelector(`#state-filter [data-f="${focused}"]`); if (again) again.focus(); }
    const q = query.trim().toLowerCase();
    const rows = seasonal.filter(s => (!filters.size || filters.has(s.state)) && (!q || (s.title + ' ' + s.original).toLowerCase().includes(q)));
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    page = Math.min(page, pages);
    const start = (page - 1) * pageSize;
    $('#summary').textContent = `${quarter ? quarterName(quarter) + ' · ' : ''}${filters.size ? [...filters].map(s => STATES[s]).join('、') + ' · ' : ''}筛选结果 ${rows.length} 部`;
    $('#shelf').innerHTML = rows.length ? rows.slice(start, start + pageSize).map(s => `<div class="shelf-row" style="${lineColour(s.id)}">${cover(s.image, s.title, '', '/show/' + s.id)}
      <div><a class="name" href="/show/${s.id}">${esc(s.title)}</a><div class="facts">${facts(s)}</div></div>${epStrip(s)}<div class="end">${ticket(s, false)}</div></div>`).join('')
      : shows.length ? `<div class="empty"><p>没有符合当前筛选条件的作品</p><button class="btn small" type="button" id="clear">清除筛选</button></div>`
      : `<div class="empty"><h2>还没有追番</h2><a class="btn primary" href="/season">${icon('season')}去看新番</a></div>`;
    const numbers = [...new Set([1, pages, ...Array.from({length: 5}, (_, i) => page - 2 + i).filter(n => n > 0 && n <= pages)])].sort((a, b) => a - b);
    const links = numbers.map((n, i) => `${i && n - numbers[i - 1] > 1 ? '<span aria-hidden="true">…</span>' : ''}<button class="btn small" type="button" data-page="${n}" aria-label="第 ${n} 页"${n === page ? ' aria-current="page"' : ''}>${n}</button>`).join('');
    document.querySelectorAll('[data-pager]').forEach((el, i) => {
      el.hidden = !rows.length;
      el.innerHTML = `<span class="page-info" role="status">${start + 1}–${Math.min(start + pageSize, rows.length)} / ${rows.length} 部 · 第 ${page} / ${pages} 页</span>
        <label class="page-size" for="page-size-${i}">每页<select id="page-size-${i}" data-page-size>${[20, 40, 60].map(n => `<option value="${n}"${n === pageSize ? ' selected' : ''}>${n} 部</option>`).join('')}</select></label>
        <div class="page-links"><button class="btn small" type="button" data-page="${page - 1}"${page === 1 ? ' disabled' : ''}>上一页</button>${links}<button class="btn small" type="button" data-page="${page + 1}"${page === pages ? ' disabled' : ''}>下一页</button></div>`;
    });
    syncUrl();
  }

  function syncUrl(push = false) {
    const p = new URLSearchParams();
    p.set('view', 'all');
    for (const state of Object.keys(STATES)) if (filters.has(state)) p.append('state', state);
    if (quarter) p.set('quarter', quarter);
    if (query) p.set('q', query);
    if (page > 1) p.set('page', page);
    if (pageSize !== 20) p.set('size', pageSize);
    const url = location.pathname + '?' + p;
    if (url !== location.pathname + location.search) history[push ? 'pushState' : 'replaceState'](null, '', url);
  }

  function search(e) { if (e.isComposing) return; query = e.target.value; page = 1; syncUrl(); renderShelf(); }
  $('#q').addEventListener('input', search);
  $('#q').addEventListener('compositionend', search);
  document.addEventListener('change', e => {
    if (e.target.id === 'following-quarter') { quarter = e.target.value; page = 1; syncUrl(true); renderShelf(); return; }
    if (!e.target.matches('[data-page-size]')) return;
    pageSize = Number(e.target.value); page = 1; syncUrl(true); renderShelf();
    $('#' + e.target.id).focus();
  });
  document.addEventListener('click', e => {
    const f = e.target.closest('[data-f]');
    if (f) {
      const state = f.dataset.f;
      if (!state) filters.clear(); else if (filters.has(state)) filters.delete(state); else filters.add(state);
      page = 1; syncUrl(true); renderShelf(); $(`[data-f="${state}"]`).focus();
    }
    const p = e.target.closest('[data-page]');
    if (p && !p.disabled && Number(p.dataset.page) !== page) {
      page = Number(p.dataset.page); syncUrl(true); renderShelf();
      $('h1').focus({preventScroll: true}); window.scrollTo({top: 0, behavior: 'instant'});
    }
    if (e.target.closest('#clear')) { filters.clear(); quarter = ''; query = ''; page = 1; $('#q').value = ''; syncUrl(true); renderShelf(); $('#q').focus(); }
    if (e.target.closest('#retry')) load();
  });

  async function load(background = false) {
    if (loading) return;
    loading = true;
    try {
      await boot();
      const forced = params.get('state');
      if (forced === 'error') throw Error('后台没有响应');
      const [list, home, releases] = await Promise.all([api('/shows?scope=history'), all ? Promise.resolve({resume: []}) : api('/home'),
        FinishedReleases.load({verifyMedia: true}).catch(e => { toast(e.message); return []; })]);
      if (forced === 'empty-first') { list.length = 0; home.resume = []; }
      const scheduled = await applyListAiringSchedules(list);
      shows = FinishedReleases.applyList(scheduled, releases);
      lastLoad = Date.now();
      const lastPlayed = {};
      for (const r of home.resume) lastPlayed[r.show_id] = Math.max(lastPlayed[r.show_id] || 0, r.progress.last_played);
      stopObserving?.();
      stopObserving = FinishedReleases.observe(next => {
        shows = FinishedReleases.applyList(scheduled, next);
        if (all) renderShelf(); else render(lastPlayed);
      }, {initial: releases});
      $('#watch-board').hidden = all;
      $('#shelf-section').hidden = !all;
      if (all) { renderShelf(); return; }
      render(lastPlayed);
    } catch (e) {
      if (background) return;
      $('#shelf-section').hidden = true;
      $('#watch-board').hidden = false;
      $('#side').innerHTML = '';
      $('#screens').innerHTML = `<div class="empty"><h2>连不上后台</h2><p>${esc(e.message)}</p><button class="btn" type="button" id="retry">重试</button></div>`;
    } finally { loading = false; }
  }
  load();
  const refresh = () => { if (!document.hidden && Date.now() - lastLoad > 10000) load(true); };
  setInterval(refresh, 60000);
  addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', refresh);
  addEventListener('pageshow', e => { if (e.persisted) { readUrl(); load(); } });
  addEventListener('popstate', () => { readUrl(); if (all) renderShelf(); });
})();
