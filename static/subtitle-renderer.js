/* Original ASS styling via libass; WebVTT markup is rendered through a safe DOM. */
'use strict';
class SubtitleRenderer {
  constructor(art, note, fontsUrl) {
    this.art = art; this.note = note; this.fontsUrl = fontsUrl;
    this.revision = 0; this.renderer = null; this.timer = null;
    this.pending = Promise.resolve();
    art.on('subtitleAfterUpdate', cues => {
      const root = art.template.$subtitle;
      root.replaceChildren();
      for (const cue of cues) {
        const line = document.createElement('div');
        line.className = 'art-subtitle-line';
        line.style.whiteSpace = 'pre-line';
        line.append(SubtitleRenderer.safeCue(cue.getCueAsHTML()));
        root.append(line);
      }
    });
    art.on('destroy', () => this.clear());
  }
  static safeCue(node) {
    // The browser parses WebVTT; copy only basic formatting, never attributes/HTML.
    if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.textContent);
    const output = ['B', 'I', 'U', 'RUBY', 'RT'].includes(node.nodeName)
      ? document.createElement(node.nodeName.toLowerCase()) : document.createDocumentFragment();
    for (const child of node.childNodes) output.append(SubtitleRenderer.safeCue(child));
    return output;
  }
  clear() {
    ++this.revision;
    clearTimeout(this.timer);
    this.request?.abort();
    this.release();
    this.art.subtitle.show = false;
    this.art.template.$subtitle.replaceChildren();
  }
  release() {
    // libass disposes itself on a worker error; its dispose method is not idempotent.
    const renderer = this.renderer;
    this.renderer = null;
    if (renderer?.worker) renderer.dispose();
  }
  async json(url, signal) {
    const r = await fetch(url, { signal });
    const data = await r.json();
    if (!r.ok) throw Error(data.error || '字幕读取失败');
    return data;
  }
  async select(track) {
    this.clear();
    const rev = this.revision;
    if (!track) { this.note.textContent = ''; return; }
    const request = this.request = new AbortController();
    this.note.textContent = track.ass_url ? '加载原样式字幕…' : '加载字幕…';
    const fallback = async () => {
      if (rev !== this.revision) return;
      clearTimeout(this.timer);
      this.release();
      // Serialize ArtPlayer's non-cancellable switches so old responses cannot win.
      this.pending = this.pending.catch(() => {}).then(async () => {
        if (rev !== this.revision) return;
        const r = await fetch(track.url, { signal: request.signal });
        if (!r.ok) throw Error('字幕加载失败，请重新选择字幕重试');
        await this.art.subtitle.switch(track.url, { type: 'vtt', name: track.label });
        if (rev !== this.revision) return;
        this.art.subtitle.show = true;
        this.art.subtitle.update();
        this.note.textContent = '';
      });
      try { await this.pending; } catch (e) {
        if (rev === this.revision) this.note.textContent = e.message;
      }
    };
    if (!track.ass_url || typeof SubtitlesOctopus !== 'function') { await fallback(); return; }
    try {
      const [response, fonts] = await Promise.all([
        fetch(track.ass_url, { signal: request.signal }),
        this.json(this.fontsUrl, request.signal),
      ]);
      if (!response.ok) throw Error('原样式字幕读取失败');
      const content = await response.text();
      if (rev !== this.revision) return;
      let failed = false;
      const fail = () => {
        if (failed || rev !== this.revision) return;
        failed = true;
        queueMicrotask(fallback);
      };
      this.timer = setTimeout(fail, 30000);
      this.renderer = new SubtitlesOctopus({
        video: this.art.video, subContent: content,
        workerUrl: '/static/vendor/libass/subtitles-octopus-worker.js',
        legacyWorkerUrl: '/static/vendor/libass/subtitles-octopus-worker-legacy.js',
        fonts: fonts.fonts,
        fallbackFont: '/static/vendor/libass/NotoSansCJKsc-Regular.otf',
        targetFps: 30, libassMemoryLimit: 64, libassGlyphLimit: 16,
        onReady: () => {
          if (rev !== this.revision || failed) return;
          clearTimeout(this.timer);
          this.renderer?.setCurrentTime(this.art.currentTime);
          this.note.textContent = '原样式字幕 · 缺失字体使用思源黑体替代';
        },
        onError: fail,
      });
      // ArtPlayer's video is at z-index 10; keep ASS on its subtitle plane.
      if (this.renderer.canvasParent) {
        this.renderer.canvasParent.style.zIndex = '20';
        this.renderer.canvasParent.style.pointerEvents = 'none';
      }
    } catch (e) { if (rev === this.revision) await fallback(); }
  }
}
window.SubtitleRenderer = SubtitleRenderer;
