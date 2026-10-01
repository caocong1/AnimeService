'use strict';
/* 后台: is everything running; season collection; cleanup; the old library; settings. */
(() => {
  shell('manage');
  const SECTIONS = ['status', 'season', 'cleanup', 'legacy', 'settings'];
  const loaded = {};
  let status = null, season = null, plans = [], showAll = {};

  const table = (cls, head, rows) => `<div class="table-wrap"><table class="m-table ${cls}"><thead><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  const more = (key, list, n) => showAll[key] || list.length <= n ? list : list.slice(0, n);
  const moreBtn = (key, list, n) => list.length > n ? `<button class="btn small quiet" type="button" data-all="${key}">${showAll[key] ? '收起' : `全部 ${list.length} 条`}</button>` : '';

  /* ---------- 状态 ---------- */

  async function renderStatus(latest) {
    const s = status = latest || await api('/status');
    s.events.sort((a, b) => b.time - a.time);
    const alive = s.heartbeat && Date.now() / 1000 - s.heartbeat.time < 240;
    const { error: recentErr, reviews } = statusProblems(s);
    const dh = s.dandan_history || {};
    const tile = (k, v, sub, state = 'ok') => `<div class="tile" data-state="${state}"><span class="k">${k}</span><span class="v"><i></i>${v}</span>${sub ? `<span class="s${state === 'down' ? ' error-line' : ''}">${sub}</span>` : ''}</div>`;
    const errSources = s.sources.filter(x => x.error || x.archive_error);
    const sources = [...errSources, ...s.sources.filter(x => !(x.error || x.archive_error))];
    const closed = t => ['complete', 'cleaned', 'linked'].includes(t.status);
    const openTasks = s.tasks.filter(t => !closed(t));
    const tasks = [...openTasks, ...s.tasks.filter(closed)];
    $('#sec-status').innerHTML = `
      <h2 class="sr">状态</h2>
      <div class="tiles">
        ${tile('后台', !alive ? '未响应' : recentErr ? '有错误' : '运行中', recentErr ? `${ago(recentErr.time)} · ${esc(recentErr.message)}` : reviews.length ? `${reviews.length} 项下载待核查 · ${esc(reviews[0].error)}` : s.heartbeat ? '心跳 ' + ago(s.heartbeat.time) : '没有心跳', !alive ? 'down' : recentErr || reviews.length ? 'warn' : 'ok')}
        ${tile('下载器', esc(s.downloader?.label || '—'), s.downloader?.blocker ? esc(s.downloader.blocker) : `已开启自动下载 ${s.authorized_count} 部`, s.downloader?.blocker ? 'warn' : 'ok')}
        ${tile('观看同步', dh.ok ? '正常' : dh.error ? '失败' : '未同步', dh.ok ? `${ago(dh.success)} · 已同步 ${dh.synced_watched || 0} 集` : esc(dh.error || ''), dh.ok ? 'ok' : dh.error ? 'down' : 'warn')}
        ${tile('检查新集', `每 ${Math.round(s.schedule.poll_seconds / 60)} 分钟`, s.schedule.requested > s.schedule.completed ? '排队中' : '上次 ' + ago(s.schedule.finished), 'ok')}
      </div>
      <div class="links" style="margin-bottom:var(--s-6)">
        <button class="btn primary" type="button" id="check">${icon('refresh')}检查新集</button>
        ${App.local ? '<button class="btn" type="button" id="probe">检测弹弹play</button>' : ''}
      </div>
      <h3 class="m-sub">来源 <span class="muted num">${s.sources.length}</span>${errSources.length ? ` <span class="chip danger">${errSources.length} 个异常</span>` : ''}</h3>
      ${s.sources.length ? table('', ['作品', '来源', '最近成功', '最新 / 异常'], more('src', sources, 10).map(x => `<tr${x.error || x.archive_error ? ' class="is-error"' : ''}><td><a href="/show/${x.show_id}">${esc(x.title)}</a></td><td class="nowrap">${x.kind === 'mikan' ? '蜜柑' : '动漫花园'}${x.enabled ? '' : ' · 停用'}</td><td class="nowrap">${ago(x.last_success)}</td><td>${esc(x.error || x.archive_error || x.latest)}</td></tr>`)) + moreBtn('src', sources, 10) : '<p class="muted">没有订阅来源</p>'}
      <h3 class="m-sub">下载任务 <span class="muted num">${openTasks.length} 进行中</span></h3>
      ${s.tasks.length ? table('', ['资源', '状态', '更新'], more('task', tasks, 10).map(t => `<tr${t.status === 'error' ? ' class="is-error"' : ''}><td style="overflow-wrap:anywhere">${esc(t.title)}${t.error ? `<div class="error-line">${esc(t.error)}</div>` : ''}</td><td class="nowrap">${TASKS[t.status] || esc(t.status)}</td><td class="nowrap">${ago(t.updated)}</td></tr>`)) + moreBtn('task', tasks, 10) : '<p class="muted">没有下载任务</p>'}
      <h3 class="m-sub">事件</h3>
      ${table('', ['时间', '范围', '记录'], more('ev', s.events, 12).map(e => `<tr${e.level === 'error' ? ' class="is-error"' : ''}><td class="nowrap">${ago(e.time)}</td><td class="nowrap">${esc(e.scope)}</td><td>${e.historical ? '<span class="muted">历史错误 · </span>' : e.request_failure ? '<span class="muted">接口请求失败 · </span>' : ''}${esc(e.message)}</td></tr>`))}${moreBtn('ev', s.events, 12)}
      <h3 class="m-sub">季度目录</h3>
      <div class="list-rows">${Object.entries(s.catalogs).map(([q, v]) => `<div class="list-row"><span><span class="t">${quarterName(q)}</span><div class="d">${v ? `${v.count} 部 · ${ago(v.success)}` : '未同步'}${v?.error ? ` · <span class="error-line">${esc(v.error)}</span>` : ''}</div></span><button class="btn small" type="button" data-sync="${q}">刷新</button></div>`).join('')}</div>`;
  }

  /* ---------- 季度收录 ---------- */

  async function renderSeason() {
    const s = season = await api('/season');
    const blocked = !s.downloader.ready && !s.enabled;
    const items = s.shows.filter(x => x.auto_download || x.error || x.preview);
    $('#sec-season').innerHTML = `
      <h2 class="sr">季度收录</h2>
      <div class="panel" style="display:grid;gap:var(--s-2);margin-bottom:var(--s-5)">
        <label class="switch" style="justify-content:space-between"><span><b>自动收录每季新番</b><span class="muted" style="display:block;font-size:var(--text-s)">${quarterName(s.start)} 起 · ${esc(s.scope)}</span></span>
          <input type="checkbox" id="season-toggle"${s.enabled ? ' checked' : ''}${blocked ? ' disabled' : ''}><span class="track"></span></label>
        ${s.blocker ? `<p class="warn-line">${esc(s.blocker)}</p>` : ''}
      </div>
      ${items.length ? `<div class="list-rows">${items.map(x => `<div class="list-row"><span><a class="t" href="/show/${x.id}">${esc(x.title)}</a>
        ${x.error ? `<div class="d warn-line">${esc(x.error)}</div>` : ''}${x.preview?.titles?.length ? `<div class="d">${x.preview.titles.slice(0, 2).map(esc).join('<br>')}</div>` : ''}</span>
        <span>${x.cleanup_hold ? '<span class="chip">已清理</span>' : x.error ? '<span class="chip warn">待核查</span>' : x.authorized ? '<span class="chip ok">已开启</span>' : '<span class="chip">关闭</span>'}</span></div>`).join('')}</div>`
        : '<div class="empty"><p>还没有自动收录的作品</p></div>'}`;
  }

  /* ---------- 清理 ---------- */

  async function renderCleanup() {
    const c = await api('/cleanup');
    plans = c.items;
    const ok = plans.filter(p => !p.blocked), blocked = plans.filter(p => p.blocked);
    $('#sec-cleanup').innerHTML = `<h2 class="sr">清理</h2>` + (ok.length ? ok.map(p => {
      const i = plans.indexOf(p);
      return `<article class="plan"><header><a class="t" href="/show/${p.id}"><b>${esc(p.title)}</b></a><span class="chip">${esc(p.reason)}</span><span class="size">${gib(p.bytes)}</span></header>
        <ul>${p.files.map(f => `<li>${esc(f.path)} · ${gib(f.size)}</li>`).join('')}</ul>
        <div class="confirm"><label class="check"><input type="checkbox" data-ok="${i}">已核对清单</label><button class="btn danger" type="button" data-delete="${i}" disabled>删除 ${p.files.length} 个文件</button></div></article>`;
    }).join('') : '<div class="empty"><p>没有待清理的文件</p></div>')
      + (blocked.length ? `<h3 class="m-sub">不能清理</h3><div class="list-rows">${blocked.map(p => `<div class="list-row"><span><a class="t" href="/show/${p.id}">条目 ${p.id}</a><div class="d">${esc(p.blocked)}</div></span></div>`).join('')}</div>` : '');
  }

  /* ---------- 旧片库 ---------- */

  async function renderLegacy() {
    const [t, q] = await Promise.all([api('/transition'), api('/thunder')]);
    const owner = t.legacy_owner?.note;
    $('#sec-legacy').innerHTML = `<h2 class="sr">旧片库</h2>
      <div class="tiles">
        <div class="tile"><span class="k">已盘点文件</span><span class="v num">${t.files}</span><span class="s">有完成记录 ${t.verified}</span></div>
        <div class="tile"><span class="k">接续作品</span><span class="v num">${t.shows.length}</span></div>
        <div class="tile"><span class="k">迅雷队列</span><span class="v num">${q.jobs.length}</span></div>
      </div>
      ${owner ? `<p class="warn-line" style="margin-bottom:var(--s-4)">${esc(owner)}</p>` : ''}
      ${t.shows.length ? `<div class="list-rows">${t.shows.map(x => {
        const done = x.episodes.filter(e => e.status === 'complete').length;
        return `<div class="list-row"><span><a class="t" href="/show/${x.id}">${esc(x.title)}</a><div class="d">已有 ${done} 集${x.unwatched ? ` · 未看 ${x.unwatched}` : ''}${x.missing.length ? ` · <span class="warn-line">缺 ${ranges(x.missing)}</span>` : ''}</div>${x.warning ? `<div class="d warn-line">${esc(x.warning)}</div>` : ''}</span>
          <span>${x.enabled ? '<span class="chip ok">补查中</span>' : '<span class="chip">暂停</span>'}</span></div>`;
      }).join('')}</div>` : '<div class="empty"><p>没有旧片库接续记录</p><a class="btn" href="/library">浏览片库</a></div>'}
      ${q.jobs.length ? `<h3 class="m-sub">迅雷队列</h3>` + table('', ['资源', '操作', '状态'], q.jobs.map(x => `<tr><td style="overflow-wrap:anywhere">${esc(x.title)}${x.error ? `<div class="error-line">${esc(x.error)}</div>` : ''}</td><td class="nowrap">${{ submit: '提交', pause: '暂停', resume: '恢复' }[x.action] || esc(x.action)}</td><td class="nowrap">${esc(x.status === 'intent' ? { pending: '等待执行', claimed: '执行中', review: '待核查' }[x.state] || '等待执行' : TASKS[x.status] || x.status)}</td></tr>`)) : ''}`;
  }

  /* ---------- 设置 ---------- */

  const FAMILIES = [['screen', '场次', '暗场里的一张票，下一集最醒目'], ['line', '线路', '每部番一条线，每集一站'], ['grid', '格子', '每集一格，勾了就是看了']];
  const DEMO = {
    screen: `<rect x="0" y="6" width="150" height="52" style="fill:var(--layer-1)"/><rect x="0" y="6" width="36" height="52" style="fill:var(--layer-2)"/><rect x="44" y="18" width="60" height="7" style="fill:var(--ink)"/><rect x="44" y="32" width="36" height="5" style="fill:var(--muted)"/><rect x="164" y="6" width="56" height="52" style="fill:var(--signal)"/><text x="192" y="42" text-anchor="middle" style="fill:var(--on-signal);font:700 26px var(--num)">10</text>`,
    line: [0, 1, 2, 3, 4, 5, 6].map(i => `${i ? `<line x1="${14 + (i - 1) * 32}" y1="32" x2="${14 + i * 32}" y2="32" style="stroke:var(--l${i < 4 ? 1 : 6},var(--ink));stroke-width:2${i > 4 ? ';stroke-dasharray:3 3' : ''}"/>` : ''}<circle cx="${14 + i * 32}" cy="32" r="10" style="fill:${i < 4 ? 'var(--l1)' : 'var(--layer-1)'};stroke:${i < 5 ? 'var(--l1)' : 'var(--line-strong)'};stroke-width:2${i > 4 ? ';stroke-dasharray:3 3' : ''}"/>`).join(''),
    grid: [0, 1, 2, 3, 4, 5, 6].map(i => `<rect x="${2 + i * 31}" y="17" width="30" height="30" style="fill:${i === 4 ? 'var(--signal)' : 'var(--layer-1)'};stroke:var(--line)"/>${i < 4 ? `<path d="M${10 + i * 31} 32l5 5 9-10" style="fill:none;stroke:var(--signal);stroke-width:2"/>` : ''}`).join(''),
  };

  function renderSettings() {
    const st = App.settings, th = fanyuTheme.get();
    const res = [['1080', '1080p'], ['720', '720p'], ['2160', '2160p'], ['', '不限']], sub = [['any_zh', '简繁均可'], ['chs', '简体'], ['cht', '繁体']];
    const opt = (list, v) => list.map(([k, n]) => `<option value="${k}"${k === v ? ' selected' : ''}>${n}</option>`).join('');
    $('#sec-settings').innerHTML = `
      <h2 class="m-sub" style="margin-top:0">外观</h2>
      <div class="theme-cards" role="group" aria-label="主题">${FAMILIES.map(([k, n, d]) => `<button type="button" class="theme-card" data-family="${k}" data-mode="${document.documentElement.dataset.mode}" data-pick="${k}" aria-pressed="${th.family === k}">
        <svg class="demo" viewBox="0 0 224 64" aria-hidden="true">${DEMO[k]}</svg><b>${n}</b></button>`).join('')}</div>
      <div style="margin-top:var(--s-4)"><div class="seg" role="group" aria-label="明暗">${[['auto', '跟随系统'], ['light', '亮'], ['dark', '暗']].map(([k, n]) => `<button type="button" data-mode-pick="${k}" aria-pressed="${th.mode === k}">${n}</button>`).join('')}</div></div>

      <h2 class="m-sub" style="margin-top:var(--s-7)">下载偏好</h2>
      <form id="prefs" class="form-grid" style="max-width:640px">
        <label class="field"><span>分辨率</span><select name="resolution">${opt(res, st.resolution)}</select></label>
        <label class="field"><span>字幕</span><select name="subtitle">${opt(sub, st.subtitle)}</select></label>
        <label class="field wide"><span>字幕组</span><input name="groups" placeholder="全部" value="${esc((st.groups || []).join(', '))}"></label>
        <label class="field"><span>检查间隔（分钟）</span><input name="poll_minutes" type="number" min="1" max="1440" value="${Math.round(st.poll_seconds / 60)}"></label>
        <label class="field"><span>弹弹play 端口</span><input name="dandan_port" type="number" min="1" max="65535" value="${st.dandan_port}"></label>
        <div class="wide"><button class="btn primary" type="submit">保存</button></div>
      </form>

      <h2 class="m-sub" style="margin-top:var(--s-7)">设备</h2>
      ${App.local ? `<div class="links"><button class="btn" type="button" id="pair">生成配对码</button><button class="btn danger" type="button" id="revoke">退出所有外部设备</button></div><div id="pair-out" style="margin-top:var(--s-3)"></div>`
        : `<div class="links"><button class="btn" type="button" id="logout">退出这台设备</button></div>`}
      ${App.local ? `<dl class="kv" style="margin-top:var(--s-6)"><dt>弹弹play</dt><dd>${esc(st.dandan_exe)}</dd></dl>` : ''}`;
  }

  /* ---------- routing ---------- */

  async function show(sec) {
    if (!SECTIONS.includes(sec)) sec = 'status';
    $$('.tabs-local a').forEach(a => a.toggleAttribute('aria-current', a.dataset.sec === sec));
    $$('.m-section').forEach(s => { s.hidden = s.dataset.sec !== sec; });
    const el = $('#sec-' + sec);
    try {
      if (sec === 'status') await renderStatus();
      if (sec === 'season') await renderSeason();
      if (sec === 'cleanup') await renderCleanup();
      if (sec === 'legacy') await renderLegacy();
      if (sec === 'settings') renderSettings();
      loaded[sec] = true;
    } catch (e) {
      el.innerHTML = `<div class="empty"><h2>读取失败</h2><p>${esc(e.message)}</p><button class="btn" type="button" data-reload="${sec}">重试</button></div>`;
    }
  }
  const current = () => location.hash.slice(1) || 'status';

  document.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.reload) return show(b.dataset.reload);
    if (b.dataset.all) { showAll[b.dataset.all] = !showAll[b.dataset.all]; return renderStatus(); }
    if (b.dataset.sync) return act(b, async () => { await api('/catalog/sync', { quarters: [b.dataset.sync] }); toast('正在刷新目录'); });
    if (b.dataset.pick) { fanyuTheme.set({ family: b.dataset.pick }); return renderSettings(); }
    if (b.dataset.modePick) { fanyuTheme.set({ mode: b.dataset.modePick }); return renderSettings(); }
    if (b.dataset.delete) return act(b, async () => {
      const p = plans[Number(b.dataset.delete)];
      await api(`/cleanup/${p.id}/confirm`, { fingerprint: p.fingerprint, confirm: '删除清单内文件' });
      toast(`已删除 ${p.title} 的 ${p.files.length} 个文件`);
      await renderCleanup();
    });
    switch (b.id) {
      case 'check': return act(b, async () => { const r = await api('/check', {}); toast(r.blocker ? '已请求检查 · 下载受阻' : '已请求检查新集'); await renderStatus(); });
      case 'probe': return act(b, async () => { const r = await api('/player/probe', {}); toast(r.ok ? '弹弹play 接口正常' : r.error); });
      case 'pair': return act(b, async () => {
        const r = await api('/access/pair', {});
        $('#pair-out').innerHTML = `<div class="panel" style="display:inline-grid;gap:4px"><span class="pair-code">${esc(r.code)}</span><span class="muted">10 分钟内有效，只能用一次</span></div>`;
      });
      case 'revoke': return act(b, async () => { await api('/access/revoke', {}); toast('所有外部设备已退出'); });
      case 'logout': return act(b, async () => { await api('/access/logout', {}); location.assign('/login'); });
    }
  });
  document.addEventListener('change', e => {
    if (e.target.dataset.ok) $(`[data-delete="${e.target.dataset.ok}"]`).disabled = !e.target.checked;
    if (e.target.id === 'season-toggle') {
      const box = e.target;
      act(box, async () => {
        try { await api('/season', { enabled: box.checked }); } catch (err) { box.checked = !box.checked; throw err; }
        toast(box.checked ? '已开启季度自动收录' : '已停止收录新作品');
        await renderSeason();
      });
    }
  });
  document.addEventListener('submit', e => {
    if (e.target.id !== 'prefs') return;
    e.preventDefault();
    const f = e.target, d = Object.fromEntries(new FormData(f));
    act(f.querySelector('[type=submit]'), async () => {
      const body = { resolution: d.resolution, subtitle: d.subtitle, groups: d.groups.split(/[,，]/).map(x => x.trim()).filter(Boolean), poll_seconds: Math.round(Number(d.poll_minutes) * 60), dandan_port: Number(d.dandan_port) };
      await api('/settings', body);
      Object.assign(App.settings, body);
      toast('已保存');
    });
  });
  addEventListener('hashchange', () => show(current()));
  $('#health').addEventListener('click', () => {
    if (current() === 'status') renderStatus();
  });
  addEventListener('fanyu:status', e => {
    if (current() === 'status' && loaded.status) renderStatus(e.detail);
  });
  addEventListener('fanyu:theme', () => { if (current() === 'settings') renderSettings(); });
  boot().then(() => show(current())).catch(e => toast(e.message));
})();
