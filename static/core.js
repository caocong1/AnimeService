/* Shared runtime for every page: API, shell, formatting, covers, episode strips. */
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const https = s => /^https:\/\//.test(s || '') ? esc(s) : '';

const STATES = { wish: '想看', trial: '试看', watching: '在追', paused: '暂搁', dropped: '弃番', completed: '看完' };
const TASKS = {
  intent: '待提交', uncertain: '等待确认', downloading: '下载中', complete: '已完成', held: '已停止',
  review: '待核查', pending: '等待迅雷', legacy_unverified: '旧文件待核验', file_missing: '文件缺失',
  error: '错误', cleaned: '已清理',
};

const App = { token: '', local: true, settings: {}, quarters: [], ready: null };

/* ---------- API ---------- */

const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await nativeFetch(...args);
  if (r.status === 401 && location.pathname !== '/login') location.assign('/login');
  return r;
};

async function api(path, body) {
  const init = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Anime-Token': App.token },
    body: JSON.stringify(body),
  };
  const res = await fetch('/api' + path, init);
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw Error(data.error || data.detail || '操作失败（' + res.status + '）');
  return data;
}

function boot() {
  App.ready ??= api('/bootstrap').then(b => {
    Object.assign(App, { token: b.token, local: b.local, settings: b.settings, quarters: b.quarters || [] });
    return App;
  });
  return App.ready;
}

/* ---------- Formatting ---------- */

const pad2 = n => String(n).padStart(2, '0');
function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}
function ago(t) {
  if (!t) return '—';
  const d = new Date(t * 1000), now = new Date(), diff = (now - d) / 1000;
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(now) - day(d)) / 86400000);
  if (days === 0) return '今天 ' + hm;
  if (days === 1) return '昨天 ' + hm;
  return (d.getFullYear() === now.getFullYear() ? '' : d.getFullYear() + '-') + `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`;
}
const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
function airDate(s, withWeek = true) {
  if (!/^\d{4}-\d{2}-\d{2}/.test(s || '')) return '';
  const d = new Date(s.slice(0, 10) + 'T00:00:00');
  return s.slice(5, 10) + (withWeek ? ' ' + WEEK[d.getDay()] : '');
}
const today = () => new Date().toISOString().slice(0, 10);
const gib = b => b >= 1024 ** 3 ? (b / 1024 ** 3).toFixed(b >= 10 * 1024 ** 3 ? 0 : 1) + ' GB' : Math.max(1, Math.round(b / 1024 ** 2)) + ' MB';
function quarterName(q) {
  const [y, m] = q.split('-');
  return `${y} 年 ${Number(m)} 月`;
}

/* ---------- Icons (one stroke family, 24 grid, 1.75 stroke) ---------- */

const ICONS = {
  home: '<path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M9.5 20v-6h5v6"/>',
  season: '<rect x="4" y="5" width="16" height="15" rx="1"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  library: '<path d="M5 4v16M10 4v16M15 5l4.5 14.5"/>',
  manage: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  back: '<path d="M14.5 5.5 8 12l6.5 6.5"/>',
  next: '<path d="M6 5.5v13L15 12z" fill="currentColor" stroke="none"/><path d="M18 5.5v13"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  chevron: '<path d="m9 5.5 6.5 6.5L9 18.5"/>',
  down: '<path d="m5.5 9 6.5 6.5L18.5 9"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  desktop: '<rect x="3.5" y="4.5" width="17" height="11" rx="1"/><path d="M9 20h6M12 15.5V20"/>',
  external: '<path d="M14 4.5h5.5V10M19.5 4.5 11 13M17 14v5.5H4.5V7H10"/>',
};
const icon = (k, cls = '') => `<svg class="i ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[k]}</svg>`;

/* ---------- Shell ---------- */

const NAV_HEALTH = '/manage#status';
const NAV = [['home', '/', '追番'], ['season', '/season', '新番'], ['library', '/library', '片库'], ['manage', '/manage', '后台']];

