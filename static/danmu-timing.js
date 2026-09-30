/* Keep each source's original clock; shift first, then deduplicate. */
(function(root) {
  'use strict';
  function settings(value = {}) {
    const offset = Number(value.offset ?? 0);
    return {offset: Number.isFinite(offset) && Math.abs(offset) <= 3600 ? offset : 0};
  }
  function mix(sources, globalOffset = 0, duration = Infinity) {
    const out = [], seen = new Set();
    if (!Number.isFinite(globalOffset) || Math.abs(globalOffset) > 3600) globalOffset = 0;
    if (!Number.isFinite(duration) || duration <= 0) duration = Infinity;
    for (const source of sources) {
      const {offset} = settings(source);
      for (const c of source.comments || []) {
        const time = c.time + offset + globalOffset;
        if (!Number.isFinite(time) || time < 0 || time >= duration) continue;
        const key = JSON.stringify([Math.round(time * 10), c.text, c.mode]);
        if (seen.has(key)) continue;
        seen.add(key); out.push({...c, time});
      }
    }
    return out.sort((a,b) => a.time - b.time);
  }
  function createReplacer(plugin) {
    let pending = Promise.resolve();
    return rows => {
      // load(rows) APPENDS in ArtPlayer. Configure, then load() to replace.
      // Serialize: load() yields per comment, so overlapping reloads interleave.
      const snapshot = rows.map(c => ({...c}));
      const next = pending.then(async () => {
        plugin.config({danmuku: snapshot});
        await plugin.load();
      });
      pending = next.catch(() => {});
      return next;
    };
  }
  function savedSources(value) {
    if (!Array.isArray(value)) return null;
    const seen = new Set();
    return value.filter(s => s && typeof s.id === 'string' && s.id.length <= 100
      && typeof s.source_identity === 'string' && typeof s.title === 'string'
      && !seen.has(s.source_identity) && seen.add(s.source_identity)).slice(0,5)
      .map(s => ({id:s.id, source_identity:s.source_identity, title:s.title.slice(0,1000), ...settings(s)}));
  }
  function playerSettings(value = {}) {
    const out = {};
    for (const [key,min,max] of [['opacity',0,1],['fontSize',12,50],['speed',1,10]])
      if (typeof value[key] === 'number' && Number.isFinite(value[key]) && value[key]>=min && value[key]<=max) out[key]=value[key];
    for (const key of ['visible','antiOverlap','synchronousPlayback'])
      if (typeof value[key] === 'boolean') out[key]=value[key];
    if (Array.isArray(value.modes)) out.modes=[...new Set(value.modes.filter(v=>[0,1,2].includes(v)))];
    if (Array.isArray(value.margin) && value.margin.length===2 && value.margin.every(v =>
      (typeof v==='number' && Number.isFinite(v) && v>=0 && v<=2000) || (typeof v==='string' && /^(?:100|\d{1,2})(?:\.\d+)?%$/.test(v)))) out.margin=[...value.margin];
    return out;
  }
  const api = {settings, mix, createReplacer, savedSources, playerSettings};
  if (typeof module !== 'undefined') module.exports = api;
  else root.DanmuTiming = api;
})(globalThis);
