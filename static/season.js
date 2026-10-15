'use strict';
/* Discovery starts across years; searching never selects or authorizes a show. */
(() => {
  shell('season');
  const kinds = { all: '全部作品', tv: 'TV 动画', web: '网络动画', movie: '剧场版', ova: 'OVA / OAD', continuing: '跨季续播', series: 'TV · 网络' };
  let quarter = '', year = '', kind = 'all', sort = 'recent', view = 'grid', query = '', source = 'local', unknown = false;
  let rows = [], seq = 0, next = null, loaded = false, searchTimer;
  const weekday = s => /^\d{4}-\d{2}-\d{2}/.test(s || '') ? new Date(s.slice(0, 10) + 'T00:00').getDay() : -1;
  function readUrl() {
    const p = new URLSearchParams(location.search);
    quarter = /^\d{4}-(01|04|07|10)$/.test(p.get('q') || '') ? p.get('q') : '';
    year = /^\d{4}$/.test(p.get('year') || '') ? p.get('year') : (quarter ? quarter.slice(0, 4) : '');
    kind = Object.hasOwn(kinds, p.get('kind')) ? p.get('kind') : 'all';
    unknown = p.get('unknown') === '1' || p.get('kind') === 'uncertain';
    sort = ['recent', 'air', 'score'].includes(p.get('sort')) ? p.get('sort') : 'recent';
    view = p.get('view') === 'week' ? 'week' : 'grid'; query = (p.get('search') || '').slice(0, 100);
    source = p.get('source') === 'online' && kind !== 'continuing' ? 'online' : 'local';
    if (source === 'online') quarter = '';
  }
  function syncUrl() {
    const u = new URLSearchParams();
    for (const [key, value] of [['q', quarter], ['year', year], ['kind', kind === 'all' ? '' : kind], ['sort', sort === 'recent' ? '' : sort], ['view', view === 'grid' ? '' : view], ['search', query], ['source', source === 'local' ? '' : source], ['unknown', unknown ? '1' : '']]) if (value) u.set(key, value);
    history.replaceState(null, '', '/season' + (u.size ? '?' + u : ''));
  }
  function controls() {
    $('#q').value = query; $('#sort').value = sort; $('#unknown').checked = unknown;
    const onlineYears = source === 'online' ? Array.from({ length: new Date().getFullYear() - 1898 }, (_, i) => new Date().getFullYear() + 1 - i) : [];
    const years = [...new Set([...onlineYears, ...(App.catalog_years || []), ...App.quarters.map(x => Number(x.slice(0, 4))), ...(year ? [Number(year)] : [])])].sort((a, b) => b - a);
    $('#year').innerHTML = '<option value="">全部年份</option>' + years.map(y => '<option value="' + y + '">' + y + ' 年</option>').join(''); $('#year').value = year;
    $('#quarter').innerHTML = '<option value="">不限季度</option>' + (year ? ['01', '04', '07', '10'].map(m => '<option value="' + year + '-' + m + '">' + Number(m) + ' 月季度</option>').join('') : '');
    $('#quarter').value = quarter; $('#quarter').disabled = !year; $('#quarter-field').hidden = source === 'online'; $('#sync').hidden = source === 'online';
    $$('[data-kind]').forEach(b => b.setAttribute('aria-current', b.dataset.kind === kind ? 'true' : 'false'));
    $$('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.source === source)));
    $$('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
    $('#search-hint').textContent = source === 'online' ? 'Bangumi 动画资料，按页筛选。查看详情可收录，点「想看」加入个人列表。' : '输入名称筛选已收录；未收录的动画可到 Bangumi 查找。';
  }
  function matches(s) {
    if (!unknown && s.region_confidence === '地区待核实') return false;
    return kind === 'all' || (kind === 'continuing' ? s.quarter_kind === 'continuing' : (kind === 'series' ? ['TV', 'WEB'] : { tv: ['TV'], web: ['WEB'], movie: ['剧场版'], ova: ['OVA'] }[kind] || []).includes(s.platform));
  }
  function card(s) {
    const local = source === 'local' || s.in_catalog;
    const link = local ? 'href="/show/' + s.id + '"' : 'href="https://bgm.tv/subject/' + s.id + '" data-detail="' + s.id + '"';
    const pick = s.selected ? '<span class="chip" data-state="' + esc(s.state) + '">' + icon('check') + esc(STATES[s.state] || s.state) + '</span>' : '<button class="btn" type="button" data-wish="' + s.id + '" aria-label="想看 ' + esc(s.title) + '">' + icon('plus') + '想看</button>';
    const facts = [s.platform || '类型待核实', s.air_date || '日期未定', s.total ? s.total + ' 集' : '', s.score ? '★ ' + s.score : '', s.quarter_kind === 'continuing' ? '跨季续播' : s.quarter_kind === 'early' ? '提前开播' : '', s.region_confidence === '地区待核实' ? '地区待核实' : ''].filter(Boolean).map(x => '<span>' + esc(x) + '</span>').join('');
    return '<article class="poster"><a ' + link + '>' + cover(s.image, s.title) + '<span class="name">' + esc(s.title) + '</span></a><div class="facts">' + facts + '</div><div class="pick">' + pick + '</div></article>';
  }
  function render() {
    controls(); const q = query.trim().toLowerCase();
    const list = rows.filter(s => matches(s) && (source === 'online' || !q || (s.title + ' ' + s.original).toLowerCase().includes(q)));
    list.sort(sort === 'score' ? (a, b) => b.score - a.score : (a, b) => { if (!a.air_date || !b.air_date) return Number(!a.air_date) - Number(!b.air_date); return sort === 'recent' ? b.air_date.localeCompare(a.air_date) : a.air_date.localeCompare(b.air_date); });
    $('#summary').textContent = (source === 'online' ? 'Bangumi 全站' : '已收录目录') + ' · ' + (quarter ? quarterName(quarter) : year ? year + ' 年' : '全部年份') + ' · ' + kinds[kind] + ' · ' + list.length + ' 部' + (source === 'online' ? '（已加载，按页筛选）' : '');
    $('#paging').innerHTML = source === 'online' && next !== null ? '<button class="btn" type="button" id="more">加载下一页</button><p class="meta">类型与地区按每页筛选，排序仅作用于已加载作品。</p>' : '';
    if (!list.length) { $('#list').innerHTML = '<div class="empty"><h2>' + (query ? '没有匹配「' + esc(query) + '」的作品' : source === 'online' ? '本页没有符合筛选的作品' : '这个范围还没有作品') + '</h2><p>' + (source === 'online' ? '试试其他类型、年份，或继续加载下一页。' : '可重置筛选，或到 Bangumi 查找未收录作品。') + '</p><div class="discover-empty-actions"><button class="btn" type="button" data-reset>重置筛选</button>' + (source === 'local' ? '<button class="btn primary" type="button" data-online>在 Bangumi 查找</button>' : '') + '</div></div>'; return; }
    if (view === 'week') $('#list').innerHTML = '<div class="week">' + [1, 2, 3, 4, 5, 6, 0, -1].map(d => { const items = list.filter(s => weekday(s.air_date) === d); return items.length ? '<section><h2>' + (d < 0 ? '日期未定' : WEEK[d]) + '<span class="count">' + items.length + '</span></h2><div class="posters">' + items.map(card).join('') + '</div></section>' : ''; }).join('') + '</div>';
    else $('#list').innerHTML = '<div class="posters">' + list.map(card).join('') + '</div>';
  }
  async function load(append = false) {
    clearTimeout(searchTimer); const id = ++seq; syncUrl(); controls(); loaded = false;
    $('#list').setAttribute('aria-busy', 'true'); $('#summary').textContent = source === 'online' ? '正在查找 Bangumi…' : '正在读取目录…';
    if (!append) { rows = []; $('#paging').innerHTML = ''; $('#list').innerHTML = '<div class="posters"><div class="skeleton" style="aspect-ratio:3/4"></div><div class="skeleton" style="aspect-ratio:3/4"></div></div>'; }
    try {
      const args = { kind: source === 'online' ? kind : 'all', include_unknown: source === 'online' ? String(unknown) : 'true' }; if (year) args.year = year;
      let data;
      if (source === 'online') { args.q = query.trim(); args.offset = append ? next : 0; data = await api('/catalog/discover?' + new URLSearchParams(args)); }
      else { if (quarter) args.quarter = quarter; args.scope = 'catalog'; data = await api('/shows?' + new URLSearchParams(args)); }
      if (id !== seq) return;
      const incoming = source === 'online' ? data.items : data;
      rows = append ? [...new Map([...rows, ...incoming].map(s => [s.id, s])).values()] : incoming; next = source === 'online' ? data.next_offset : null; loaded = true; render();
    } catch (e) {
      if (id !== seq) return;
      $('#summary').textContent = '目录读取失败';
      if (append) { loaded = true; render(); $('#paging').innerHTML = '<p class="meta">' + esc(e.message) + '</p><button class="btn" id="more" type="button">重试下一页</button>'; }
      else $('#list').innerHTML = '<div class="empty"><h2>读取失败</h2><p>' + esc(e.message) + '</p><button class="btn" type="button" id="retry">重试</button></div>';
    } finally { if (id === seq) $('#list').setAttribute('aria-busy', 'false'); }
  }
  function online() { source = 'online'; quarter = ''; if (kind === 'continuing') kind = 'all'; load(); }
  function reset() { quarter = ''; year = ''; kind = 'all'; sort = 'recent'; unknown = false; query = ''; load(); }
  function searchInput(e) { if (e.isComposing) return; query = e.target.value; syncUrl(); if (source === 'local') { if (loaded) render(); } else { clearTimeout(searchTimer); searchTimer = setTimeout(() => load(), 450); } }
  $('#q').addEventListener('input', searchInput); $('#q').addEventListener('compositionend', searchInput);
  $('#search-form').addEventListener('submit', e => { e.preventDefault(); online(); });
  $('#year').addEventListener('change', e => { year = e.target.value; quarter = ''; load(); });
  $('#quarter').addEventListener('change', e => { quarter = e.target.value; load(); });
  $('#sort').addEventListener('change', e => { sort = e.target.value; syncUrl(); if (loaded) render(); });
  $('#unknown').addEventListener('change', e => { unknown = e.target.checked; if (source === 'online') load(); else { syncUrl(); if (loaded) render(); } });
  document.addEventListener('click', e => {
    const b = e.target.closest('button, a[data-detail]'); if (!b) return;
    if (b.dataset.kind) { const wasOnline = source === 'online'; kind = b.dataset.kind; if (kind === 'continuing') source = 'local'; if (wasOnline || !loaded) load(); else { syncUrl(); render(); } return; }
    if (b.dataset.source) { if (b.dataset.source === 'online') online(); else { source = 'local'; load(); } return; }
    if (b.dataset.view) { view = b.dataset.view; syncUrl(); if (loaded) render(); return; }
    if (b.id === 'reset' || 'reset' in b.dataset) { reset(); return; }
    if ('online' in b.dataset) { online(); return; }
    if (b.id === 'retry') { load(); return; }
    if (b.id === 'more') return act(b, () => load(true));
    if (b.id === 'sync') return act(b, async () => { const quarters = quarter ? [quarter] : year ? ['01', '04', '07', '10'].map(m => year + '-' + m) : App.quarters; await api('/catalog/sync', { quarters }); toast('正在刷新' + (quarter ? quarterName(quarter) : year ? year + ' 年目录' : '近期季度目录')); setTimeout(() => load(), 8000); });
    if (b.dataset.detail) { e.preventDefault(); return act(b, async () => { await api('/catalog/import/' + Number(b.dataset.detail), {}); location.assign('/show/' + Number(b.dataset.detail)); }); }
    if (b.dataset.wish) return act(b, async () => {
      const sid = Number(b.dataset.wish), s = rows.find(x => x.id === sid);
      if (source === 'online' && !s.in_catalog) { await api('/catalog/import/' + sid, {}); s.in_catalog = true; }
      await api('/shows/' + sid + '/state', { state: 'wish' }); Object.assign(s, { selected: 1, state: 'wish' }); render();
      toast('已加入想看：' + s.title + '；符合新番规则后会自动核验找源', { label: '撤销', run: () => act(null, async () => { await api('/shows/' + sid + '/unselect', {}); s.selected = 0; render(); }) });
    });
  });
  window.addEventListener('popstate', () => { readUrl(); load(); }); readUrl();
  boot().then(() => load()).catch(e => { $('#list').innerHTML = '<div class="empty"><h2>读取失败</h2><p>' + esc(e.message) + '</p><a class="btn" href="/season">刷新页面重试</a></div>'; toast(e.message); });
})();
