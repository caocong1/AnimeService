/* Frame capture at the video's own resolution; subtitles and danmu are redrawn from what is on screen. */
(function(root) {
  'use strict';
  // Where the picture sits inside a letterboxed <video> (object-fit: contain), and native px per CSS px.
  function contentBox(box, width, height) {
    const s = Math.min(box.width / width, box.height / height);
    return {left: box.left + (box.width - width * s) / 2, top: box.top + (box.height - height * s) / 2, scale: 1 / s};
  }
  function opacity(el) {
    let o = 1;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) o *= parseFloat(getComputedStyle(n).opacity);
    return o;
  }
  function drawText(ctx, el, place, scale) {
    const cs = getComputedStyle(el), r = place(el.getBoundingClientRect()), text = el.innerText.trim();
    const alpha = cs.visibility === 'visible' && text && r.w > 0 && r.h > 0 ? opacity(el) : 0;
    if (!alpha) return;
    const size = parseFloat(cs.fontSize) * scale, lines = text.split('\n'), lh = r.h / lines.length;
    const align = cs.textAlign === 'center' ? 'center' : /right|end/.test(cs.textAlign) ? 'right' : 'left';
    const x = align === 'center' ? r.x + r.w / 2 : align === 'right' ? r.x + r.w : r.x;
    Object.assign(ctx, {font: `${cs.fontStyle} ${cs.fontWeight} ${size}px ${cs.fontFamily}`, textAlign: align, textBaseline: 'middle',
      globalAlpha: alpha, fillStyle: cs.color, strokeStyle: 'rgba(0,0,0,.85)', lineWidth: Math.max(2, size / 12), lineJoin: 'round'});
    lines.forEach((line, i) => { const y = r.y + lh * (i + 0.5); ctx.strokeText(line, x, y); ctx.fillText(line, x, y); });
    ctx.globalAlpha = 1;
  }
  // layers: canvases drawn as-is (libass); texts: elements redrawn as outlined text (WebVTT lines, danmu).
  function capture(video, {layers = [], texts = []} = {}) {
    const width = video.videoWidth, height = video.videoHeight;
    if (!width || !height) throw Error('视频画面还没加载');
    const canvas = document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, width, height);
    const box = contentBox(video.getBoundingClientRect(), width, height);
    const place = r => ({x: (r.left - box.left) * box.scale, y: (r.top - box.top) * box.scale, w: r.width * box.scale, h: r.height * box.scale});
    for (const layer of layers) {
      const r = place(layer.getBoundingClientRect());
      if (r.w > 0 && r.h > 0 && layer.width && layer.height) ctx.drawImage(layer, r.x, r.y, r.w, r.h);
    }
    for (const el of texts) drawText(ctx, el, place, box.scale);
    return canvas;
  }
  const blob = canvas => new Promise((ok, fail) => canvas.toBlob(b => b ? ok(b) : fail(Error('截图编码失败')), 'image/png'));
  const canCopy = () => !!(root.isSecureContext && root.navigator?.clipboard?.write && root.ClipboardItem);
  // Clipboard writes must start inside the click, so the blob goes in as a promise.
  const copy = canvas => navigator.clipboard.write([new ClipboardItem({'image/png': blob(canvas)})]);
  async function download(canvas, name) {
    const url = URL.createObjectURL(await blob(canvas)), a = document.createElement('a');
    a.href = url; a.download = name.replace(/[\\/:*?"<>|]+/g, ' ').trim() + '.png';
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  const api = {contentBox, capture, copy, download, canCopy};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.Screenshot = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