function shell(active) {
  const links = cls => NAV.map(([k, href, label]) =>
    `<a class="${cls}" href="${href}" data-nav="${k}"${k === active ? ' aria-current="page"' : ''}>${icon(k)}<span>${label}</span></a>`).join('');
  const bar = document.createElement('header');
  bar.className = 'bar';
  bar.innerHTML = `<div class="bar-in"><a class="brand" href="/" aria-label="番屿 首页"><img src="/static/icon.svg" alt="" width="28" height="28"><span>番屿</span></a>
    <nav class="nav" aria-label="主导航">${links('nav-link')}</nav>
    <div class="bar-end"><a class="health" id="health" href="${NAV_HEALTH}" role="status"><i></i><span>连接中</span></a>
    <button class="icon-btn" id="check-now" type="button" aria-label="检查新集" title="检查新集">${icon('refresh')}</button></div></div>`;
  const tabs = document.createElement('nav');
  tabs.className = 'tabbar';
  tabs.setAttribute('aria-label', '主导航');
  tabs.innerHTML = links('tab');
  const skip = document.createElement('a');
  skip.className = 'skip'; skip.href = '#main'; skip.textContent = '跳到主要内容';
  document.body.prepend(skip, bar);
  document.body.append(tabs);
  const toastEl = document.createElement('div');
  toastEl.className = 'toast'; toastEl.id = 'toast'; toastEl.setAttribute('role', 'status');
  document.body.append(toastEl);
  $('#check-now').addEventListener('click', e => act(e.currentTarget, async () => {
    const r = await api('/check', {});
    toast(r.blocker ? '已请求检查 · 下载受阻：' + r.blocker : '已请求检查新集');
  }));
  boot().then(() => { health(); setInterval(health, 60000); }).catch(e => toast(e.message));
}

async function health() {
  const el = $('#health');
  if (!el) return;
  try {
    const s = await api('/status');
    const alive = s.heartbeat && Date.now() / 1000 - s.heartbeat.time < 240;
    const blocked = s.downloader?.blocker;
    const err = s.events.some(e => e.level === 'error' && Date.now() / 1000 - e.time < 3600);
    el.dataset.state = !alive ? 'down' : blocked || err ? 'warn' : 'ok';
    el.lastElementChild.textContent = !alive ? '后台未响应' : blocked ? '下载受阻' : err ? '有错误' : '后台正常';
    el.title = alive ? '心跳 ' + ago(s.heartbeat.time) : '';
    dispatchEvent(new CustomEvent('fanyu:status', { detail: s }));
  } catch {
    el.dataset.state = 'down';
    el.lastElementChild.textContent = '连接中断';
  }
}

let toastTimer;
function toast(msg, action) {
  const el = $('#toast');
  if (!el) return;
  el.innerHTML = `<span>${esc(msg)}</span>` + (action ? `<button type="button" class="link">${esc(action.label)}</button>` : '');
  if (action) el.querySelector('button').onclick = () => { el.classList.remove('on'); action.run(); };
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), action ? 7000 : 4000);
}

/* Runs an async action with the control busy; errors go to the toast. */
async function act(control, fn) {
  if (control) { control.disabled = true; control.setAttribute('aria-busy', 'true'); }
  try { return await fn(); } catch (e) { toast(e.message); } finally {
    if (control) { control.disabled = false; control.removeAttribute('aria-busy'); }
  }
}

/* ---------- Covers ---------- */

function cover(src, title, cls = '', href = '') {
  const initial = esc(String(title || '番').trim().slice(0, 1));
  const url = https(src);
  const tag = href ? 'a' : 'span';
  const attrs = href ? ` href="${href}" tabindex="-1" aria-hidden="true"` : '';
  return `<${tag} class="cover ${cls}${url ? '' : ' no-img'}" data-initial="${initial}"${attrs}>${url ? `<img src="${url}" alt="" loading="lazy" decoding="async">` : ''}</${tag}>`;
}
const lineColour = id => `--lc:var(--l${id % 8 + 1})`;
document.addEventListener('error', e => {
  if (e.target.tagName === 'IMG' && e.target.parentElement?.classList.contains('cover')) {
    e.target.parentElement.classList.add('no-img');
    e.target.remove();
  }
}, true);

