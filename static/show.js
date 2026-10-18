'use strict';
/* 作品页: play, set state, tick episodes, set up downloads — one page, no modal. */
(() => {
  shell('home');
  const sid = Number(location.pathname.split('/').pop());
  let d = null, epOpen = false, stopObserving;

  const RES = [['1080', '1080p'], ['720', '720p'], ['2160', '2160p'], ['', '不限']];
  const SUB = [['any_zh', '简繁均可'], ['chs', '简体'], ['cht', '繁体']];
  const opts = (list, v) => list.map(([k, n]) => `<option value="${k}"${k === v ? ' selected' : ''}>${n}</option>`).join('');
  const label = (list, v) => (list.find(([k]) => k === v) || [, v])[1];

  function epInfo(e, raw) {
    if (e.release) return '自制字幕 · ' + FinishedReleases.label(e.release) + (e.s === 'resume' ? ` · 看到 ${clock(e.position)}` : '');
    if (e.s === 'resume') return `<span class="track" style="--p:${Math.round(e.position / e.duration * 100)}%"><i></i></span><span class="num">${clock(e.position)} / ${clock(e.duration)}</span>`;
    if (e.s === 'downloading') return `<span class="track" style="--p:${e.progress}%"><i></i></span><span class="num">${e.progress}%</span>${e.download ? '<span>' + esc(e.download.reason) + '</span>' : ''}`;
    if (e.s === 'future') return e.date ? `${airDate(e.date)} 播出` : '';
    if (e.s === 'missing') return e.download ? esc(e.download.reason) + (e.download.source_error ? '；部分查源失败：' + esc(e.download.source_error) : '') + (e.download.retry_at ? ' · ' + new Date(e.download.retry_at * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) + ' 自动重试' : '') : raw && TASKS[raw.status] ? TASKS[raw.status] : '后台正在检查本集资源';
    if (e.s === 'aired') return '已播出';
    if (raw?.watch_method) return raw.watch_method === 'manual' ? '手动标记' : '播放器同步';
    return '';
  }

  function epRow(e) {
    const raw = d.episodes.find(x => x.episode === e.n);
    const isNext = d.board.next?.n === e.n;
    const play = e.media || e.release ? `<a class="btn small${isNext ? ' primary' : ''}" href="${watchHref(e)}" aria-label="第 ${e.n} 集${e.s === 'resume' ? '续播' : e.s === 'watched' ? '重播' : '播放'}">${icon('play')}<span class="lbl">${e.s === 'resume' ? '续播' : e.s === 'watched' ? '重播' : '播放'}</span></a>` : '';
    const tick = e.s === 'future' ? '' : e.s === 'watched'
      ? `<button class="btn small quiet" type="button" data-watch="${e.n}" data-finished="false" aria-label="第 ${e.n} 集改为未看">${icon('check')}<span class="lbl">已看</span></button>`
      : `<button class="btn small" type="button" data-watch="${e.n}" data-finished="true" aria-label="第 ${e.n} 集标记看完">${icon('check')}<span class="lbl">看完</span></button>`;
    return `<div class="ep-row" data-s="${e.s}"${isNext ? ' data-next' : ''}><span class="no"><span>${e.n}</span></span><span class="st">${EP_LABEL[e.s]}</span><span class="info">${epInfo(e, raw)}</span><span class="acts">${play}${tick}</span></div>`;
  }

  function episodes() {
    const eps = d.board.eps;
    if (!eps.length) return '<p class="muted">还没有分集信息</p>';
    let run = 0;
    while (run < eps.length && eps[run].s === 'watched') run++;
    const rest = eps.slice(run).map(epRow).join('');
    if (run < 2) return `<div class="ep-list">${eps.map(epRow).join('')}</div>`;
    return `<details class="ep-fold"${epOpen ? ' open' : ''}><summary><div class="ep-row" data-s="watched"><span class="no"><span>1–${run}</span></span><span class="st">已看</span><span class="info"></span><span class="acts">${icon('chevron')}</span></div></summary>
      <div class="ep-list">${eps.slice(0, run).map(epRow).join('')}</div></details><div class="ep-list" style="margin-top:var(--row-gap)">${rest}</div>`;
  }

  function autoDl(s, m) {
    const ready = m.confirmed && m.inventory_checked;
    const src = [m.mikan_id && '蜜柑', m.dmhy_keyword && '动漫花园'].filter(Boolean).join(' · ');
    const sub = s.cleanup_hold ? '已清理，不再下载' : !ready ? '需要先完成下载设置' : s.authorized ? '已开启 · ' + src : '关闭 · ' + src;
    const disabled = !s.authorized && (!ready || s.cleanup_hold);
    return `<div class="auto-dl"><span class="label"><b>自动下载</b><span id="dl-sub">${sub}</span></span>
      <label class="switch"><span class="sr">自动下载</span><input type="checkbox" id="authorize"${s.authorized ? ' checked' : ''}${disabled ? ' disabled' : ''}><span class="track"></span></label></div>
      ${!ready && !s.cleanup_hold ? '<a class="link" href="#download">去设置</a>' : ''}`;
  }

  function nextState(b) {
    if (b.next) return ticket({ title: d.show.title, next: b.next }).replace('class="ticket', 'class="ticket hero');
    const f = b.eps.find(e => e.s === 'future'), dl = b.eps.find(e => e.s === 'downloading'), miss = b.eps.find(e => e.s === 'missing');
    const line = dl ? `第 ${dl.n} 集下载中 ${dl.progress}%` : miss ? `第 ${miss.n} 集缺集` : f ? `第 ${f.n} 集${f.date ? ' · ' + airDate(f.date) : ''}` : b.eps.length && b.eps.every(e => e.s === 'watched') ? '全部看完' : '';
    return line ? `<div class="panel"><span class="muted">${dl || miss || !f ? '' : '下一集 '}</span><b>${line}</b></div>` : '';
  }

  function download(s, m) {
    const set = [m.mikan_id ? '蜜柑 ' + m.mikan_id : '', m.dmhy_keyword ? '动漫花园' : ''].filter(Boolean);
    const aside = m.confirmed ? `${label(RES, m.resolution ?? App.settings.resolution)} · ${label(SUB, m.subtitle || App.settings.subtitle)}` : '未设置';
    return `<details class="more" id="download"><summary>下载设置<span class="aside">${aside}</span>${icon('chevron')}</summary><div class="body">
      <div style="display:flex;flex-wrap:wrap;gap:var(--s-2)"><button class="btn small" type="button" id="prepare">自动准备</button><button class="btn small quiet" type="button" id="find-mikan">查找蜜柑条目</button></div>
      <div id="prepare-out"></div>
      <form id="mapping" class="form-grid">
        <label class="field wide"><span>发布别名（每行一个）</span><textarea name="aliases" rows="3">${esc((m.aliases || [s.title, s.original].filter(Boolean)).join('\n'))}</textarea></label>
        <label class="field"><span>蜜柑 ID</span><input name="mikan_id" inputmode="numeric" value="${esc(m.mikan_id || '')}"></label>
        <label class="field"><span>动漫花园关键词</span><input name="dmhy_keyword" value="${esc(m.dmhy_keyword || '')}"></label>
        <label class="field"><span>季号</span><input name="season" type="number" min="1" max="99" value="${m.season || 1}"></label>
        <label class="field"><span>集号偏移</span><input name="offset" type="number" min="-999" max="999" value="${m.offset || 0}"></label>
        <label class="field"><span>起始集</span><input name="start" type="number" min="1" max="999" value="${m.start || 1}"></label>
        <label class="field"><span>结束集</span><input name="end" type="number" min="1" max="999" value="${m.end || s.total || 999}"></label>
        <label class="field"><span>分辨率</span><select name="resolution">${opts(RES, m.resolution ?? App.settings.resolution)}</select></label>
        <label class="field"><span>字幕</span><select name="subtitle">${opts(SUB, m.subtitle || App.settings.subtitle)}</select></label>
        <label class="field wide"><span>字幕组</span><input name="groups" placeholder="全部" value="${esc((m.groups || App.settings.groups || []).join(', '))}"></label>
        <div class="wide"><button class="btn small" type="button" id="scan">检查已有文件</button><div id="inventory" style="margin-top:var(--s-2)"></div></div>
        <label class="check wide"><input type="checkbox" name="inventory_checked"${m.inventory_checked ? ' checked' : ''}>已核对已有文件</label>
        <label class="check wide"><input type="checkbox" name="confirmed"${m.confirmed ? ' checked' : ''}>已核对季号、集号与字幕</label>
        <div class="wide"><button class="btn primary" type="submit">保存下载设置</button></div>
      </form>
      ${d.sources.length ? `<table><thead><tr><th>来源</th><th>最近成功</th><th>异常</th></tr></thead><tbody>${d.sources.map(x => `<tr${x.error ? ' class="is-error"' : ''}><td>${x.kind === 'mikan' ? '蜜柑' : '动漫花园'}${x.enabled ? '' : '（停用）'}</td><td class="nowrap">${ago(x.last_success)}</td><td>${esc(x.error || x.archive_error || '')}</td></tr>`).join('')}</tbody></table>` : ''}
    </div></details>`;
  }

  function sourceReview() {
    const r = d.source_review, p = r?.policy || {};
    if (!r) return '';
    const samples = (r.samples || []).filter(s => !s.source_blocker), reviewed = samples.filter(s => (r.recommendation || []).includes(s.family));
    const candidates = [...new Map((r.candidates || []).map(c => [c.family.key,c])).values()];
    const names = keys => keys.map(k => samples.find(s => s.family === k)?.family_info.label || k).join('；');
    const status = {preparing:'核验种子',queued:'等待下载',analyzing:'提取证据',ready:'待评审',excluded:'已排除',error:'核验失败',analysis_error:'分析失败'};
    const options = (value, blank = true) => `${blank ? '<option value="">不设置</option>' : ''}${reviewed.map(s => `<option value="${s.family}"${s.family === value ? ' selected' : ''}>${esc(s.family_info.label)}</option>`).join('')}`;
    return `<details class="more" id="source-review"><summary>首集选源评测<span class="aside">${p.mode === 'selected' ? '主源已确认' : p.mode === 'sampling' ? (p.playback_family ? '首集可播放 · 评测中' : '评测中') : '半自动'}</span>${icon('chevron')}</summary><div class="body">
      <p>画质优先，其次更新及时性，最后字幕翻译。每种片源与压制版本取一集，简繁同版只取一次。正片仍一集一版。</p>
      <p>想看的新番通过作品、季集和旧文件核验后，后台自动找首集样本、提取字幕并由定时评审选主源和替补。证据不足会继续待评；旧番或首集不在授权范围时不会自动补首集。</p>
      ${p.playback_family ? '<p>首集完成后即可在追番播放，后续先沿用这一片源；评分完成后再确认主源和替补。</p>' : ''}
      ${r.sampling_limit?.available_families > 12 ? `<p>当前发现 ${r.sampling_limit.available_families} 种首集版本，自动取样最多12种、20GiB；超出范围的版本尚未比较，可在下方指定本次取样。</p>` : ''}
      ${r.fidelity === 'original' ? '<p><b>原片保真优先</b> 排除声明了超分、补帧或AI画面修复的版本；优先官方原始发布、保留帧率与纹理。1080p标签本身不证明原生制作。</p>' : ''}
      ${p.context ? `<p><b>原作核对重点</b> ${esc(p.context)}</p>` : ''}
      ${p.primary ? `<p><b>主源</b> ${esc(names([p.primary]))}<br><b>替补</b> ${esc(names(p.backups || []) || '未设置')}<br>等待主源 ${p.wait_hours} 小时后才用已评审替补补缺集。</p>` : ''}
      <button class="btn small" id="sample-sources" type="button"${!safelyEligible() ? ' disabled' : ''}>${samples.length ? '补查首集样本' : '开始首集多源评测'}</button>
      <button class="btn small quiet" id="refresh-source-review" type="button">刷新评测</button>
      <p><a class="link" href="/subtitle-reviews?show_id=${sid}">字幕对比记录</a> · <a class="link" href="/subtitle-reviews">全部字幕评审</a></p>
      ${candidates.length > 12 ? `<p>首集片源超过12种，请选择本次取样；已取样片源也计入12种上限。</p><div class="form-grid">${candidates.map(c => `<label class="check wide"><input type="checkbox" data-review-candidate="${c.id}"${samples.some(s => s.family === c.family.key) ? ' checked' : ''}>${esc(c.family.label)}</label>`).join('')}</div>` : ''}
      <p class="muted">评测下载保留单独副本，不计已看。字幕提取、截图和可用的本机OCR自动执行；翻译与画质评分需核对实物和日语/原作依据后记录。</p>
      ${samples.map(s => { const e = s.evidence || {}, v = (e.probe?.streams || []).find(x => x.codec_type === 'video');
        const file = name => `/api/shows/${sid}/source-review/${s.candidate_id}/file/${name}`;
        const pending = s.state === 'queued' ? (s.download?.status === 'complete' ? '下载完成 · 待分析' : s.download?.status === 'downloading' ? `${s.download.provider === 'qbit' ? 'qB' : '迅雷'}下载中` : s.download?.status === 'review' ? '下载待核查' : '等待下载提交') : status[s.state] || s.state;
        return `<details class="more"><summary>${esc(s.family_info.label)}<span class="aside">${s.processing_blocker ? '保真规则排除' : s.review?.decision === 'excluded' || s.review?.recommendation_eligible === false || s.review?.fidelity_qualification?.eligible === false ? '已排除' : s.review?.visual !== undefined ? `画质 ${s.review.visual} · 字幕 ${s.review.translation}` : esc(pending)}</span></summary><div class="body">
          <p>${esc(s.title)}</p><p class="muted">${esc(s.download?.status ? TASKS[s.download.status] || s.download.status : '')}${s.published ? ' · 发布 ' + ago(s.published) : ' · 发布时间未取得'}${s.release_delay_hours != null ? ` · 首播后 ${s.release_delay_hours} 小时发布` : ' · 首播延迟待核实'}${v ? ` · ${v.width}×${v.height} ${esc(v.codec_name)} · 平均总码率 ${(e.average_total_bitrate / 1e6).toFixed(2)} Mbps` : ''}</p>
          ${s.error || s.download?.error ? `<p class="warn-line">${esc(s.error || s.download.error)}</p>` : ''}
          ${s.processing_blocker ? `<p class="warn-line">${esc(s.processing_blocker)}。旧评分保留作历史，不参与当前选源。</p>` : ''}
          ${s.review?.evidence ? `<p><b>实评依据</b> ${esc(s.review.evidence)}</p><p class="muted">${esc(s.review.original_basis || '')} · ${s.review.confidence === 'high' ? '完整评测' : s.review.confidence === 'medium' ? '抽样评测，不能代表整季质量' : '资格核查'}</p>` : ''}
          ${s.state === 'ready' ? `<p>${(e.tracks || []).map(t => t.file ? `<a class="link" href="${file(t.file)}" target="_blank">${esc(t.label)} · ${t.metrics.cues}条字幕${t.metrics.chinese_cues ? ` · ${t.metrics.chinese_cues}条中文候选` : ''}</a>` : esc(t.label) + '（图形字幕）').join(' · ')}</p>
            <p>${(e.frames || []).map((f, i) => `<a class="link" href="${file(f.file)}" target="_blank">画面${i + 1}（${clock(f.seconds)}）</a>`).join(' · ')} · <a class="link" href="${file('packet.json')}" target="_blank">完整证据</a></p>
            ${e.ocr ? `<p class="muted">${e.ocr.file ? `<a class="link" href="${file(e.ocr.file)}" target="_blank">OCR字幕（估计时间，待校对）</a> · ` : ''}${esc(typeof e.ocr === 'string' ? e.ocr : e.ocr.note)}</p>` : ''}
            <form class="form-grid" data-source-review="${s.candidate_id}">
              <label class="field"><span>画质实评（0–100）</span><input name="visual" type="number" min="0" max="100" required value="${s.review.visual ?? ''}"></label>
              <label class="field"><span>翻译实评（0–100）</span><input name="translation" type="number" min="0" max="100" required value="${s.review.translation ?? ''}"></label>
              <label class="field wide"><span>画面、时间点、字幕差异证据</span><textarea name="evidence" rows="3" required>${esc(s.review.evidence || '')}</textarea></label>
              <label class="field wide"><span>日语原声/台词及原作依据（含来源）</span><textarea name="original_basis" rows="3" required>${esc(s.review.original_basis || '')}</textarea></label>
              <label class="check wide"><input type="checkbox" name="chinese_verified" required${s.review.chinese_verified ? ' checked' : ''}>已实际核对中文字幕及翻译</label>
              <label class="field"><span>置信度</span><select name="confidence"><option value="medium">中：抽样评测</option><option value="high"${s.review.confidence === 'high' ? ' selected' : ''}>高：完整评测</option></select></label>
              <div class="wide"><button class="btn small" type="submit">保存实物评审</button><button class="btn small quiet" type="button" data-exclude-source="${s.candidate_id}">按上述依据排除</button></div>
            </form>` : ''}
        </div></details>`; }).join('')}
      ${reviewed.length ? `<form id="choose-source" class="form-grid"><label class="field wide"><span>主源</span><select name="primary" required>${options(p.primary || r.recommendation[0],false)}</select></label>
        ${[0,1,2,3].map(i => `<label class="field wide"><span>替补 ${i + 1}</span><select name="backup${i}">${options(p.backups?.[i])}</select></label>`).join('')}
        <label class="field"><span>等主源最长小时数</span><input name="wait_hours" type="number" min="1" max="168" value="${p.wait_hours || 24}" required></label>
        <div class="wide"><button class="btn primary" type="submit">确认来源并自动追更</button></div></form>` : ''}
      ${!samples.length ? `<p class="muted">目前有 ${r.candidates.length} 个经作品、季集核验的首集候选。</p>` : ''}
    </div></details>`;
  }

  function safelyEligible() {
    const s = d.show, m = s.mapping;
    return s.selected && s.authorized && !s.cleanup_hold && m.confirmed && m.inventory_checked &&
      (['trial','watching'].includes(s.state) || (s.state === 'wish' && s.auto_download)) && (m.start || 1) === 1;
  }

  function render() {
    const s = d.show, m = s.mapping || {}, b = d.board;
    document.title = s.title + ' · 番屿';
    const files = d.episodes.filter(e => e.path);
    const officials = (s.metadata.infobox || []).filter(i => /官方网站|官方網站|website/i.test(i.key) && typeof i.value === 'string' && /^https:\/\//.test(i.value));
    const watched = b.eps.filter(e => e.s === 'watched').length, ready = b.eps.filter(e => e.s === 'ready' || e.s === 'resume').length;
    $('#main').innerHTML = `
      <section class="show-hero">
        ${cover(s.image, s.title)}
        <div>
          <a class="crumb" href="/">${icon('back')}追番</a>
          <h1>${esc(s.title)}</h1>
          ${s.original && s.original !== s.title ? `<p class="original">${esc(s.original)}</p>` : ''}
          <div class="facts">${s.score ? `<a class="bgm-score" href="https://bgm.tv/subject/${s.id}" target="_blank" rel="noopener" title="在 Bangumi 查看">Bangumi <b>${s.score}</b></a>` : ''}${s.air_date ? `<span>${esc(s.air_date)} 首播</span>` : ''}<span>${s.total ? `共 <b>${s.total}</b> 集` : '集数未定'}</span></div>
          <div class="tags">${(s.tags || []).slice(0, 6).map(t => `<span class="chip">${esc(t.name || t)}</span>`).join('')}</div>
        </div>
        <div class="aside"><div id="next-availability">${nextState(b)}</div>${autoDl(s, m)}</div>
        <div class="states"><div class="seg" role="group" aria-label="追番状态">${Object.entries(STATES).map(([k, v]) => `<button type="button" data-state="${k}" aria-pressed="${!!(s.selected && s.state === k)}">${v}</button>`).join('')}</div></div>
      </section>
      <div class="show-grid">
        <section id="episode-availability" aria-labelledby="eps-title">
          <h2 class="group-head" id="eps-title"><b>分集</b><span class="count">已看 ${watched}/${s.total || b.eps.length}${ready ? ' · 待看 ' + ready : ''}</span></h2>
          ${episodes()}
        </section>
        <div class="aside-col">
          <section aria-label="我的评价">
            <h2>我的评分</h2>
            <form id="feedback" style="display:grid;gap:var(--s-3)">
              <div class="rating" role="group" aria-label="评分 1 到 10">${Array.from({ length: 10 }, (_, i) => `<button type="button" data-rate="${i + 1}" aria-pressed="${s.rating === i + 1}" class="${s.rating >= i + 1 ? 'on' : ''}">${i + 1}</button>`).join('')}</div>
              <input type="hidden" name="rating" value="${s.rating || ''}">
              <label class="field"><span class="sr">感想</span><textarea name="feedback" rows="3" placeholder="感想">${esc(s.feedback)}</textarea></label>
              ${s.state === 'dropped' || s.state === 'paused' || s.drop_reason ? `<label class="field"><span>${s.state === 'paused' ? '暂搁原因' : '弃番原因'}</span><input name="drop_reason" value="${esc(s.drop_reason)}"></label>` : `<input type="hidden" name="drop_reason" value="">`}
              <div><button class="btn small" type="submit">保存</button></div>
            </form>
          </section>
          ${download(s, m)}
          ${sourceReview()}
          ${files.length ? `<details class="more"><summary>文件<span class="aside">${files.length} 个</span>${icon('chevron')}</summary><div class="body">${files.map(e => `<div class="inventory-item" style="grid-template-columns:56px minmax(0,1fr) auto"><span class="num">第 ${e.episode} 集</span><span>${esc(e.path.split(/[\\/]/).pop())}</span><button class="btn small quiet" type="button" data-copy="${e.episode}">复制路径</button></div>`).join('')}</div></details>` : ''}
          ${d.candidates.length ? `<details class="more"><summary>候选记录<span class="aside">${d.candidates.length}</span>${icon('chevron')}</summary><div class="body"><table class="cand"><tbody>${d.candidates.slice(0, 40).map(c => `<tr><td>${esc(c.title)}</td><td class="nowrap">${esc(c.status)}${c.reason ? '<br><span class="muted">' + esc(c.reason) + '</span>' : ''}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
          <details class="more"><summary>简介与链接${icon('chevron')}</summary><div class="body">
            ${s.summary ? `<p class="summary-text">${esc(s.summary)}</p>` : ''}
            <div class="links"><a class="btn small" href="https://bgm.tv/subject/${s.id}" target="_blank" rel="noopener">Bangumi ${icon('external')}</a>${officials.map(i => `<a class="btn small" href="${https(i.value)}" target="_blank" rel="noopener">官网 ${icon('external')}</a>`).join('')}<a class="btn small quiet" href="/library?tab=history&amp;show=${s.id}">观看记录</a></div>
            <div><button class="btn small quiet" type="button" id="refresh">更新资料</button></div>
          </div></details>
        </div>
      </div>`;
    if (location.hash === '#download') $('#download').open = true;
    if (location.hash === '#source-review') $('#source-review').open = true;
  }

  async function load() {
    try {
      await boot();
      const [detail, releases] = await Promise.all([api('/shows/' + sid),
        FinishedReleases.load({verifyMedia: true}).catch(e => { toast(e.message); return []; })]);
      d = detail;
      applyAiringSchedule(d);
      const baseline = d.board;
      d.board = FinishedReleases.applyBoard(d.show, baseline, releases);
      render();
      stopObserving?.();
      stopObserving = FinishedReleases.observe(next => {
        epOpen = !!$('.ep-fold')?.open;
        d.board = FinishedReleases.applyBoard(d.show, baseline, next);
        const watched = d.board.eps.filter(e => e.s === 'watched').length;
        const ready = d.board.eps.filter(e => e.s === 'ready' || e.s === 'resume').length;
        $('#episode-availability').innerHTML = `<h2 class="group-head" id="eps-title"><b>分集</b><span class="count">已看 ${watched}/${d.show.total || d.board.eps.length}${ready ? ' · 待看 ' + ready : ''}</span></h2>${episodes()}`;
        $('#next-availability').innerHTML = nextState(d.board);
      }, {initial: releases});
    } catch (e) {
      $('#main').innerHTML = `<div class="empty"><h2>${/404|没有此条目/.test(e.message) ? '没有这部作品' : '读取失败'}</h2><p>${esc(e.message)}</p><a class="btn" href="/">回到追番</a></div>`;
    }
  }

  async function setState(state, previous) {
    await api(`/shows/${sid}/state`, { state });
    await load();
    toast('已设为' + STATES[state], previous && previous !== state ? { label: '撤销', run: () => act(null, () => setState(previous)) } : null);
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('button, a[href="#download"]');
    if (!b) return;
    if (b.matches('a[href="#download"]')) { $('#download').open = true; return; }
    if (b.dataset.excludeSource) return act(b, async () => {
      const f = b.closest('form'), data = Object.fromEntries(new FormData(f));
      if (!data.evidence.trim() || !data.original_basis.trim()) throw Error('请填写排除依据和实际核查来源');
      await api(`/shows/${sid}/source-review/review`, {candidate_id:Number(b.dataset.excludeSource),review:{
        decision:'excluded',evidence:data.evidence,original_basis:data.original_basis,reviewer:'用户手动评审'}});
      await load(); $('#source-review').open = true; toast('已排除此样本，不纳入主源或替补');
    });
    if (b.dataset.state) return act(b, () => setState(b.dataset.state, d.show.selected ? d.show.state : null));
    if (b.dataset.watch) return act(b, async () => {
      const n = Number(b.dataset.watch), fin = b.dataset.finished === 'true';
      epOpen = !!b.closest('.ep-fold');
      const r = await api(`/shows/${sid}/watch`, { episode: n, finished: fin });
      await load();
      $(`[data-watch="${n}"]`)?.focus();
      toast(`第 ${n} 集${fin ? '已看' : '改为未看'}${r.state ? ' · 已设为' + STATES[r.state] : ''}`, { label: '撤销', run: () => act(null, async () => { await api(`/shows/${sid}/watch`, { episode: n, finished: !fin }); await load(); }) });
    });
    if (b.dataset.rate) {
      const v = Number(b.dataset.rate), form = $('#feedback');
      const cur = Number(form.rating.value) === v ? '' : v;
      form.rating.value = cur;
      $$('[data-rate]').forEach(x => { x.classList.toggle('on', cur && Number(x.dataset.rate) <= cur); x.setAttribute('aria-pressed', String(Number(x.dataset.rate) === cur)); });
      return;
    }
    if (b.dataset.copy) return act(b, async () => {
      await navigator.clipboard.writeText(d.episodes.find(x => x.episode === Number(b.dataset.copy)).path);
      toast('已复制路径');
    });
    if (b.dataset.mid) { $('#mapping [name=mikan_id]').value = b.dataset.mid; toast('已填入蜜柑 ID ' + b.dataset.mid); return; }
    if (b.dataset.link) return act(b, async () => {
      const row = b.closest('[data-path]');
      await api(`/shows/${sid}/link-local`, { episode: Number(row.querySelector('input').value), path: row.dataset.path });
      b.textContent = '已关联';
      b.disabled = true;
    });
    switch (b.id) {
      case 'sample-sources': return act(b, async () => {
        const choices = $$('[data-review-candidate]');
        const ids = choices.filter(x => x.checked).map(x => Number(x.dataset.reviewCandidate));
        if (choices.length && !ids.length) throw Error('请至少选择一种首集片源');
        await api(`/shows/${sid}/source-review/start`, choices.length ? {candidate_ids:ids} : {});
        await load(); $('#source-review').open = true; toast('已核验首集并加入迅雷评测队列');
      });
      case 'refresh-source-review': return act(b, async () => { await load(); $('#source-review').open = true; });
      case 'refresh': return act(b, async () => { await api(`/shows/${sid}/refresh`, {}); toast('正在更新资料'); });
      case 'prepare': return act(b, async () => {
        $('#prepare-out').innerHTML = '<p class="muted">正在查找来源…</p>';
        const plan = await api(`/shows/${sid}/prepare`, {});
        for (const [k, v] of Object.entries(plan.mapping)) {
          const f = $(`#mapping [name="${k}"]`);
          if (f && f.type !== 'checkbox') f.value = Array.isArray(v) ? v.join(k === 'aliases' ? '\n' : ', ') : v;
        }
        $('#prepare-out').innerHTML = `<div class="sample"><b>${esc(plan.source.title || plan.source.id || '')}</b>${plan.preview.map(t => `<span>${esc(t)}</span>`).join('') || '<span>没有匹配的发布</span>'}<span>已有文件 ${plan.inventory_count} 个</span></div>${plan.warning ? `<p class="warn-line">${esc(plan.warning)}</p>` : ''}`;
      });
      case 'find-mikan': return act(b, async () => {
        const list = await api(`/shows/${sid}/mikan`);
        $('#prepare-out').innerHTML = list.length ? `<div class="list-rows">${list.map(x => `<div class="list-row"><a href="${https(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.id)} ${icon('external')}</a><button class="btn small" type="button" data-mid="${esc(x.id)}">用 ${esc(x.id)}</button></div>`).join('')}</div>` : '<p class="muted">蜜柑没有匹配条目</p>';
      });
      case 'scan': return act(b, async () => {
        const aliases = $('#mapping [name=aliases]').value.split('\n').map(x => x.trim()).filter(Boolean);
        const files = await api(`/shows/${sid}/inventory`, { aliases });
        $('#inventory').innerHTML = files.length ? files.map(f => `<div class="inventory-item" data-path="${esc(f.path)}"><span>${esc(f.path)}<br><span class="muted">${gib(f.size)}</span></span><label><span class="sr">第几集</span><input type="number" min="1" max="999" placeholder="集"></label><button class="btn small" type="button" data-link="1">关联</button></div>`).join('') : '<p class="muted">没有匹配的已有文件</p>';
      });
    }
  });

  document.addEventListener('change', e => {
    if (e.target.id !== 'authorize') return;
    const box = e.target;
    act(box, async () => {
      try { await api(`/shows/${sid}/authorize`, { enabled: box.checked }); }
      catch (err) { box.checked = !box.checked; throw err; }
      await load();
      toast(box.checked ? '已开启自动下载' : '已关闭自动下载');
    });
  });

  document.addEventListener('submit', e => {
    e.preventDefault();
    const f = e.target, data = Object.fromEntries(new FormData(f)), btn = f.querySelector('[type=submit]');
    if (f.dataset.sourceReview) return act(btn, async () => {
      await api(`/shows/${sid}/source-review/review`, {candidate_id:Number(f.dataset.sourceReview),review:{
        visual:Number(data.visual),translation:Number(data.translation),evidence:data.evidence,original_basis:data.original_basis,
        chinese_verified:f.chinese_verified.checked,confidence:data.confidence,reviewer:'用户手动评审'}});
      await load(); $('#source-review').open = true; toast('评审已保存，可确认主源和替补');
    });
    if (f.id === 'choose-source') return act(btn, async () => {
      await api(`/shows/${sid}/source-review/choose`, {primary:data.primary,backups:[0,1,2,3].map(i=>data['backup'+i]).filter(Boolean),wait_hours:Number(data.wait_hours)});
      await load(); $('#source-review').open = true; toast('主源和替补已确认，后续自动补缺集');
    });
    if (f.id === 'feedback') return act(btn, async () => {
      await api(`/shows/${sid}/feedback`, { rating: data.rating ? Number(data.rating) : null, feedback: data.feedback, drop_reason: data.drop_reason || '' });
      toast('已保存');
    });
    if (f.id === 'mapping') return act(btn, async () => {
      for (const k of ['season', 'offset', 'start', 'end']) data[k] = Number(data[k]);
      data.aliases = data.aliases.split('\n').map(x => x.trim()).filter(Boolean);
      data.groups = data.groups.split(/[,，]/).map(x => x.trim()).filter(Boolean);
      data.confirmed = f.confirmed.checked;
      data.inventory_checked = f.inventory_checked.checked;
      await api(`/shows/${sid}/mapping`, data);
      await load();
      $('#download').open = true;
      toast('下载设置已保存');
    });
  });

  load();
  addEventListener('pageshow', e => { if (e.persisted) load(); });
})();
