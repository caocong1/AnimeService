'use strict';
((root) => {
  function parseTime(value) {
    const s = String(value).trim();
    if (!/^\d+(?:\.\d+)?$/.test(s) && !/^\d+:\d{1,2}(?:\.\d+)?$/.test(s) && !/^\d+:\d{1,2}:\d{1,2}(?:\.\d+)?$/.test(s)) throw Error('时间请填写秒数、分:秒或时:分:秒');
    const p = s.split(':').map(Number);
    if (p.length > 1 && (p.at(-1) >= 60 || (p.length === 3 && p[1] >= 60))) throw Error('分、秒字段必须小于60');
    const result = p.reduce((sum, x) => sum * 60 + x, 0);
    if (!Number.isFinite(result)) throw Error('时间无效');
    return result;
  }
  function timeText(seconds) {
    const ms = Math.max(0, Math.round(seconds * 1000)), sec = Math.floor(ms / 1000);
    return `${String(Math.floor(sec / 3600)).padStart(2,'0')}:${String(Math.floor(sec / 60) % 60).padStart(2,'0')}:${String(sec % 60).padStart(2,'0')}.${String(ms % 1000).padStart(3,'0')}`;
  }
  function dimensions(width, height, requested, format) {
    let w = requested || width, h = Math.max(2, Math.round(w * height / width));
    if (format === 'mp4') { w = Math.max(2, Math.floor(w / 2) * 2); h = Math.max(2, Math.floor(h / 2) * 2); }
    return {width:w, height:h};
  }
  function mount({id, player, audio, request}) {
    const el = name => document.getElementById('clip-' + name), dialog = el('dialog'), video = el('video'), canvas = el('crop-canvas');
    const base = `/media/${id}/clips`, storage = `fanyu-clip-job:${id}`;
    let info = null, job = null, timer = null, previewEnd = null, drag = null, closed = false, submitting = false, restoreTime = 0;
    const fail = e => { el('error').textContent = e.message; };
    const getTime = name => parseTime(el(name).value);
    function setTime(name, value) {
      value = Math.max(0, Math.min(info.duration, value));
      el(name).value = timeText(value); el(name + '-range').value = String(value); update();
    }
    function resetCrop() { el('x').value = 0; el('y').value = 0; el('cw').value = info.width; el('ch').value = info.height; }
    function crop() { return el('crop').checked ? {x:Number(el('x').value), y:Number(el('y').value), width:Number(el('cw').value), height:Number(el('ch').value)} : null; }
    function payload() {
      return {start:getTime('start'), end:getTime('end'), format:el('format').value, audio:el('audio').checked,
        audio_index:el('audio-track').value === '' ? null : Number(el('audio-track').value),
        width:Number(el('size').value === 'custom' ? el('width').value : el('size').value), fps:Number(el('fps').value),
        quality:el('quality').value, loop:el('loop').checked, crop:crop()};
    }
    function validate(p) {
      const limit = p.format === 'gif' ? 30 : 300;
      if (!(0 <= p.start && p.start < p.end && p.end <= info.duration && p.end - p.start >= .1 && p.end - p.start <= limit)) throw Error(`请选择视频内的有效起止时间；${p.format.toUpperCase()}最长${limit}秒`);
      if (!Number.isInteger(p.width) || (p.width !== 0 && (p.width < 64 || p.width > 3840))) throw Error('输出宽度为64–3840像素');
      const c = p.crop;
      if (c && (!Object.values(c).every(Number.isInteger) || c.x < 0 || c.y < 0 || c.width < 2 || c.height < 2 || c.x + c.width > info.width || c.y + c.height > info.height)) throw Error('裁剪区域超出画面，请重新框选或填写坐标');
      const size = dimensions(c?.width || info.width, c?.height || info.height, p.width, p.format);
      if (size.width > 3840 || size.height > 3840 || (p.format === 'gif' && size.width * size.height > 1280*720)) throw Error('输出尺寸过大，请缩小宽度；GIF建议480或640像素');
      return size;
    }
    function update() {
      if (!info) return;
      const gif = el('format').value === 'gif', busy = submitting || ['queued','running','cancelling'].includes(job?.state);
      el('custom-label').hidden = el('size').value !== 'custom';
      el('audio').disabled = gif || !info.audio_tracks.length;
      el('audio-label').hidden = gif || !info.audio_tracks.length;
      el('audio-track').disabled = !el('audio').checked;
      el('quality-label').hidden = gif; el('loop-label').hidden = !gif;
      el('crop-fields').hidden = el('crop-hint').hidden = canvas.hidden = !el('crop').checked;
      for (const opt of el('fps').options) opt.disabled = gif && (Number(opt.value) === 0 || Number(opt.value) > 30);
      if (gif && (el('fps').value === '0' || Number(el('fps').value) > 30)) el('fps').value = '15';
      try {
        const p = payload(), size = validate(p);
        el('summary').textContent = `${(p.end - p.start).toFixed(3)} 秒 · ${size.width} × ${size.height} px · ${p.format.toUpperCase()} · ${gif || !p.audio || !info.audio_tracks.length ? '无声' : '含所选音轨'}`;
        el('submit').disabled = busy;
      } catch (e) { el('summary').textContent = e.message; el('submit').disabled = true; }
      draw();
    }
    function draw() {
      if (!info || canvas.hidden) return;
      const r = video.getBoundingClientRect(), scale = Math.min(r.width / info.width, r.height / info.height);
      if (!scale) return;
      const w = info.width * scale, h = info.height * scale;
      canvas.style.left = `${(r.width - w) / 2}px`; canvas.style.top = `${(r.height - h) / 2}px`;
      canvas.style.width = `${w}px`; canvas.style.height = `${h}px`; canvas.width = Math.round(w); canvas.height = Math.round(h);
      const ctx = canvas.getContext('2d'), c = crop();
      ctx.fillStyle = 'rgba(0,0,0,.5)'; ctx.fillRect(0,0,w,h);
      if (c) { ctx.clearRect(c.x*scale,c.y*scale,c.width*scale,c.height*scale); ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.strokeRect(c.x*scale,c.y*scale,c.width*scale,c.height*scale); }
    }
    function point(e) {
      const r = canvas.getBoundingClientRect();
      return {x:Math.round(Math.max(0, Math.min(info.width, (e.clientX-r.left)/r.width*info.width))), y:Math.round(Math.max(0, Math.min(info.height, (e.clientY-r.top)/r.height*info.height)))};
    }
    canvas.addEventListener('pointerdown', e => { video.pause(); drag = point(e); canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener('pointermove', e => {
      if (!drag) return;
      const p = point(e), x = Math.min(drag.x,p.x), y = Math.min(drag.y,p.y);
      el('x').value = x; el('y').value = y; el('cw').value = Math.max(2,Math.abs(drag.x-p.x)); el('ch').value = Math.max(2,Math.abs(drag.y-p.y)); update();
    });
    for (const event of ['pointerup','pointercancel']) canvas.addEventListener(event, () => { drag = null; });
    const observer = new ResizeObserver(draw); observer.observe(video);
    video.addEventListener('timeupdate', () => { if (previewEnd !== null && video.currentTime >= previewEnd) { video.pause(); previewEnd = null; } });
    video.addEventListener('loadedmetadata', () => { video.currentTime = Math.min(restoreTime, video.duration || restoreTime); draw(); });
    video.addEventListener('error', () => { el('error').textContent = '浏览器无法预览这个编码；仍可填写时间并导出MP4/GIF。'; });
    for (const name of ['start','end']) {
      el(name).addEventListener('input', () => { try { el(name+'-range').value = String(getTime(name)); } catch (_) {} update(); });
      el(name+'-range').addEventListener('input', () => { const t = Number(el(name+'-range').value); setTime(name,t); video.pause(); previewEnd = null; video.currentTime = t; });
      el('mark-'+name).addEventListener('click', () => setTime(name,video.currentTime));
    }
    el('main-position').addEventListener('click', () => { setTime('start',player()?.currentTime || 0); setTime('end',Math.min(info.duration,getTime('start')+10)); video.currentTime = getTime('start'); });
    el('preview-range').addEventListener('click', () => { try { const p = payload(); validate(p); previewEnd = p.end; video.currentTime = p.start; video.play().catch(fail); } catch (e) { fail(e); } });
    el('crop-reset').addEventListener('click', () => { resetCrop(); update(); });
    el('form').addEventListener('input', update);
    el('format').addEventListener('change', () => { if (el('format').value === 'gif') { el('size').value = '640'; el('fps').value = '15'; } update(); });
    el('crop').addEventListener('change', () => {
      if (el('crop').checked) video.pause(); update();
      if (el('crop').checked) video.closest('.clip-preview').scrollIntoView({block:'nearest'});
    });
    function renderJob() {
      if (!job) return;
      el('job').hidden = false; el('progress').value = job.progress;
      const busy = ['queued','running','cancelling'].includes(job.state);
      el('cancel').hidden = !busy; el('cancel').disabled = job.state === 'cancelling';
      const text = {queued:'等待导出',running:`正在导出 ${job.progress}%`,cancelling:'正在取消…',cancelled:'导出已取消',complete:'导出完成，可预览或下载',failed:'导出失败，请重试；也可缩短片段或减小尺寸',interrupted:'上次导出因服务重启中断，请重新导出'};
      el('status').textContent = text[job.state] || '状态未知';
      el('result').hidden = job.state !== 'complete';
      if (job.state === 'complete') {
        const url = `/api/web${base}/${job.id}/file`, gif = job.options.format === 'gif';
        el('download').href = url + '?download=true'; el('download').download = job.name;
        el('result-video').hidden = gif; el('result-gif').hidden = !gif;
        const target = el(gif ? 'result-gif' : 'result-video');
        if (target.getAttribute('src') !== url) target.src = url;
      }
      update();
      return busy;
    }
    async function poll() {
      if (!job || closed) return;
      try { job = await request(`${base}/${job.id}`); if (renderJob()) timer = setTimeout(poll,1000); }
      catch (e) { fail(e); if (e.status !== 404 && !closed) timer = setTimeout(poll,4000); }
    }
    el('form').addEventListener('submit', async e => {
      e.preventDefault(); if (submitting || ['queued','running','cancelling'].includes(job?.state)) return;
      el('error').textContent = '';
      try {
        const p = payload(); validate(p); submitting = true; update();
        job = await request(base,p);
        try { localStorage.setItem(storage,job.id); } catch (_) {}
        clearTimeout(timer); renderJob(); timer = setTimeout(poll,500);
      } catch (e) { fail(e); } finally { submitting = false; update(); }
    });
    el('cancel').addEventListener('click', async () => { try { job = await request(`${base}/${job.id}/cancel`,{}); renderJob(); } catch (e) { fail(e); } });
    el('close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { video.pause(); el('result-video').pause(); player()?.pause(); document.getElementById('open-clip').focus(); });
    document.getElementById('open-clip').addEventListener('click', async () => {
      if (dialog.open) return;
      const art = player(); art?.pause(); dialog.showModal(); el('error').textContent = '';
      if (info) { draw(); return; }
      try {
        info = await request(base); el('loading').hidden = true; el('form').hidden = false;
        for (const name of ['start','end']) el(name+'-range').max = String(info.duration);
        const current = Math.max(0,Math.min(info.duration-.1,art?.currentTime || 0));
        setTime('start',current); setTime('end',Math.min(info.duration,current+10)); resetCrop();
        el('audio-track').replaceChildren(...info.audio_tracks.map(t => new Option(t.label,String(t.index))));
        const selected = info.audio_tracks.find(t => String(t.index) === String(audio())) || info.audio_tracks.find(t => t.default) || info.audio_tracks[0];
        if (selected) el('audio-track').value = String(selected.index);
        else el('audio').checked = false;
        restoreTime = current; video.src = art?.video?.currentSrc || `/api/web/media/${id}/stream`;
        update();
        let saved; try { saved = localStorage.getItem(storage); } catch (_) {}
        if (saved && /^[0-9a-f]{32}$/.test(saved)) { job = {id:saved}; poll(); }
      } catch (e) { info = null; el('loading').textContent = '视频信息读取失败，关闭后可重新尝试。'; fail(e); }
    });
    return {close() { closed = true; clearTimeout(timer); observer.disconnect(); video.pause(); el('result-video').pause(); }};
  }
  const api = {parseTime,timeText,dimensions,mount};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.ClipExport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
