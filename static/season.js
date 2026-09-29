'use strict';
/* 新番: browse a quarter by cover and decide what to follow. */
(() => {
  shell('season');
  const p = new URLSearchParams(location.search);
  let quarter = p.get('q') || '', kind = p.get('kind') || 'series', sort = p.get('sort') || 'air', view = p.get('view') || 'grid', query = p.get('search') || '';
  let rows = [], seq = 0;

  const weekday = s => /^\d{4}-\d{2}-\d{2}/.test(s || '') ? new Date(s.slice(0, 10) + 'T00:00').getDay() : -1;

  function card(s) {
    const pick = s.selected
      ? `<span class="chip" data-state="${s.state}">${icon('check')}${STATES[s.state]}</span>`
      : `<button class="btn" type="button" data-wish="${s.id}" aria-label="想看 ${esc(s.title)}">${icon('plus')}想看</button>`;
    const facts = [s.air_date ? airDate(s.air_date) : '日期未定', s.total ? s.total + ' 集' : '', s.score ? '★ ' + s.score : '',
      s.quarter_kind === 'continuing' ? '跨季' : s.quarter_kind === 'early' ? '提前开播' : '', s.region_confidence === '地区待核实' ? '地区待核实' : '']
      .filter(Boolean).map(x => `<span>${esc(x)}</span>`).join('');
    return `<article class="poster"><a href="/show/${s.id}">${cover(s.image, s.title)}<span class="name">${esc(s.title)}</span></a><div class="facts">${facts}</div><div class="pick">${pick}</div></article>`;
  }

  function render() {
    const q = query.trim().toLowerCase();
    let list = rows.filter(s => !q || (s.title + ' ' + s.original).toLowerCase().includes(q));
    list.sort(sort === 'score' ? (a, b) => b.score - a.score : (a, b) => (a.air_date || '9999').localeCompare(b.air_date || '9999'));
    const picked = rows.filter(s => s.selected).length;
    $('#summary').textContent = rows.length ? `${rows.length} 部 · 已加入 ${picked} 部` : '';
    if (!rows.length) {
      $('#list').innerHTML = `<div class="empty"><h2>${esc(quarterName(quarter))}还没有目录</h2><button class="btn primary" type="button" data-sync>刷新目录</button></div>`;
      return;
    }
    if (!list.length) {
      $('#list').innerHTML = `<div class="empty"><p>没有匹配「${esc(query)}」的作品</p><button class="btn small" type="button" id="clear">清除搜索</button></div>`;
      return;
    }
    if (view === 'week') {
      const days = [1, 2, 3, 4, 5, 6, 0, -1];
      $('#list').innerHTML = '<div class="week">' + days.map(d => {
        const items = list.filter(s => weekday(s.air_date) === d);
        return items.length ? `<section><h2>${d < 0 ? '日期未定' : WEEK[d]}<span class="count">${items.length}</span></h2><div class="posters">${items.map(card).join('')}</div></section>` : '';
      }).join('') + '</div>';
    } else $('#list').innerHTML = `<div class="posters">${list.map(card).join('')}</div>`;
  }

  function syncUrl() {
    const u = new URLSearchParams({ q: quarter });
    if (kind !== 'series') u.set('kind', kind);
    if (sort !== 'air') u.set('sort', sort);
    if (view !== 'grid') u.set('view', view);
    if (query) u.set('search', query);
    history.replaceState(null, '', '/season?' + u);
  }

  async function load() {
    const id = ++seq;
    syncUrl();
    $('#quarters').innerHTML = [...App.quarters].reverse().map(x => `<a href="/season?q=${x}"${x === quarter ? ' aria-current="page"' : ''} data-q="${x}">${quarterName(x)}</a>`).join('');
    try {
      const data = await api('/shows?' + new URLSearchParams({ quarter, kind, scope: 'catalog' }));
      if (id !== seq) return;
      rows = data;
      render();
    } catch (e) {
      $('#list').innerHTML = `<div class="empty"><h2>读取失败</h2><p>${esc(e.message)}</p><button class="btn" type="button" id="retry">重试</button></div>`;
    }
  }

  $('#q').value = query; $('#kind').value = kind; $('#sort').value = sort;
  $$('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
  $('#q').addEventListener('input', e => { if (e.isComposing) return; query = e.target.value; syncUrl(); render(); });
  $('#q').addEventListener('compositionend', e => { query = e.target.value; syncUrl(); render(); });
  $('#kind').addEventListener('change', e => { kind = e.target.value; load(); });
  $('#sort').addEventListener('change', e => { sort = e.target.value; syncUrl(); render(); });

  document.addEventListener('click', e => {
    const b = e.target.closest('button, a[data-q]');
    if (!b) return;
    if (b.dataset.q) { e.preventDefault(); quarter = b.dataset.q; load(); return; }
    if (b.dataset.view) { view = b.dataset.view; $$('[data-view]').forEach(x => x.setAttribute('aria-pressed', String(x === b))); syncUrl(); render(); return; }
    if (b.id === 'clear') { query = ''; $('#q').value = ''; syncUrl(); render(); return; }
    if (b.id === 'retry') { load(); return; }
    if (b.id === 'sync' || 'sync' in b.dataset) return act(b, async () => {
      await api('/catalog/sync', { quarters: [quarter] });
      toast('正在刷新目录');
      setTimeout(load, 8000);
    });
    if (b.dataset.wish) return act(b, async () => {
      const sid = Number(b.dataset.wish), s = rows.find(x => x.id === sid);
      await api(`/shows/${sid}/state`, { state: 'wish' });
      Object.assign(s, { selected: 1, state: 'wish' });
      render();
      toast('已加入想看：' + s.title, { label: '撤销', run: () => act(null, async () => { await api(`/shows/${sid}/unselect`, {}); s.selected = 0; render(); }) });
    });
  });

  boot().then(() => {
    if (!App.quarters.includes(quarter)) quarter = App.quarters[App.quarters.length - 1];
    load();
  }).catch(e => toast(e.message));
})();