/* ---------- Episodes ---------- */

const EP_LABEL = { watched: '已看', resume: '续播', ready: '可播放', downloading: '下载中', missing: '缺集', aired: '已播出', future: '未播出' };
const watchHref = e => `/watch?media=${e.media}${e.s === 'resume' ? '&resume=1' : ''}`;

const coarse = matchMedia('(pointer: coarse)').matches;
/* One DOM for every family; tokens and CSS decide how it reads (ticket chips, station line, tick grid). */
function epStrip(show) {
  const eps = show.eps || [];
  if (!eps.length) return '';
  const next = show.next?.n;
  let run = 0;
  while (run < eps.length && eps[run].s === 'watched') run++;
  let after = 0, future = 0;
  const items = eps.map((e, i) => {
    const rel = next == null ? '' : e.n < next ? 'before' : e.n === next ? 'next' : 'after';
    const cls = [i < run && run > 3 ? 'in-run' : '', rel === 'after' && ++after <= 4 ? 'soon' : '', e.s === 'future' && ++future > 2 ? 'far' : ''].join(' ').trim();
    const extra = e.s === 'downloading' ? ` ${e.progress}%` : e.s === 'future' && e.date ? ' ' + airDate(e.date, false) : '';
    const label = `第 ${e.n} 集 ${EP_LABEL[e.s]}${extra}`;
    const style = e.s === 'downloading' ? ` style="--p:${e.progress}%"` : e.s === 'resume' && e.duration ? ` style="--p:${Math.round(e.position / e.duration * 100)}%"` : '';
    const inner = `<span class="ep-n">${e.n}</span><span class="ep-note">${e.s === 'missing' ? '缺' : e.s === 'downloading' ? e.progress + '%' : e.s === 'future' && e.date ? airDate(e.date, false) : ''}</span>`;
    const hit = e.media && !coarse
      ? `<a class="ep-hit" href="${watchHref(e)}" aria-label="${label}" title="${label}">${inner}</a>`
      : `<span class="ep-hit" role="img" aria-label="${label}" title="${label}">${inner}</span>`;
    return `<li class="ep ${cls}" data-s="${e.s}" data-rel="${rel}"${style}>${hit}</li>`;
  });
  const runItem = run > 3 ? `<li class="ep run" data-s="watched"><span class="ep-hit" role="img" aria-label="第 1 至 ${run} 集 已看"><span class="ep-n">1–${run}</span></span></li>` : '';
  const long = eps.length > 16 ? ' data-long' : '';
  return `<ol class="eps"${long} aria-label="分集">${runItem}${items.join('')}</ol>`;
}

/* [1,2,3,5] -> "1–3、5" */
function ranges(ns) {
  const out = [];
  for (let i = 0; i < ns.length; i++) {
    let j = i;
    while (j + 1 < ns.length && ns[j + 1] === ns[j] + 1) j++;
    out.push(j > i ? `${ns[i]}–${ns[j]}` : `${ns[i]}`);
    i = j;
  }
  return out.join('、') + ' 集';
}

function ticket(show, big = true) {
  const e = show.next;
  if (!e) return '';
  const resume = e.s === 'resume';
  return `<a class="ticket${big ? '' : ' small'}" href="${watchHref(e)}" data-s="${e.s}" aria-label="${esc(show.title)} 第 ${e.n} 集 ${resume ? '续播' : '播放'}">
    <span class="t-ep"><small>第</small><b>${e.n}</b><small>集</small></span>
    <span class="t-act">${icon('play')}<span class="t-verb">${resume ? '续播' : '播放'}</span>${resume ? `<span class="t-time">${clock(e.position)}</span>` : ''}</span></a>`;
}

function stateChip(s) {
  return s.selected ? `<span class="chip" data-state="${s.state}">${STATES[s.state]}</span>` : '';
}
