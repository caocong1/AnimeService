'use strict';
/* 放映室: the picture first; mark watched and go to the next episode without leaving. */
(() => {
  shell('');
  const params = new URLSearchParams(location.search), releaseId = params.get('release');
  const id = releaseId ? 'finished:' + releaseId : params.get('media');
  if (!id) return location.replace('/library');
  const finishedEndpoint = releaseId ? '/api/finished-viewing/' + encodeURIComponent(releaseId) : null;
  let releases = [], sourceBoard = null, sequence = 0, progressQueue = Promise.resolve(), moved = false;
  let sourceOnly = false;
  let alignmentSites = ['bilibili'];
  const tracking = text => { $('#tracking-status').textContent = text; };
  // Paused/background pages also keep the old media/build alive during publication.
  const viewingSession = crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(24)), x => x.toString(16).padStart(2, '0')).join('');
  let viewingLeaseTimer = null;
  const viewingLease = (closed = false) => media?.viewing_session_supported ? fetch(finishedEndpoint ? finishedEndpoint + '/viewing-session' : `/api/web/media/${id}/viewing-session`, {
    method: 'POST', keepalive: closed,
    headers: { 'Content-Type': 'application/json', 'X-Anime-Token': App.token },
    body: JSON.stringify({ session: viewingSession, closed }),
  }).catch(() => {}) : Promise.resolve();
  window.addEventListener('pagehide', () => { clearInterval(viewingLeaseTimer); viewingLease(true); });
  window.addEventListener('pageshow', () => {
    clearInterval(viewingLeaseTimer);
    viewingLease(); viewingLeaseTimer = setInterval(viewingLease, 30000);
  });
  let played = false, art = null, media = null, show = null, lastPosition = 0, lastSave = 0, selected = [], comments = [], activeDanmu = null, danmuRev = 0, tracks = [];
  let searchRev = 0;
  let detailRev = 0;
  let resultRows = [], resultContext = '本集推荐';
  const sourceInfoCache = new Map();
  let infoTimer = null, infoBusy = false;
  let replaceDanmu = null;
  let audioTracks = [], audioFingerprint = '', audioCurrent = '', audioRevision = 0, audioSwitching = false, initialMetadata = true;
  let audioCopySupported = false;
  const audioStorageKey = `fanyu-audio-track:${id}`;
  const sourceStorageKey = `fanyu-danmu-sources:${id}`;
  const removedStorageKey = `fanyu-danmu-removed:${id}`;
  const removedSources = new Set(Array.isArray(readSaved(removedStorageKey)) ? readSaved(removedStorageKey) : []);
  function saveRemoved() { try { localStorage.setItem(removedStorageKey, JSON.stringify([...removedSources])); } catch (_) {} }
  // Display preferences follow the viewer across episodes; sources/timing stay per media.
  const playerStorageKey = 'fanyu-danmu-player';
  const blockedStorageKey = 'fanyu-danmu-blocked-users';
  const blocked = new Map(DanmuTiming.blockedUsers(readSaved(blockedStorageKey)).map(({id, ...u}) => [id, u]));
  const onScreen = new Set();
  let display = DanmuTiming.playerSettings(readSaved(playerStorageKey) || readSaved(`fanyu-danmu-player:${id}`) || {}), listRows = [], followUntil = 0, menuTarget = null;
  function readSaved(key) {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return null; }
  }
  function saveSources() {
    try { localStorage.setItem(sourceStorageKey, JSON.stringify(DanmuTiming.savedSources(selected))); }
    catch (_) { toast('本次来源已生效，但浏览器未能保存，刷新后可能丢失'); }
  }
  const danmuRetry = DanmuRetry.create();
  const web = (path, body) => {
    const run = () => api('/web' + path, body);
    if (!(path.startsWith('/danmu/') || path === `/media/${id}/danmu`)) return run();
    const revision = [danmuRev, detailRev, searchRev];
    return danmuRetry.request(run, {
      current: () => revision[0] === danmuRev && revision[1] === detailRev && revision[2] === searchRev,
      onRetry: ms => {
        if (ms === null) return;
        const message = `临时失败 · ${ms / 1000} 秒后自动重试`;
        if (path === `/media/${id}/danmu`) $('#danmu-note').textContent = message;
        else if (path.startsWith('/danmu/show/') || path.startsWith('/danmu/resolve')) $('#danmu-episodes').innerHTML = `<p class="dm-empty" role="status">${message}</p>`;
        else if (path.startsWith('/danmu/search')) $('#danmu-results-note').textContent = message;
      },
    });
  };
  window.addEventListener('pagehide', () => danmuRetry.close());
  window.addEventListener('pageshow', () => danmuRetry.resume());
  const alignment = DanmuAlignment.create({
    request: (path, body) => web(`/media/${id}/alignment${path}`, body),
    present: source => selected.includes(source), save: saveTiming,
    render: () => { if (!document.activeElement?.matches('#danmu-selected input')) renderSelected(); }, apply: applyDanmu,
  });
  function alignSource(source, options) {
    if (sourceOnly) return;
    if (alignmentSites.includes(DanmuAlignment.sourceSite(source)) && source.count > 0) alignment.start(source, options);
  }
  window.addEventListener('pagehide', () => selected.forEach(source => alignment.cancel(source)));

  function screenState(html) {
    $('#screen-state').innerHTML = html;
    $('#screen-state').hidden = !html;
  }

  /* ---------- episodes of this show (or files of this folder) ---------- */

  const resumable = () => { const p = media?.progress; return !media?.finished && !!p && p.position > 5 && p.position < (p.duration || 0) - 30; };
  const isCurrent = e => releaseId ? e.n === media?.episode : e.media === id;
  const current = () => show?.board.eps.find(isCurrent);
  const nextEp = () => { const c = current(); return c && show.board.eps.find(e => e.n > c.n && (e.media || e.release)); };

  function renderSources() {
    const row = sourceBoard?.eps.find(e => e.n === media.episode), options = [];
    if (row?.media) options.push({url:'/watch?media=' + row.media, label:'原视频', active:!releaseId});
    const versions = releases.filter(r => r.show_id === media.show_id && r.season === Number(show.show.mapping?.season || 1) && r.episode === media.episode).sort((a,b) => FinishedReleases.compare(b,a));
    for (const r of versions) options.push({url:r.player_url,label:'自制字幕 · ' + FinishedReleases.label(r),active:r.id === releaseId});
    $('#playback-source-row').hidden = !options.length;
    $('#playback-source').innerHTML = options.map(o => `<option value="${esc(o.url)}"${o.active ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
  }

  function renderActs() {
    const c = current(), n = nextEp(), pos = media?.progress?.position || 0;
    const parts = [];
    if (c) parts.push(c.s === 'watched'
      ? `<button class="btn" type="button" data-mark="false">${icon('check')}已看</button>`
      : `<button class="btn" type="button" data-mark="true">${icon('check')}标记看完</button>`);
    if (n) parts.push(`<a class="btn primary" href="${watchHref(n)}">${icon('next')}第 ${n.n} 集</a>`);
    if (resumable()) parts.push(`<button class="btn quiet" type="button" id="restart">从头播放</button>`);
    if (App.local && !sourceOnly) parts.push(`<button class="btn quiet" type="button" id="desktop" title="用本机播放器打开">${icon('desktop')}<span>本机播放器</span></button>`);
    $('#acts').innerHTML = parts.join('');
  }

  function renderList(items) {
    $('#tab-playlist').textContent = show ? '分集' : '同一文件夹';
    $('#playlist').innerHTML = items;
    const list = $('#playlist'), here = $('#playlist [aria-current]');
    if (here && list.scrollHeight > list.clientHeight) list.scrollTop = here.offsetTop - list.clientHeight / 2;
  }

  async function loadShow() {
    show = await api('/shows/' + media.show_id);
    sourceBoard = show.board;
    show.board = FinishedReleases.applyBoard(show.show, sourceBoard, releases);
    if (releaseId) media.finished = current()?.s === 'watched';
    renderSources();
    $('#parent').innerHTML = icon('back') + esc(show.show.title);
    $('#parent').href = '/show/' + show.show.id;
    const c = current();
    $('#title').textContent = c ? `第 ${c.n} 集` : media.name;
    document.title = `${show.show.title} 第 ${c?.n ?? media.episode} 集 · 番屿`;
    renderActs();
    const eps = show.board.eps.filter(e => e.s !== 'future');
    let run = 0;
    while (run < eps.length && eps[run].s === 'watched' && !isCurrent(eps[run])) run++;
    const row = e => {
      const here = isCurrent(e), seen = e.s === 'watched';
      const sub = here ? '正在播放' : e.s === 'resume' ? '看到 ' + clock(e.position) : e.s === 'downloading' ? `下载中 ${e.progress}%` : EP_LABEL[e.s];
      const body = `<b>第 ${e.n} 集</b><span>${sub}</span>`;
      const mark = seen
        ? `<button class="btn small quiet mark" type="button" data-ep="${e.n}" data-finished="false" aria-pressed="true" aria-label="第 ${e.n} 集已看，改为未看" title="改为未看">${icon('check')}</button>`
        : `<button class="btn small mark" type="button" data-ep="${e.n}" data-finished="true" aria-label="第 ${e.n} 集标记看完">看完</button>`;
      return `<div class="pl-item" data-s="${e.s}"${here ? ' aria-current="true"' : ''}><span class="no">${here ? icon('play') : ''}</span>
        ${(e.media || e.release) && !here ? `<a class="go" href="${watchHref(e)}">${body}</a>` : `<span class="go">${body}</span>`}${mark}</div>`;
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
    await progressQueue;
    const r = await api(releaseId && n === media.episode ? `/finished-viewing/${encodeURIComponent(releaseId)}/watch` : `/shows/${show.show.id}/watch`, { episode: n, finished });
    try { releases = await FinishedReleases.load(); }
    catch (e) {
      releases = releases.map(r => r.show_id === media.show_id && r.episode === n ? {...r,finished} : r);
      tracking(e.message);
    }
    await loadShow();
    const nx = nextEp();
    toast(`第 ${n} 集${finished ? '已看' : '改为未看'}${r.state ? ' · 已设为' + STATES[r.state] : ''}`, finished && nx && current()?.n === n ? { label: `播放第 ${nx.n} 集`, run: () => location.assign(watchHref(nx)) } : null);
  }

  /* ---------- player ---------- */

  /* Only real playback moves the stored position; opening or leaving the page never does. */
  async function saveProgress(keepalive = false) {
    if (audioSwitching || !played || !art || !Number.isFinite(art.duration) || art.duration <= 0) return;
    if (finishedEndpoint) {
      const body = {position:art.currentTime,duration:art.duration,playing:moved,session:viewingSession,sequence:++sequence};
      moved = false;
      const run = async () => {
        try {
          const response = await fetch(finishedEndpoint + '/progress', {method:'POST',keepalive,headers:{'Content-Type':'application/json','X-Anime-Token':App.token},body:JSON.stringify(body)});
          if (!response.ok) throw Error('观看记录未同步，请稍后重试');
          tracking('续播进度已同步 · ' + clock(body.position));
        } catch (e) { moved ||= body.playing; tracking(e.message); }
      };
      if (keepalive) return run();
      progressQueue = progressQueue.then(run); return progressQueue;
    }
    const delta = art.currentTime - lastPosition, playing = !art.video.paused && delta > 0 && delta < 35;
    lastPosition = art.currentTime;
    await fetch(`/api/web/media/${id}/progress`, {
      method: 'POST', keepalive,
      headers: { 'Content-Type': 'application/json', 'X-Anime-Token': App.token },
      body: JSON.stringify({ position: art.currentTime, duration: art.duration, playing }),
    });
  }

  function desktopButton() {
    return App.local && !sourceOnly ? `<button class="btn" type="button" id="desktop">${icon('desktop')}用本机播放器打开</button>` : '';
  }
  function playbackFailure() {
    const compatible = !audioCurrent && audioCopySupported && audioTracks.length;
    screenState(`<b>浏览器无法播放这个文件</b>${compatible ? '<button class="btn" type="button" id="compat-playback">原码流兼容播放</button><span>只换封装，视频不转码；浏览器仍须支持原编码。</span>' : ''}${desktopButton()}`);
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
      url: releaseId ? `/static/finished-media/${encodeURIComponent(releaseId)}/episode.mp4` : `/api/web/media/${id}/stream`,
      type: media.name.toLowerCase().endsWith('.mp4') ? 'mp4' : 'mkv',
      volume: 0.7, theme: accent, lang: 'zh-cn', setting: true, playbackRate: true, fullscreen: true, fullscreenWeb: true, hotkey: false, gesture: false,
      subtitle: { escape: true, style: { color: '#fff', fontSize: 'clamp(16px, 2.2vw, 28px)', textShadow: '0 1px 3px #000, 1px 0 2px #000' } },
      plugins: [artplayerPluginDanmuku({ danmuku: [], emitter: false, ...DISPLAY_DEFAULTS, antiOverlap: true, margin: [10, '25%'], ...pluginDisplay(), beforeEmit: () => false, beforeVisible: showDanmu })],
    });
    art.on('artplayerPluginDanmuku:config', options => { saveDisplay(options); renderDisplay(); });
    for (const [event, visible] of [['show',true],['hide',false]]) art.on(`artplayerPluginDanmuku:${event}`, () => saveDisplay({visible}));
    // Persist legacy per-media preferences before loading comments emits config events.
    saveDisplay({});
    art.template.$player.addEventListener('contextmenu', e => {
      const hit = danmuAt(e.clientX, e.clientY);
      if (!hit) return closeMenu();
      e.preventDefault(); e.stopPropagation(); openMenu(hit, e.clientX, e.clientY);
    }, true);
    art.controls.add({ name: 'shot', position: 'right', index: 20, html: art.icons.screenshot, tooltip: '截图', click: screenshot });
    if (!sourceOnly) {
      const clip = ClipExport.mount({id, player: () => art, audio: () => audioCurrent, request: (path, body) => web(path, body)});
      art.on('destroy', () => clip.close());
    } else { $('#open-clip').disabled = true; $('#open-clip').title = '片段工具暂不可用'; }
    const picture = PictureQuality.mount(art, {request: () => sourceOnly
      ? Promise.resolve({width:media.release.width,height:media.release.height,dynamic_range:'unknown'})
      : web(`/media/${id}/picture`),
      select: $('#picture-mode'), note: $('#picture-note'), details: $('#picture-details')});
    art.on('destroy', () => picture.close());
    renderDisplay();
    $('#screen').tabIndex=0;
    $('#screen').setAttribute('aria-label','视频播放器；空格播放或暂停，左右方向键或左右滑动快退快进，上下方向键增减音量，全屏时可用鼠标滚轮增减音量，每次5%，回车切换全屏');
    const adjustVolume = step => {
      art.volume = Math.max(0, Math.min(1, Math.round((art.volume + step) * 100) / 100));
      art.notice.show = `音量 ${Math.round(art.volume * 100)}%`;
    };
    // Only fullscreen owns wheel volume; inline playback must leave page scrolling alone.
    art.template.$player.addEventListener('wheel', e => {
      if (!art.fullscreen && !art.fullscreenWeb) return;
      if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
      if (!Number.isFinite(e.deltaY) || !e.deltaY || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      if (e.target.closest('input, textarea, select, button, [contenteditable], .art-settings, .art-contextmenus, #danmu-menu')) return;
      e.preventDefault();
      adjustVolume(e.deltaY < 0 ? 0.05 : -0.05);
    }, {passive: false});
    // Page-wide hotkeys, unless focus sits in a control that owns these keys (buttons, fields, the danmu list, dialogs).
    document.addEventListener('keydown', e => {
      const t = e.target;
      if(t!==document.body && !$('#screen').contains(t) && !art.template.$player.contains(t)) return;
      if(t.closest('input, textarea, select, button, [contenteditable], #danmu-menu')) return;
      if(e.isComposing || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
      if(e.code==='Space') { e.preventDefault(); if(!e.repeat) art.toggle(); }
      if(e.key==='Enter') { e.preventDefault(); if(!e.repeat) art.fullscreen=!art.fullscreen; }
      if(e.key==='ArrowLeft' || e.key==='ArrowRight') { e.preventDefault(); art.currentTime=Math.max(0,Math.min(art.duration,art.currentTime+(e.key==='ArrowRight'?5:-5))); }
      if(e.key==='ArrowUp' || e.key==='ArrowDown') { e.preventDefault(); adjustVolume(e.key==='ArrowUp'?0.05:-0.05); }
    });
    // Horizontal swipe on the video seeks in the arrow keys' 5 s steps; a full player width is 60 s. Seeks once, on release.
    let swipe = null;
    const $video = art.template.$video, clock = Artplayer.utils.secondToTime;
    $video.addEventListener('touchstart', e => {
      const t = e.touches[0];
      swipe = e.touches.length===1 && art.duration ? { x: t.pageX, y: t.pageY, from: art.currentTime, on: false } : null;
    }, {passive: true});
    $video.addEventListener('touchmove', e => {
      if(!swipe || e.touches.length!==1) { swipe = null; return; }
      const t = e.touches[0], dx = t.pageX-swipe.x, dy = t.pageY-swipe.y;
      if(!swipe.on) {
        if(Math.hypot(dx, dy) < 12) return;
        if(Math.abs(dx) < Math.abs(dy)) { swipe = null; return; }
        swipe.on = true;
      }
      const step = Math.round(dx/art.width*12)*5;
      swipe.to = Math.max(0, Math.min(art.duration, swipe.from+step));
      art.notice.show = `${step>0?'+':''}${step} 秒　${clock(swipe.to)} / ${clock(art.duration)}`;
    }, {passive: true});
    $video.addEventListener('touchend', () => { if(swipe?.on) art.currentTime = swipe.to; swipe = null; });
    $video.addEventListener('touchcancel', () => { swipe = null; });
    art.on('video:loadedmetadata', () => {
      if (!initialMetadata || audioSwitching) return;
      initialMetadata = false;
      applyDanmu().catch(() => {});
      if (resumable()) { art.currentTime = media.progress.position; lastPosition = art.currentTime; }
      // If audible autoplay is blocked, wait for the viewer to press play without muting.
      art.play().catch(() => {});
    });
    art.on('video:playing', () => { if (!releaseId) played = true; });
    art.on('video:seeked', () => { if (releaseId) lastPosition = art.currentTime; });
    art.on('video:error', () => { if (!audioSwitching) playbackFailure(); });
    art.on('video:timeupdate', () => {
      if (releaseId) { const delta = art.currentTime-lastPosition; lastPosition = art.currentTime;
        if (!art.video.seeking && !art.video.paused && delta > 0 && delta < 35) { played = true; moved = true; } }
      followList(); if (Date.now() - lastSave > 15000) { lastSave = Date.now(); saveProgress().catch(() => {}); }
    });
    art.on('video:pause', () => saveProgress().catch(() => {}));
    art.on('video:ended', () => {
      saveProgress().catch(() => {});
      const c = current(), n = nextEp();
      if (!c || c.s === 'watched') return;
      screenState(`<b>第 ${c.n} 集放完了</b><div class="links" style="justify-content:center">
        ${n ? `<button class="btn primary" type="button" data-end-next="${n.n}">${icon('check')}看完，播放第 ${n.n} 集</button>` : ''}
        <button class="btn" type="button" data-mark="true">${icon('check')}标记看完</button><button class="btn quiet" type="button" id="dismiss">不标记</button></div>`);
    });
    addEventListener('pagehide', () => saveProgress(true).catch(() => {}));
    document.addEventListener('visibilitychange', () => { if (document.hidden) saveProgress(true).catch(() => {}); });
    loadSubtitles();
    loadAudioTracks();
    restoreDanmu();
  }

  /* ---------- audio tracks ---------- */

  function audioLabel(value) {
    return audioTracks.find(t => String(t.index) === value)?.label || '文件默认';
  }
  function renderAudioSetting() {
    if (audioTracks.length <= 1) {
      if (art.setting.find('audio-track')) art.setting.remove('audio-track');
      return;
    }
    const option = {
      name: 'audio-track', html: '音轨', width: 300,
      tooltip: audioSwitching ? '正在切换…' : esc(audioLabel(audioCurrent)),
      selector: [{html: '文件默认', value: '', default: audioCurrent === ''},
        ...audioTracks.map(t => ({html: esc(t.label), value: String(t.index), default: String(t.index) === audioCurrent}))],
      onSelect: item => {
        changeAudioTrack(item.value);
        // Do not announce a new track before preparation and playback succeed.
        return esc(audioLabel(audioCurrent));
      },
    };
    if (art.setting.find('audio-track')) art.setting.update(option);
    else art.setting.add(option);
  }
  async function loadAudioTracks() {
    if (sourceOnly) return;
    const note = $('#audio-note'), sel = $('#audio-track');
    try {
      const d = await web(`/media/${id}/audio-tracks`);
      audioTracks = d.tracks; audioFingerprint = d.fingerprint;
      audioCopySupported = d.supported;
      $('#audio-track-row').hidden = audioTracks.length <= 1;
      sel.innerHTML = '<option value="">文件默认</option>' + audioTracks.map(t => `<option value="${t.index}">${esc(t.label)}${t.default ? '（默认标记）' : ''}</option>`).join('');
      sel.disabled = !d.supported || !audioTracks.length;
      note.textContent = !audioTracks.length ? '未检测到音轨' : !d.supported ? '此视频请用本机播放器切换音轨' : '';
      if (art.video.error && !audioSwitching) playbackFailure();
      if (sel.disabled) return;
      renderAudioSetting();
      const saved = readSaved(audioStorageKey);
      if (saved?.fingerprint === audioFingerprint && audioTracks.some(t => String(t.index) === saved.index)) {
        await changeAudioTrack(saved.index);
      }
    } catch (e) { note.textContent = '音轨读取失败，可刷新重试或用本机播放器'; }
  }
  function loadAudioUrl(url, snapshot) {
    return new Promise((resolve, reject) => {
      const video = art.video;
      const cleanup = () => { clearTimeout(timer); video.removeEventListener('loadedmetadata', metadata); video.removeEventListener('canplay', ready); video.removeEventListener('error', failed); };
      const failed = () => { cleanup(); reject(Error('这条音轨无法在浏览器播放')); };
      const metadata = () => {
        initialMetadata = false;
        art.currentTime = Math.min(snapshot.time, Math.max(0, art.duration - 0.1));
        art.playbackRate = snapshot.rate;
        art.volume = snapshot.volume; art.muted = snapshot.muted;
      };
      const ready = () => { cleanup(); resolve(); };
      const timer = setTimeout(() => { cleanup(); reject(Error('音轨加载超时，请重试')); }, 30000);
      video.addEventListener('loadedmetadata', metadata);
      video.addEventListener('canplay', ready);
      video.addEventListener('error', failed);
      art.type = url.endsWith('/stream') && !url.includes('/audio-tracks/') ? (media.name.toLowerCase().endsWith('.mp4') ? 'mp4' : 'mkv') : 'mp4';
      art.url = url;
    });
  }
  async function changeAudioTrack(value) {
    value = String(value);
    if (!art || audioSwitching) return;
    if (value && !audioTracks.some(t => String(t.index) === value)) return;
    const rev = ++audioRevision, sel = $('#audio-track'), note = $('#audio-note');
    sel.value = audioCurrent;
    if (value === audioCurrent) { note.textContent = ''; renderAudioSetting(); return; }
    // Preparation leaves the original playback available; a later selection supersedes it.
    try {
      if (value) {
        note.textContent = `正在准备${audioLabel(value)}，当前播放可继续…`;
        art.notice.show = '正在准备音轨…';
        let state = await web(`/media/${id}/audio-tracks/${value}/prepare`, {});
        const deadline = Date.now() + 1250000;
        while (state.state === 'preparing' && rev === audioRevision && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 1000));
          if (rev !== audioRevision) return;
          state = await web(`/media/${id}/audio-tracks/${value}`);
        }
        if (rev !== audioRevision) return;
        if (state.state !== 'ready') throw Error(state.message || '音轨尚未准备好，请重试');
      }
      if (rev !== audioRevision) return;
      const snapshot = {time: initialMetadata ? (media.progress?.position || 0) : art.currentTime, rate: art.playbackRate,
        volume: art.volume, muted: art.muted, paused: art.video.paused, url: art.url};
      audioSwitching = true; sel.disabled = true;
      note.textContent = '正在切换音轨…'; art.pause();
      try {
        await loadAudioUrl(value ? `/api/web/media/${id}/audio-tracks/${value}/stream` : `/api/web/media/${id}/stream`, snapshot);
      } catch (e) {
        await loadAudioUrl(snapshot.url, snapshot).catch(() => {
          playbackFailure();
        });
        if (!snapshot.paused) art.play().catch(() => {});
        throw e;
      }
      audioCurrent = value; sel.value = value;
      lastPosition = art.currentTime;
      screenState('');
      await changeSubtitle().catch(() => {}); await applyDanmu().catch(() => {});
      note.textContent = '';
      if (!snapshot.paused) {
        try { await art.play(); } catch (_) { note.textContent = '音轨已切换，点击播放继续'; }
      }
      try { localStorage.setItem(audioStorageKey, JSON.stringify({index: value, fingerprint: audioFingerprint})); }
      catch (_) { note.textContent = '音轨已切换，但浏览器未能保存选择'; }
      art.notice.show = `音轨：${audioLabel(value)}`;
    } catch (e) {
      if (rev === audioRevision) { note.textContent = e.message; art.notice.show = e.message; }
    } finally {
      if (rev === audioRevision) { audioSwitching = false; sel.disabled = false; sel.value = audioCurrent; renderAudioSetting(); }
    }
  }
  addEventListener('pagehide', () => { ++audioRevision; });

  /* ---------- subtitles ---------- */

  async function loadSubtitles() {
    const note = $('#sub-note'), sel = $('#subtitle');
    if (releaseId) {
      $('#playing-subtitle').textContent = '系统自制 · ' + FinishedReleases.label(media.release) + ' · 已烧录在画面中';
      return;
    }
    try {
      const d = await web(`/media/${id}/subtitles`);
      tracks = d.tracks;
      $('#subtitle-row').hidden = false;
      sel.innerHTML = '<option value="">关闭</option>' + d.tracks.map(t => `<option value="${t.index}"${t.supported ? '' : ' disabled'}>${esc(t.label)}${t.supported ? '' : '（图形字幕）'}</option>`).join('');
      sel.disabled = !d.tracks.some(t => t.supported);
      note.textContent = d.tracks.length ? '' : '未发现可关闭字幕轨；画面硬字幕尚未核验';
      $('#playing-subtitle').textContent = d.tracks.length ? '可关闭字幕已关闭；画面硬字幕不受此开关影响' : '未加载外挂或内嵌字幕；画面硬字幕尚未核验';
      if (d.default !== null) { sel.value = String(d.default); await changeSubtitle(); }
    } catch (e) { note.textContent = e.message; $('#playing-subtitle').textContent = '字幕信息读取失败 · ' + e.message; $('#subtitle-row').hidden = false; }
  }
  let subtitleRenderer = null;
  async function changeSubtitle() {
    const value = $('#subtitle').value, note = $('#sub-note');
    if (!art || releaseId) return;
    subtitleRenderer ??= new SubtitleRenderer(art, note, `/api/web/media/${id}/subtitle-fonts`, ({status,track}) => {
      const source = track?.origin === 'authored' ? '系统自制' : track?.origin === 'embedded' ? '视频自带 · 内嵌软字幕' : track?.origin === 'sidecar' ? '发布包外挂字幕' : '来源未核实';
      const label = track?.label.replace(/^系统自制\s*·\s*/, '');
      const file = track?.origin === 'embedded' ? '轨道 ' + track.index : track?.file_name;
      $('#playing-subtitle').textContent = status === 'off' ? '可关闭字幕已关闭；画面硬字幕不受此开关影响' : `${source} · ${label}${file ? ' · ' + file : ''}${track.build_id ? ' · 版本 ' + track.build_id : ''} · ${{loading:'加载中',active:'正在使用',error:'加载失败'}[status]}`;
    });
    await subtitleRenderer.select(value === '' ? null : tracks.find(x => String(x.index) === value));
  }

  /* ---------- danmu ---------- */

  async function applyDanmu() {
    if (!art) return;
    comments = DanmuTiming.mix(selected, 0, art.duration);
    replaceDanmu ||= DanmuTiming.createReplacer(art.plugins.artplayerPluginDanmuku);
    onScreen.clear(); refreshDanmuList();
    await replaceDanmu(comments);
    renderNote();
  }
  function renderNote() {
    const failures=selected.filter(s=>s.error).length, n=listRows.length;
    $('#danmu-note').textContent = failures ? (n ? `${n} 条 · 有失败` : '来源失败') : `${n} 条`;
  }

  /* ---------- display settings ---------- */

  const DISPLAY_DEFAULTS = {speed: 5, fontSize: 22, opacity: 0.85};
  const pluginDisplay = () => { const {fontFamily, ...rest} = display; return rest; };
  function saveDisplay(patch) {
    display = DanmuTiming.playerSettings({...display, ...patch});
    try { localStorage.setItem(playerStorageKey, JSON.stringify(display)); }
    catch (_) { toast('浏览器未能保存弹幕显示设置'); }
  }
  const speedLabel = v => v <= 2 ? '极快' : v <= 4 ? '快' : v <= 6 ? '标准' : v <= 8 ? '慢' : '极慢';
  function renderDisplay() {
    const o = art?.plugins.artplayerPluginDanmuku.option || {};
    const speed = typeof o.speed === 'number' ? o.speed : DISPLAY_DEFAULTS.speed, opacity = typeof o.opacity === 'number' ? o.opacity : DISPLAY_DEFAULTS.opacity;
    const size = typeof o.fontSize === 'number' ? o.fontSize : DISPLAY_DEFAULTS.fontSize;
    $('#dm-speed').value = 11 - speed; $('#dm-speed-out').textContent = speedLabel(speed);
    $('#dm-font-size').value = size; $('#dm-font-size-out').textContent = typeof o.fontSize === 'string' ? o.fontSize : `${size}px`;
    $('#dm-opacity').value = opacity; $('#dm-opacity-out').textContent = `${Math.round(opacity * 100)}%`;
    $('#dm-font').value = display.fontFamily || 'default';
    const font = DanmuTiming.FONTS[display.fontFamily] || '', layer = art?.template.$danmuku;
    if (layer) { layer.style.setProperty('--dm-font', font); layer.toggleAttribute('data-font', !!font); }
  }
  const configDisplay = patch => art?.plugins.artplayerPluginDanmuku.config(patch);

  /* ---------- screenshot ---------- */

  const shotStorageKey = 'fanyu-screenshot';
  let shot = {danmu: false, subtitle: true, to: 'download', ...readSaved(shotStorageKey)};
  function saveShot(patch) {
    shot = {...shot, ...patch};
    try { localStorage.setItem(shotStorageKey, JSON.stringify(shot)); } catch (_) {}
    renderShot();
  }
  function renderShot() {
    $('#shot-danmu').checked = shot.danmu; $('#shot-subtitle').checked = shot.subtitle;
    for (const x of $('#shot-to').children) x.setAttribute('aria-pressed', String(x.dataset.to === shot.to));
  }
  /* Synchronous up to the clipboard write: browsers only allow it inside the click. */
  function screenshot() {
    if (!art) return;
    const libass = subtitleRenderer?.renderer?.canvas, layer = art.plugins.artplayerPluginDanmuku;
    let canvas;
    try {
      canvas = Screenshot.capture(art.video, {
        layers: shot.subtitle && libass ? [libass] : [],
        texts: [...(shot.subtitle && art.subtitle.show ? art.template.$subtitle.children : []), ...(shot.danmu && !layer.isHide ? art.template.$danmuku.children : [])],
      });
    } catch (e) { art.notice.show = e.message; return; }
    const name = `${show ? `${show.show.title} 第 ${current()?.n ?? media.episode} 集` : media.name.replace(/\.[^.]+$/, '')} ${clock(art.currentTime).replace(/:/g, '-')}`;
    const done = text => { art.notice.show = text; };
    if (shot.to === 'clipboard' && Screenshot.canCopy()) Screenshot.copy(canvas).then(() => done('截图已复制到剪贴板'), e => done('复制失败：' + e.message));
    else Screenshot.download(canvas, name).then(() => done(shot.to === 'clipboard' ? '浏览器不支持复制图片，已改为下载' : '截图已下载'), e => done(e.message));
  }

  /* ---------- danmu list and blocking ---------- */

  const ROW = 32;
  const isBlocked = c => !!c.user && blocked.has(c.user);
  function showDanmu(d) {
    if (isBlocked(d)) return false;
    if (onScreen.size > 300) for (const x of onScreen) if (!x.$ref) onScreen.delete(x);
    onScreen.add(d);
    return true;
  }
  function danmuAt(x, y) {
    if (art.plugins.artplayerPluginDanmuku.isHide) return null;
    for (const d of onScreen) {
      if (!d.$ref) { onScreen.delete(d); continue; }
      if (d.$ref.style.visibility !== 'visible' || !d.$ref.textContent) continue;
      const r = d.$ref.getBoundingClientRect();
      if (x >= r.left - 4 && x <= r.right + 4 && y >= r.top - 4 && y <= r.bottom + 4) return d;
    }
    return null;
  }
  function refreshDanmuList() {
    listRows = comments.filter(c => !isBlocked(c));
    $('#danmu-list-space').style.height = `${listRows.length * ROW}px`;
    $('#danmu-list-count').textContent = listRows.length || '';
    renderRows();
  }
  function renderRows() {
    if ($('#danmu-list').hidden) return;
    const box = $('#danmu-list-scroll'), first = Math.max(0, Math.floor(box.scrollTop / ROW) - 5);
    const last = Math.min(listRows.length, first + Math.ceil(box.clientHeight / ROW) + 10);
    $('#danmu-list-space').innerHTML = listRows.slice(first, last).map((c, k) =>
      `<div class="dm-row" data-row="${first + k}" style="top:${(first + k) * ROW}px"><time>${clock(c.time)}</time><span title="${esc(c.text)}">${esc(c.text)}</span></div>`).join('');
  }
  /* Keep the latest comment at the bottom, chat-style, unless the viewer is scrolling. */
  function followList() {
    if (!art || $('#danmu-list').hidden || Date.now() < followUntil) return;
    const box = $('#danmu-list-scroll'), t = art.currentTime;
    let lo = 0, hi = listRows.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (listRows[mid].time <= t) lo = mid + 1; else hi = mid; }
    const top = Math.max(0, lo * ROW - box.clientHeight);
    if (Math.abs(box.scrollTop - top) > 1) box.scrollTop = top;
  }
  function selectTab(tab) {
    const list = tab === 'danmu-list';
    $('#tab-playlist').setAttribute('aria-selected', String(!list)); $('#tab-playlist').tabIndex = list ? -1 : 0;
    $('#tab-danmu-list').setAttribute('aria-selected', String(list)); $('#tab-danmu-list').tabIndex = list ? 0 : -1;
    $('#playlist').hidden = list; $('#danmu-list').hidden = !list;
    if (list) { followUntil = 0; renderRows(); followList(); }
  }
  function openMenu(c, x, y) {
    const menu = $('#danmu-menu'), b = $('#danmu-block-user');
    menuTarget = c;
    (document.fullscreenElement || document.body).append(menu);
    $('#danmu-menu-text').textContent = c.text;
    b.disabled = !c.user; b.textContent = c.user ? '屏蔽此用户' : '此来源不提供发送者，无法屏蔽';
    $('#danmu-find-user').hidden = !(c.user && c.site === 'bilibili');
    $('#danmu-menu-uids').hidden = true;
    menu.hidden = false;
    menu.style.left = `${Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8))}px`;
    followUntil = Infinity;
    (b.disabled ? menu : b).focus();
  }
  function closeMenu() {
    if ($('#danmu-menu').hidden) return;
    $('#danmu-menu').hidden = true; menuTarget = null; followUntil = Date.now() + 3000;
  }
  function saveBlocked() {
    try { localStorage.setItem(blockedStorageKey, JSON.stringify([...blocked].map(([id, u]) => ({id, ...u})))); }
    catch (_) { toast('本次屏蔽已生效，但浏览器未能保存'); }
  }
  function blockUser(c) {
    blocked.set(c.user, {text: c.text.slice(0, 60), site: c.site}); saveBlocked();
    for (const d of onScreen) if (d.user === c.user && d.$ref) d.$ref.textContent = '';
    refreshDanmuList(); renderNote(); renderBlocked();
    toast('已屏蔽该用户的弹幕', {label: '撤销', run: () => unblockUser(c.user)});
  }
  function unblockUser(user) {
    blocked.delete(user); saveBlocked();
    refreshDanmuList(); renderNote(); renderBlocked();
  }
  const SITE_NAMES = {bilibili: 'B站', bahamut: '巴哈'};
  /* Candidates only: several UIDs can share one hash, and 16-digit UIDs cannot be recovered. */
  function renderUids(box, hash) {
    const uids = BiliUid.uidCandidates(hash);
    box.innerHTML = uids.length
      ? `<p>可能是${uids.length > 1 ? `以下 ${uids.length} 个之一` : ''}：</p>${uids.map(uid => `<a href="https://space.bilibili.com/${uid}" target="_blank" rel="noopener noreferrer">UID ${uid}</a>`).join('')}`
      : '<p>没有 10 位以内的 UID 匹配，可能是新注册的 16 位 UID，无法反查。</p>';
    box.hidden = false;
  }
  function renderBlocked() {
    const box = $('#dm-blocked'), open = !!box.querySelector('details[open]');
    box.innerHTML = blocked.size ? `<details class="dm-blocked"${open ? ' open' : ''}><summary><b>${blocked.size}</b> 个用户${icon('down')}</summary><div class="dm-blocked-list">${[...blocked].reverse().map(([user, u]) =>
      `<div class="dm-blocked-row"><span><b>${esc(u.text || '（无示例弹幕）')}</b><small>${SITE_NAMES[u.site] || '用户'} ${esc(user)}</small></span><span class="dm-blocked-acts">${u.site === 'bilibili' ? `<button class="btn quiet small" type="button" data-find-uid="${esc(user)}">查找 UID</button>` : ''}<button class="btn quiet small" type="button" data-unblock="${esc(user)}">解除屏蔽</button></span></div>${u.site === 'bilibili' ? `<div class="dm-uids" data-uids="${esc(user)}" hidden></div>` : ''}`).join('')}</div></details>`
      : '<p class="pc-note">无</p>';
  }
  function restoreTiming(source) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(`fanyu-danmu:${id}:${source.source_identity}`) || 'null'); } catch (_) {}
    const timing = saved || source;
    return {...source, ...DanmuTiming.settings(timing), ...DanmuAlignment.preferences(timing),
      alignmentManual: timing.alignmentManual ?? (!!saved || !!timing.offset)};
  }
    function saveTiming(source) {
    if (!source.source_identity) return;
    try { localStorage.setItem(`fanyu-danmu:${id}:${source.source_identity}`, JSON.stringify({...DanmuTiming.settings(source), ...DanmuAlignment.preferences(source)})); }
      catch (_) { toast('本次调整已生效，但浏览器未能保存设置'); }
      saveSources();
    }
  function renderSelected() {
    if (document.activeElement?.matches('#danmu-selected input')) return;
    $('#danmu-selected').innerHTML = selected.map((x, i) => {
      const site = DanmuAlignment.sourceSite(x), p = sourceLabel(site || x.title), busy = ['queued','running','retrying'].includes(x.alignmentStatus);
      const unavailable = !['bilibili','bahamut'].includes(site) ? '自动对轴目前支持 B站视频和巴哈姆特来源，可用提前／延后手动调整'
        : !alignmentSites.includes(site) ? '巴哈姆特自动对轴等待服务空闲更新，原偏移保持'
        : sourceOnly ? '服务更新等待播放空闲，暂不能自动对轴，可先手动调整' : '';
      const status = x.loading ? '加载中…' : x.error ? '获取失败' : x.count > 0 ? `${x.count} 条` : x.count === 0 ? '暂无弹幕' : '尚未加载';
      const alignStatus = unavailable || (x.alignmentEnabled === false ? '自动对齐已关闭' : x.alignmentManual ? '手动调时' : ({queued:'等待音频对齐…', running:'正在音频对齐…', retrying:x.alignmentMessage || '稍后自动重试音频对齐', matched:`已自动对齐 ${x.offset > 0 ? '+' : ''}${x.offset || 0} 秒`, unreliable:'音频未能可靠匹配', unsupported:'此来源暂不支持音频对齐，可手动调整', unavailable:x.alignmentMessage || '自动对齐不可用'}[x.alignmentStatus] || ''));
      const acts = [
        sourceLink(x.source_url),
        `<button class="dm-act" type="button" data-align-source="${i}"${busy || unavailable ? ' disabled' : ''}${unavailable ? ` title="${esc(unavailable)}"` : ''}>${busy ? '自动对轴中…' : x.alignmentStatus && !x.alignmentManual && x.alignmentEnabled !== false ? '重新自动对轴' : '自动对轴'}</button>`,
        !unavailable && x.alignmentEnabled !== false ? `<button class="dm-act" type="button" data-disable-alignment="${i}">关闭自动对齐</button>` : '',
        `<button class="dm-act dm-act-remove" type="button" data-remove="${i}">移除</button>`,
      ].join('');
      return `<article class="dm-source"><span class="dm-mark" aria-hidden="true">${p.mark}</span>
      <div class="dm-info"><div class="dm-line"><b class="dm-name">${p.name}</b><span class="dm-meta" data-state="${x.error ? 'error' : x.count > 0 ? 'ok' : ''}">${status}</span>${!x.loading && (x.error || !x.count) ? `<button class="dm-act" data-reload-source="${i}" type="button">${x.error ? '重试' : '刷新'}</button>` : ''}</div>
      <p class="dm-title" title="${esc(cleanTitle(x.title))}">${esc(cleanTitle(x.title))}${x.uploader ? ` · UP主 ${esc(x.uploader)}` : ''}</p><div class="dm-acts">${acts}</div></div>
      <div class="dm-timing"><div class="dm-stepper"><button type="button" data-nudge="${i}" data-delta="-1" aria-label="${p.name} 提前1秒">提前</button><label class="dm-value"><input type="number" min="-3600" max="3600" step="0.1" data-timing="${i}" data-field="offset" value="${x.offset || 0}" aria-label="${p.name} 偏移秒数"><span>秒</span></label><button type="button" data-nudge="${i}" data-delta="1" aria-label="${p.name} 延后1秒">延后</button></div>${alignStatus ? `<p class="dm-alignment" role="status" data-state="${x.alignmentStatus === 'matched' && !x.alignmentManual && x.alignmentEnabled !== false ? 'ok' : ''}">${esc(alignStatus)}</p>` : ''}</div></article>`;
    }).join('') || '<p class="dm-empty">还没有弹幕来源。</p>';
  }
  function sourceLabel(title = '') {
    if (/bilibili|B站/i.test(title)) return {name:'Bilibili', mark:'哔'};
    if (/bahamut|巴哈/i.test(title)) return {name:'巴哈姆特', mark:'巴'};
    if (/iqiyi/i.test(title)) return {name:'爱奇艺', mark:'爱'};
    if (/youku/i.test(title)) return {name:'优酷', mark:'优'};
    if (/tencent/i.test(title)) return {name:'腾讯视频', mark:'腾'};
    return {name:'弹幕来源', mark:'弹'};
  }
  function cleanTitle(title = '') {
    return title.replace(/\((?:\d{4}|N\/A)\)【[^】]*】from \w+/g, '').replace(/【(?:bilibili1|bahamut)】\s*/g, '').trim();
  }
  function sourceLink(url, label = '原站') {
    try {
      const u = new URL(url);
      if (u.protocol !== 'https:' || u.username || u.password || u.port || !['www.bilibili.com','ani.gamer.com.tw','www.iqiyi.com','v.youku.com','v.qq.com'].includes(u.hostname)) return '';
      if ([...u.searchParams.keys()].some(k => !['p','sn'].includes(k))) return '';
      return `<a class="dm-source-link" href="${esc(u.href)}" target="_blank" rel="noopener noreferrer" aria-label="${esc(label)}（新标签页）">${esc(label)}${icon('external')}</a>`;
    } catch (_) { return ''; }
  }
  const chosenEpisode = () => activeDanmu?.episodes?.[Number($('#danmu-episodes input[name="danmu-ep"]:checked')?.value)];
  function renderDanmuDetail(d) {
    activeDanmu = d.bangumi;
    activeDanmu.uploader ||= resultRows.find(x => String(x.animeId) === String(activeDanmu.animeId))?.uploader || '';
    showDetail(true, '选择分集');
    const episodes=activeDanmu.episodes || [], ugc=activeDanmu.type?.includes('B站视频');
    const here=ugc ? -1 : episodes.findIndex(x=>x.source_key&&Number(x.episodeNumber)===media.episode);
    const preferred=episodes.findIndex(x=>x.source_key&&x.url===activeDanmu.preferred_url);
    const pick=preferred>=0 ? preferred : here>=0 ? here : episodes.findIndex(x=>x.source_key);
    const p=sourceLabel(siteOf(activeDanmu) || activeDanmu.animeTitle);
    $('#danmu-episodes').innerHTML = `<div class="dm-detail-head"><span class="dm-mark" aria-hidden="true">${p.mark}</span><div><h3>${esc(cleanTitle(activeDanmu.animeTitle))}</h3><small>${p.name} · ${episodes.length} ${ugc ? '个分P · 请核对版本与集数' : '集'}</small><small id="danmu-uploader">${activeDanmu.uploader ? `UP主 ${esc(activeDanmu.uploader)}` : ''}</small></div></div>${episodes.length
      ? `<div class="dm-eps" role="radiogroup" aria-label="分集">${episodes.map((x, i) => `<label class="dm-ep"><input type="radio" name="danmu-ep" value="${i}"${x.source_key ? '' : ' disabled'}${i === pick ? ' checked' : ''}><span>${esc(cleanTitle(x.episodeTitle))}</span>${i === here ? '<em>本集</em>' : ''}</label>`).join('')}</div><footer class="dm-detail-foot"><div><span id="danmu-episode-link"></span><small id="danmu-source-info" aria-live="polite"></small></div><button class="btn primary" type="button" id="add-source"${pick < 0 ? ' disabled' : ''}>添加</button></footer>`
      : '<p class="dm-empty">此来源暂时没有可用分集。</p>'}`;
    const list=$('#danmu-episodes .dm-eps'), chosen=$('#danmu-episodes input:checked')?.closest('label');
    if (list && chosen) list.scrollTop = chosen.offsetTop - (list.clientHeight - chosen.offsetHeight) / 2;
    updateEpisodeLink();
  }
  const siteOf = x => x.site || x.source || x.animeTitle?.match(/from (\w+)$/)?.[1];
  function showDetail(on, title = '添加弹幕来源') {
    $('#danmu-search-view').hidden = on; $('#danmu-detail-view').hidden = !on; $('#danmu-back').hidden = !on;
    $('#danmu-dialog-title').textContent = title;
  }
  function updateEpisodeLink() {
    const box = $('#danmu-episode-link');
    if (box) box.innerHTML = sourceLink(chosenEpisode()?.source_url, '此集原站');
    const ep = chosenEpisode(), button = $('#add-source'), info = $('#danmu-source-info');
    const existing = ep && selected.find(x => x.source_identity === ep.source_identity);
    if (button) { button.disabled = !ep?.source_key || !!existing; button.textContent = existing ? '已添加' : '添加'; }
    clearTimeout(infoTimer);
    if (!info) return;
    if (existing && typeof existing.count === 'number' && !existing.error && !existing.loading) {
      info.textContent = `已加载 ${existing.count} 条弹幕`; return;
    }
    if (ep?.site !== 'bilibili' || !ep.source_key) { info.textContent = ''; return; }
    const cached = sourceInfoCache.get(ep.source_identity);
    if (cached && cached.expires > Date.now()) { showSourceInfo(ep, cached.data); return; }
    info.textContent = '正在查询弹幕数量…';
    infoTimer = setTimeout(queryChosenSourceInfo, 300);
  }
  function showSourceInfo(ep, data) {
    rememberUploader(ep, data);
    if (data.uploader) {
      const uploader = $('#danmu-uploader');
      if (uploader) uploader.textContent = `UP主 ${data.uploader}`;
    }
    const info = $('#danmu-source-info');
    if (info) info.textContent = Number.isInteger(data.count) && data.count >= 0
      ? `B站弹幕 ${data.count.toLocaleString()} 条${data.count === 0 ? ' · 暂无弹幕' : ''}`
      : '数量暂不可用，可添加后查看实际加载条数';
  }
  function rememberUploader(ep, data) {
    if (!data.uploader) return;
    ep.uploader = data.uploader;
    const source = selected.find(x => x.source_identity === ep.source_identity);
    if (source && source.uploader !== data.uploader) {
      source.uploader = data.uploader; saveSources(); renderSelected();
    }
  }
  async function queryChosenSourceInfo() {
    if (infoBusy || $('#danmu-detail-view').hidden || !$('#danmu-dialog').open) return;
    const ep = chosenEpisode(), rev = detailRev;
    if (ep?.site !== 'bilibili' || !ep.source_key) return;
    infoBusy = true;
    try {
      let data;
      try { data = await api('/web/danmu/source/' + encodeURIComponent(ep.source_key) + '/info'); }
      catch (_) { data = {count: null}; }
      if (sourceInfoCache.size >= 100) sourceInfoCache.delete(sourceInfoCache.keys().next().value);
      sourceInfoCache.set(ep.source_identity, {data, expires: Date.now() + (data.count === null ? 30000 : 600000)});
      rememberUploader(ep, data);
      if (rev === detailRev && ep === chosenEpisode() && $('#danmu-dialog').open) showSourceInfo(ep, data);
    } finally {
      infoBusy = false;
      // Rapid selection changes queue only the latest part, never every intermediate part.
      if (rev !== detailRev || ep !== chosenEpisode()) updateEpisodeLink();
    }
  }
  function renderDanmuResults(rows = resultRows, context = resultContext) {
    resultRows = rows; resultContext = context;
    const site = $('#danmu-site [aria-pressed="true"]').dataset.site;
    for (const b of $('#danmu-site').children) {
      const n = b.dataset.site ? rows.filter(x => siteOf(x) === b.dataset.site).length : rows.length;
      b.dataset.count = n;
      b.disabled = !n && b.dataset.site !== site;
    }
    const shown = rows.filter(x => !site || siteOf(x) === site);
    $('#danmu-results-note').textContent = `${context} · ${shown.length} 个`;
    $('#danmu-results').innerHTML = shown.map(x => { const p=sourceLabel(siteOf(x) || x.animeTitle); return `<div class="dm-result-row"><button class="dm-result" type="button" data-anime="${esc(x.animeId)}"><span class="dm-mark" aria-hidden="true">${p.mark}</span><span><b>${esc(cleanTitle(x.animeTitle))}</b><small>${p.name} · ${x.episodeCount ?? '—'} ${x.type?.includes('B站视频') ? '个分P' : '集'}${x.uploader ? ` · UP主 ${esc(x.uploader)}` : ''}${Number.isInteger(x.danmaku_total) && x.danmaku_total >= 0 ? ` · 视频总弹幕 ${x.danmaku_total.toLocaleString()} 条` : ''}</small></span>${icon('chevron')}</button>${sourceLink(x.source_url)}</div>`; }).join('') || `<p class="dm-empty">${rows.length && site ? '这类来源没有结果。' : '没有找到相关结果。B站标题搜索受收录限制，可用网址或 BV号直达查询。'}</p>`;
  }
  async function loadSource(source) {
    source.loading=true; source.error=null; renderSelected();
    try {
      const d=await web('/danmu/comments', {episodes:[source.id]});
      if (!selected.includes(source)) return;
      Object.assign(source, d.sources[0]);
    } catch(e) { if (selected.includes(source)) { source.error=e.message; source.comments=[]; } }
    finally { source.loading=false; if (selected.includes(source)) { renderSelected(); updateEpisodeLink(); await applyDanmu(); alignSource(source,{confirmed:source.alignmentConfirmed === true}); } }
  }
  function closeSourceDialog() { ++detailRev; ++searchRev; $('#danmu-dialog').close(); }
  async function restoreDanmu() {
    const saved = DanmuTiming.savedSources(readSaved(sourceStorageKey));
    $('#retry-danmu').disabled = false;
    if (saved === null) return autoDanmu();
    selected = saved.map(restoreTiming);
    renderSelected();
    await applyDanmu();
    // An explicitly empty list stays empty; removed automatic sources stay removed.
    await Promise.all(selected.map(source => loadSource(source)));
    if (saved.length) await autoDanmu();
  }
  async function autoDanmu() {
    const rev = ++danmuRev, note = $('#danmu-note');
    $('#retry-danmu').disabled = false;
    note.textContent = '匹配中…';
    note.title = '临时失败会自动重试，最多三次';
    const searchVersion = searchRev;
    try {
      const d = sourceOnly
        ? await DanmuMatch.match(show.show, media.episode, web)
        : await web(`/media/${id}/danmu`);
      if (rev !== danmuRev) return;
      if (searchVersion === searchRev && d.candidates) renderDanmuResults(d.candidates, '本集推荐');
      if (d.status === 'matched') {
        const matched=d.selected.map(x => restoreTiming({...x, ...d.sources.find(s => s.id === x.id)}));
        for (const s of matched) { if (removedSources.has(s.source_identity)) continue; const old=selected.find(x=>x.source_identity===s.source_identity); if(old) Object.assign(old,{id:s.id,comments:s.comments,count:s.count,error:s.error}); else if(selected.length<5) selected.push(s); }
        saveSources(); renderSelected(); await applyDanmu(); note.title = d.title || '';
        selected.forEach(source => alignSource(source));
      } else { if (!selected.length) { note.textContent = d.status === 'error' ? '暂时失败' : '未匹配'; renderSelected(); if(d.message) $('#danmu-selected .dm-empty').textContent=d.message; } note.title = d.message || ''; }
    } catch (e) { if (rev === danmuRev) { note.textContent = '未连接'; note.title = e.message; if(!selected.length) $('#danmu-selected').innerHTML='<p class="dm-empty">自动匹配暂时失败。</p>'; } }
  }

  /* ---------- events ---------- */

  document.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.mark) return act(b, async () => { screenState(''); await mark(current().n, b.dataset.mark === 'true'); });
    if (b.dataset.ep) return act(b, () => mark(Number(b.dataset.ep), b.dataset.finished === 'true'));
    if (b.dataset.endNext) return act(b, async () => { const next = nextEp(); await mark(current().n, true); if (next) location.assign(watchHref(next)); });
    if (b.dataset.remove) { ++danmuRev; const i=Number(b.dataset.remove); alignment.cancel(selected[i]); removedSources.add(selected[i].source_identity); saveRemoved(); selected.splice(i, 1); saveSources(); renderSelected(); applyDanmu().catch(e => toast(e.message)); (document.querySelector(`[data-timing="${Math.min(i,selected.length-1)}"][data-field="offset"]`) || $('#open-danmu-search')).focus(); return; }
    if (b.dataset.nudge !== undefined) {
      const x = selected[Number(b.dataset.nudge)];
      alignment.manual(x);
      x.offset = Math.max(-3600, Math.min(3600, (x.offset || 0) + Number(b.dataset.delta)));
      saveTiming(x); const selector=`[data-nudge="${b.dataset.nudge}"][data-delta="${b.dataset.delta}"]`; renderSelected(); document.querySelector(selector)?.focus(); applyDanmu().catch(e => toast(e.message)); return;
    }
    if (b.dataset.findUid !== undefined) { const box = document.querySelector(`[data-uids="${CSS.escape(b.dataset.findUid)}"]`); if (box.hidden) renderUids(box, b.dataset.findUid); else box.hidden = true; return; }
    if (b.dataset.unblock !== undefined) { unblockUser(b.dataset.unblock); ($('#dm-blocked [data-unblock]') || $('#danmu-display-reset')).focus(); return; }
    if (b.dataset.reloadSource !== undefined) return loadSource(selected[Number(b.dataset.reloadSource)]);
    if (b.dataset.alignSource !== undefined) {
      const source=selected[Number(b.dataset.alignSource)];
      if (!source || sourceOnly || !alignmentSites.includes(DanmuAlignment.sourceSite(source))) return;
      source.alignmentEnabled=true; source.alignmentManual=false; saveTiming(source);
      return alignment.start(source,{force:true,confirmed:true});
    }
    if (b.dataset.disableAlignment !== undefined) {
      const source=selected[Number(b.dataset.disableAlignment)];
      alignment.cancel(source); source.alignmentEnabled=false; source.alignmentStatus='disabled'; saveTiming(source); renderSelected(); return;
    }
    if (b.dataset.to) return saveShot({to: b.dataset.to});
    if (b.dataset.site !== undefined) { for (const x of $('#danmu-site').children) x.setAttribute('aria-pressed', String(x === b)); return renderDanmuResults(); }
    if (b.dataset.anime) return act(b, async () => {
      ++danmuRev;
      const rev=++detailRev;
      showDetail(true, '选择分集');
      $('#danmu-episodes').innerHTML='<p class="dm-empty" role="status">正在读取分集…</p>';
      $('#danmu-back').focus();
      let d;
      try { d = await web('/danmu/show/' + b.dataset.anime); }
      catch(e) { if(rev===detailRev) $('#danmu-episodes').innerHTML=`<p class="dm-empty">${esc(e.message)}。返回搜索结果后可重试。</p>`; return; }
      if(rev!==detailRev) return;
      renderDanmuDetail(d);
    });
    switch (b.id) {
      case 'dismiss': return screenState('');
      case 'restart': if (art) { art.currentTime = 0; art.play(); b.remove(); } return;
      case 'desktop': return act(b, async () => { await web(`/media/${id}/desktop`, {}); toast('已在本机播放器打开'); });
      case 'compat-playback': return act(b, async () => {
        const track = audioTracks.find(t => t.default) || audioTracks[0];
        if (!audioCopySupported || !track) return;
        await changeAudioTrack(String(track.index));
      });
      case 'retry-danmu': return autoDanmu();
      case 'shot': return screenshot();
      case 'danmu-display-reset': saveDisplay({fontFamily: 'default'}); configDisplay(DISPLAY_DEFAULTS); renderDisplay(); return;
      case 'tab-playlist': case 'tab-danmu-list': return selectTab(b.getAttribute('aria-controls'));
      case 'open-danmu-search': showDetail(false); $('#danmu-dialog').showModal(); $('#danmu-q').focus(); return;
      case 'close-danmu-search': return closeSourceDialog();
      case 'danmu-back': ++detailRev; showDetail(false); $('#danmu-q').focus(); return;
      case 'add-source': {
        const ep = chosenEpisode();
        if (!ep?.source_key) return toast('这一集不能直接加载');
        const existing=selected.find(x=>x.source_identity===ep.source_identity);
        if(existing) return toast('这个来源已经在使用');
        if (selected.length >= 5) return toast('最多使用 5 个来源，先移除一个再添加');
        ++danmuRev;
        const source=restoreTiming({ id: ep.source_key, source_identity: ep.source_identity, source_url: ep.source_url, site: ep.site, uploader: ep.uploader || activeDanmu.uploader || '', title: activeDanmu.animeTitle + ' · ' + ep.episodeTitle });
        removedSources.delete(source.source_identity); saveRemoved(); source.alignmentConfirmed=true; selected.push(source); saveSources(); updateEpisodeLink();
        ++detailRev; clearTimeout(infoTimer); showDetail(false);
        (document.querySelector(`[data-anime="${CSS.escape(String(activeDanmu.animeId))}"]`) || $('#danmu-q')).focus();
        toast('已添加，可以继续选择其他来源'); return loadSource(source);
      }
      case 'load-danmu': return act(b, async () => {
        const rev = ++danmuRev, sources = [...selected];
        const d = await web('/danmu/comments', { episodes: sources.map(x => x.id) });
        if (rev !== danmuRev) return;
        selected = selected.map(x => ({...x, error: null, ...d.sources.find(s => s.id === x.id)}));
        renderSelected();
        await applyDanmu();
        $('#danmu-note').textContent = `${comments.length} 条`;
        const failed = d.sources.filter(s => s.error);
        if (failed.length) toast(`${failed.length} 个来源失败：${failed[0].error}`);
      });
    }
  });
  $('#subtitle').addEventListener('change', changeSubtitle);
  $('#audio-track').addEventListener('change', e => changeAudioTrack(e.target.value));
  $('#danmu-dialog').addEventListener('cancel', () => { ++detailRev; });
  $('#danmu-dialog').addEventListener('keydown', e => { if(e.key==='Escape' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); closeSourceDialog(); } });
  $('#danmu-dialog').addEventListener('close', () => $('#open-danmu-search').focus());
  document.addEventListener('change', e => {
    const input = e.target;
    if (input.name === 'danmu-ep') return updateEpisodeLink();
    if (input.dataset.timing === undefined) return;
    if (!input.checkValidity()) { input.reportValidity(); return; }
    const source = selected[Number(input.dataset.timing)];
    if (input.dataset.field === 'offset') alignment.manual(source);
    source[input.dataset.field] = input.value === '' ? null : Number(input.value);
    Object.assign(source, DanmuTiming.settings(source));
    saveTiming(source); applyDanmu().catch(e => toast(e.message));
  });
  document.addEventListener('input', e => {
    if (e.target.dataset.field === 'offset' && e.target.dataset.timing !== undefined) {
      alignment.manual(selected[Number(e.target.dataset.timing)]);
    }
  });
  $('#danmu-selected').addEventListener('focusout', () => setTimeout(() => {
    if (!document.activeElement?.matches('#danmu-selected input')) renderSelected();
  },0));
  $('#danmu-search').addEventListener('submit', e => {
    e.preventDefault();
    ++danmuRev;
    const rev = ++searchRev;
    ++detailRev;
    act(e.target.querySelector('button'), async () => {
      $('#danmu-results-note').textContent='搜索中…';
      resultRows=[]; $('#danmu-results').innerHTML='';
      try {
        const d = await web('/danmu/search?q=' + encodeURIComponent($('#danmu-q').value.trim()));
        if (rev === searchRev) {
          if (d.bangumi) { $('#danmu-results-note').textContent='直达查询完成'; renderDanmuDetail(d); }
          else renderDanmuResults(d.animes || [], '搜索结果');
        }
      } catch(e) { if(rev===searchRev) { $('#danmu-results-note').textContent='搜索未完成'; $('#danmu-results').innerHTML=`<p class="dm-empty">${esc(e.message)}。可重试，或使用网址 / BV号直达查询。</p>`; } }
    });
  });
  $('#danmu-search').addEventListener('keydown', e => { if(e.key==='Enter' && (e.isComposing || e.keyCode===229)) e.preventDefault(); });
  $('#danmu-direct').addEventListener('submit', e => {
    e.preventDefault();
    ++danmuRev; ++searchRev;
    const rev=++detailRev;
    act(e.target.querySelector('button'), async () => {
      showDetail(true, '查询B站视频');
      $('#danmu-episodes').innerHTML='<p class="dm-empty" role="status">正在读取视频标题与分P…</p>';
      $('#danmu-back').focus();
      try {
        const d=await web('/danmu/resolve?q='+encodeURIComponent($('#danmu-url').value.trim()));
        if(rev===detailRev) renderDanmuDetail(d);
      } catch(e) {
        if(rev===detailRev) $('#danmu-episodes').innerHTML=`<p class="dm-empty">${esc(e.message)}。返回后可修改输入或重试。</p>`;
      }
    });
  });
  $('#danmu-direct').addEventListener('keydown', e => { if(e.key==='Enter' && (e.isComposing || e.keyCode===229)) e.preventDefault(); });
  $('#dm-speed').addEventListener('input', e => configDisplay({speed: 11 - Number(e.target.value)}));
  $('#dm-font-size').addEventListener('input', e => configDisplay({fontSize: Number(e.target.value)}));
  $('#dm-opacity').addEventListener('input', e => configDisplay({opacity: Number(e.target.value)}));
  $('#dm-font').addEventListener('change', e => { saveDisplay({fontFamily: e.target.value}); renderDisplay(); });
  $('#shot-danmu').addEventListener('change', e => saveShot({danmu: e.target.checked}));
  $('#shot-subtitle').addEventListener('change', e => saveShot({subtitle: e.target.checked}));
  const listBox = $('#danmu-list-scroll');
  let rowsFrame = 0;
  listBox.addEventListener('scroll', () => { cancelAnimationFrame(rowsFrame); rowsFrame = requestAnimationFrame(renderRows); });
  for (const type of ['wheel', 'touchstart', 'pointerdown', 'keydown']) listBox.addEventListener(type, () => { if (followUntil !== Infinity) followUntil = Date.now() + 5000; }, {passive: true});
  listBox.addEventListener('contextmenu', e => {
    const row = e.target.closest('[data-row]');
    if (!row) return;
    e.preventDefault(); openMenu(listRows[Number(row.dataset.row)], e.clientX, e.clientY);
  });
  $('.side-tabs').addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const next = $('#tab-playlist').getAttribute('aria-selected') === 'true' ? 'tab-danmu-list' : 'tab-playlist';
    selectTab($('#' + next).getAttribute('aria-controls')); $('#' + next).focus();
  });
  // In fullscreen the menu lives inside the player; keep its clicks from toggling playback.
  for (const type of ['click', 'dblclick', 'pointerdown', 'mousedown', 'contextmenu']) $('#danmu-menu').addEventListener(type, e => { e.stopPropagation(); if (type === 'contextmenu') e.preventDefault(); });
  $('#danmu-block-user').addEventListener('click', () => { const c = menuTarget; closeMenu(); if (c?.user) blockUser(c); });
  $('#danmu-find-user').addEventListener('click', () => {
    if (!menuTarget?.user) return;
    const menu = $('#danmu-menu');
    renderUids($('#danmu-menu-uids'), menuTarget.user);
    menu.style.left = `${Math.max(8, Math.min(parseFloat(menu.style.left), innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(parseFloat(menu.style.top), innerHeight - menu.offsetHeight - 8))}px`;
  });
  document.addEventListener('pointerdown', e => { if (!e.target.closest('#danmu-menu')) closeMenu(); }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#danmu-menu').hidden) { e.preventDefault(); closeMenu(); } });
  for (const type of ['resize', 'blur']) addEventListener(type, closeMenu);
  document.addEventListener('fullscreenchange', closeMenu);
  addEventListener('resize', renderRows);
  $('#close-danmu-search').innerHTML = icon('close'); $('#danmu-back').innerHTML = icon('back');
  renderDisplay(); renderBlocked(); renderShot();
  $('#playback-source').addEventListener('change', async e => { await saveProgress(); location.assign(e.target.value); });

  (async () => {
    try {
      await boot();
      try {
        const capabilities = await web('/alignment-capabilities');
        if (Array.isArray(capabilities.sites)) alignmentSites = capabilities.sites.filter(site => ['bilibili','bahamut'].includes(site));
      } catch (_) { /* An older worker can keep protecting active playback. */ }
      try { releases = await FinishedReleases.load({verifyMedia:true}); }
      catch (e) { if (releaseId) throw e; tracking(e.message); }
      try { media = await web('/media/' + id); }
      catch (e) {
        if (!releaseId || e.status !== 404) throw e;
        // Static UI can update while the previous worker protects an active movie.
        // The established delivery API already validates current ownership and SHA.
        const record = await api('/finished-viewing/' + encodeURIComponent(releaseId));
        const release = releases.find(r => r.id === releaseId && r.video_sha256 === record.video_sha256);
        if (!release) throw Error('成品版本无法核验，请刷新后重试');
        sourceOnly = true;
        media = {id,release_id:releaseId,name:releaseId+'.mp4',folder:release.title,
          show_id:record.show_id,episode:record.episode,progress:record.progress,
          finished:record.finished,release,viewing_session_supported:true};
      }
      viewingLease();
      $('#playing-file').textContent = media.name;
      if (media.show_id) $('#playing-subtitle-library').href = '/subtitle-library?show=' + media.show_id;
      $('#danmu-q').value = media.show_id ? '' : media.folder;
      if (media.show_id) { await loadShow(); $('#danmu-q').value = show.show.title; } else await loadFolder();
      openPlayer();
    } catch (e) {
      screenState(`<b>${/404|未找到/.test(e.message) ? '找不到这个文件' : '打不开这个文件'}</b><span>${esc(e.message)}</span><a class="btn" href="/library">去片库</a>`);
    }
  })();
})();
