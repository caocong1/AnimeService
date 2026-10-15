'use strict';
/* Native video remains the decoder, clock and HDR output. The optional scaler is
   confined to tagged 8-bit SDR; it never changes the source or playback speed. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PictureQuality = api;
})(typeof globalThis === 'object' ? globalThis : this, () => {
  const MODES = {native: '原片 · 原生显示', pixel: '原像素 · 不放大', lanczos: '精细缩放 · SDR'};
  function geometry(sw, sh, width, height, dpr = 1, mode = 'native') {
    if (![sw, sh, width, height, dpr].every(x => Number.isFinite(x) && x > 0)) return null;
    const fit = Math.min(width * dpr / sw, height * dpr / sh);
    const scale = mode === 'pixel' ? Math.min(1, fit) : fit;
    const w = Math.max(1, Math.min(Math.floor(width * dpr), Math.round(sw * scale)));
    const h = Math.max(1, Math.min(Math.floor(height * dpr), Math.round(sh * scale)));
    return {width: w, height: h, cssWidth: w / dpr, cssHeight: h / dpr, scale};
  }
  function eligible(p) {
    return !!p?.sdr_scaler_eligible && p.bit_depth === 8 && p.dynamic_range === 'SDR';
  }
  const vertex = `#version 300 es
    in vec2 position; out vec2 uv;
    void main(){ uv=vec2((position.x+1.0)*0.5,(1.0-position.y)*0.5); gl_Position=vec4(position,0,1); }`;
  const fragment = `#version 300 es
    precision highp float; uniform sampler2D frame; uniform vec2 sourceSize;
    in vec2 uv; out vec4 colour;
    float kernel(float v) {
      v=abs(v); if(v<0.00001) return 1.0; if(v>=3.0) return 0.0;
      float p=3.141592653589793*v; return sin(p)*sin(p/3.0)/(p*p/3.0);
    }
    void main(){
      vec2 p=uv*sourceSize-0.5, base=floor(p), f=p-base;
      vec3 sum=vec3(0); float total=0.0;
      for(int y=-2;y<=3;y++) for(int x=-2;x<=3;x++) {
        float w=kernel(float(x)-f.x)*kernel(float(y)-f.y);
        vec2 at=clamp(base+vec2(x,y)+0.5,vec2(0.5),sourceSize-0.5)/sourceSize;
        sum+=texture(frame,at).rgb*w; total+=w;
      }
      // Limit overshoot to the neighbouring pixels: no invented sharpness or halos.
      vec3 a=texture(frame,(clamp(base,vec2(0),sourceSize-1.0)+0.5)/sourceSize).rgb;
      vec3 b=texture(frame,(clamp(base+vec2(1,0),vec2(0),sourceSize-1.0)+0.5)/sourceSize).rgb;
      vec3 c=texture(frame,(clamp(base+vec2(0,1),vec2(0),sourceSize-1.0)+0.5)/sourceSize).rgb;
      vec3 d=texture(frame,(clamp(base+vec2(1,1),vec2(0),sourceSize-1.0)+0.5)/sourceSize).rgb;
      colour=vec4(clamp(sum/total,min(min(a,b),min(c,d)),max(max(a,b),max(c,d))),1);
    }`;
  class Scaler {
    constructor(video, parent, failure) {
      this.video = video; this.failure = failure; this.active = false; this.closed = false;
      this.canvas = document.createElement('canvas'); this.canvas.className = 'picture-canvas';
      this.canvas.hidden = true; this.canvas.setAttribute('aria-hidden', 'true');
      parent.insertBefore(this.canvas, video.nextSibling);
      this.canvas.addEventListener('webglcontextlost', e => { e.preventDefault(); this.stop(); failure('显卡显示通路已重置，已恢复原生显示'); });
    }
    init() {
      if (this.gl) return;
      const gl = this.canvas.getContext('webgl2', {alpha: false, antialias: false, depth: false, stencil: false, preserveDrawingBuffer: false});
      if (!gl) throw Error('此浏览器无法启用精细缩放');
      this.gl = gl;
      if ('drawingBufferColorSpace' in gl) gl.drawingBufferColorSpace = 'srgb';
      if ('unpackColorSpace' in gl) gl.unpackColorSpace = 'srgb';
      const compile = (type, source) => {
        const shader = gl.createShader(type); gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) { gl.deleteShader(shader); throw Error('精细缩放初始化失败'); }
        return shader;
      };
      const v = compile(gl.VERTEX_SHADER, vertex), f = compile(gl.FRAGMENT_SHADER, fragment);
      this.program = gl.createProgram(); gl.attachShader(this.program, v); gl.attachShader(this.program, f); gl.linkProgram(this.program);
      gl.deleteShader(v); gl.deleteShader(f);
      if (!gl.getProgramParameter(this.program, gl.LINK_STATUS)) throw Error('精细缩放初始化失败');
      gl.useProgram(this.program);
      this.buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);
      const attr = gl.getAttribLocation(this.program, 'position'); gl.enableVertexAttribArray(attr); gl.vertexAttribPointer(attr,2,gl.FLOAT,false,0,0);
      this.texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this.size = gl.getUniformLocation(this.program, 'sourceSize');
      this.limit = Math.min(4096, gl.getParameter(gl.MAX_TEXTURE_SIZE), gl.getParameter(gl.MAX_RENDERBUFFER_SIZE));
    }
    draw() {
      if (!this.active || this.closed || this.video.readyState < 2) return;
      try {
        const {gl, video, canvas} = this;
        gl.viewport(0,0,canvas.width,canvas.height);
        gl.uniform2f(this.size,video.videoWidth,video.videoHeight);
        gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,video);
        gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
        if (gl.getError() !== gl.NO_ERROR) throw Error('显卡无法稳定绘制，已恢复原生显示');
        canvas.hidden = false;
      } catch (e) { this.stop(); this.failure(e.message); }
    }
    start(g) {
      this.init();
      if (g.width > this.limit || g.height > this.limit || this.video.videoWidth > this.limit || this.video.videoHeight > this.limit)
        throw Error('当前尺寸超出精细缩放范围，保留原生显示');
      if (this.canvas.width !== g.width) this.canvas.width = g.width;
      if (this.canvas.height !== g.height) this.canvas.height = g.height;
      this.canvas.style.width = g.cssWidth + 'px'; this.canvas.style.height = g.cssHeight + 'px';
      if (!this.active) {
        this.active = true; this.draw();
        const tick = () => { this.callback = null; if (!this.active || this.closed) return; this.draw(); if (this.active) this.callback = this.video.requestVideoFrameCallback(tick); };
        this.callback = this.video.requestVideoFrameCallback(tick);
      } else this.draw();
    }
    stop() {
      this.active = false; this.canvas.hidden = true;
      if (this.callback != null) this.video.cancelVideoFrameCallback(this.callback);
      this.callback = null;
    }
    close() {
      this.closed = true; this.stop();
      if (this.gl) { this.gl.deleteTexture(this.texture); this.gl.deleteBuffer(this.buffer); this.gl.deleteProgram(this.program); }
      this.canvas.remove();
    }
  }
  function mount(art, {request, select, note, details}) {
    const video = art.video, parent = video.parentElement;
    let source = null, mode = 'native', fallback = '', scaler = null, closed = false, failed = false, lastQuality = null, pip = false;
    try { const saved = localStorage.getItem('fanyu-picture-mode'); if (MODES[saved]) mode = saved; } catch (_) {}
    const fail = message => { failed = true; fallback = message; scaler?.stop(); update(); };
    function settings() {
      const opt = {name: 'picture', html: '画面', tooltip: MODES[mode], width: 250,
        selector: Object.entries(MODES).map(([value, html]) => ({value, html, default: value === mode})),
        onSelect: item => { choose(item.value); return MODES[mode]; }};
      if (art.setting.find('picture')) art.setting.update(opt); else art.setting.add(opt);
    }
    function choose(value) {
      if (!MODES[value]) return;
      mode = value; failed = false; fallback = ''; lastQuality = null;
      try { localStorage.setItem('fanyu-picture-mode', mode); } catch (_) {}
      settings(); update();
    }
    function update() {
      if (closed) return;
      select.value = mode;
      const rect = parent.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      const g = geometry(video.videoWidth, video.videoHeight, rect.width, rect.height, dpr, mode);
      // Size the actual native surface in device pixels. No transform, cropping or CSS filter.
      if (g) { video.style.width = g.cssWidth + 'px'; video.style.height = g.cssHeight + 'px'; }
      if (mode !== 'lanczos' || !eligible(source) || !g || g.scale <= 1 || g.scale > 2.5 || failed || !video.requestVideoFrameCallback || document.hidden || pip) {
        scaler?.stop();
      } else {
        try { scaler ??= new Scaler(video, parent, fail); scaler.start(g); }
        catch (e) { failed = true; fallback = e.message; scaler?.stop(); }
      }
      const q = video.getVideoPlaybackQuality?.();
      const nativeReason = pip ? '画中画使用原生显示' : failed ? fallback : !source ? '正在读取原片参数' : !eligible(source) ? '此原片保留原生色彩通路' : !video.requestVideoFrameCallback ? '浏览器不支持逐帧显示' : g?.scale <= 1 ? '当前无需放大，使用原生显示' : g?.scale > 2.5 ? '放大比例过高，使用原生显示' : '';
      note.textContent = `${scaler?.active ? 'Lanczos 3 精细缩放' : '原生显示'} · ${g ? `${g.width} × ${g.height} 显示像素` : '等待视频'}${mode === 'pixel' && g?.scale < 1 ? ' · 空间不足，按比例缩小' : ''}${mode === 'lanczos' && !scaler?.active ? ' · ' + nativeReason : ''}`;
      const lines = ['视频原码流直出 / 复制转封装；未降码率、未降低分辨率。'];
      if (source) {
        lines.push(`原片：${source.width} × ${source.height} · ${(source.codec_name || '未知编码').toUpperCase()}${source.profile ? ' / ' + source.profile : ''} · ${source.bit_depth ? source.bit_depth + '-bit' : '位深未标注'} · ${source.fps ? source.fps.toFixed(3) + ' fps' : '帧率未标注'}`);
        lines.push(`色彩：${source.color_primaries || '色域未标注'} / ${source.color_transfer || '传递函数未标注'} / ${source.color_space || '矩阵未标注'} · ${source.color_range === 'tv' ? '有限范围' : source.color_range === 'pc' ? '全范围' : '范围未标注'} · ${source.dynamic_range === 'unknown' ? '动态范围未知' : source.dynamic_range}`);
        if (source.dynamic_range !== 'SDR' || source.bit_depth > 8) lines.push('10-bit / HDR 的最终层次与色彩取决于浏览器、系统 HDR 设置和屏幕；此页面不能证明端到端完美还原。');
      }
      lines.push(`实际解码：${video.videoWidth ? video.videoWidth + ' × ' + video.videoHeight : '尚未输出画面'}${q ? ` · 总帧 ${q.totalVideoFrames} / 掉帧 ${q.droppedVideoFrames}` : ' · 浏览器未提供掉帧统计'}`);
      if (g) lines.push(`缩放：${g.scale.toFixed(3)} 倍 · 屏幕像素比 ${dpr}；原像素模式在空间足够时一显示图像像素对应一屏幕像素。`);
      if (source?.sample_aspect_ratio && !['1:1','1/1'].includes(source.sample_aspect_ratio)) lines.push('非方形或未标注采样比例：按浏览器解码后的宽高比显示，不作为编码采样点 1:1 验证。');
      lines.push('精细缩放仅处理明确标注的 8-bit SDR 放大画面，不恢复原片缺失的细节；小窗、HDR、10-bit、未知色彩保留原生显示。');
      details.textContent = lines.join('\n');
    }
    select.addEventListener('change', () => choose(select.value));
    const observer = new ResizeObserver(update); observer.observe(parent);
    const events = ['loadedmetadata', 'loadeddata', 'seeked', 'resize', 'emptied'];
    events.forEach(e => video.addEventListener(e, update));
    const visibility = () => { if (document.hidden) scaler?.stop(); else update(); };
    document.addEventListener('visibilitychange', visibility);
    // Auto-return to native display if the extra renderer hurts playback cadence.
    const timer = setInterval(() => {
      const q = video.getVideoPlaybackQuality?.();
      if (scaler?.active && !video.paused && q && lastQuality) {
        const frames = q.totalVideoFrames - lastQuality.totalVideoFrames;
        if (frames >= 20 && q.droppedVideoFrames - lastQuality.droppedVideoFrames > frames * 0.08) fail('精细缩放期间掉帧较多，已恢复原生显示');
      }
      lastQuality = q; update();
    }, 2000);
    const nativeSurface = () => { pip = true; scaler?.stop(); update(); };
    const leavePip = () => { pip = false; update(); };
    video.addEventListener('enterpictureinpicture', nativeSurface);
    video.addEventListener('leavepictureinpicture', leavePip);
    settings(); update();
    request().then(p => { if (!closed) { source = p; update(); } }).catch(() => { if (!closed) { failed = true; fallback = '原片参数读取失败，保留原生显示'; update(); } });
    return {choose, update, get mode() { return mode; }, close() {
      closed = true; clearInterval(timer); observer.disconnect(); scaler?.close();
      events.forEach(e => video.removeEventListener(e, update));
      document.removeEventListener('visibilitychange', visibility);
      video.removeEventListener('enterpictureinpicture', nativeSurface); video.removeEventListener('leavepictureinpicture', leavePip);
    }};
  }
  return {geometry, eligible, mount, MODES, vertex, fragment};
});
