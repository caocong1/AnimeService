/* Background alignment never overwrites a manual edit or a removed source. */
(function(root) {
  'use strict';
  function preferences(value = {}) {
    const out = {};
    for (const key of ['alignmentManual', 'alignmentEnabled'])
      if (typeof value[key] === 'boolean') out[key] = value[key];
    return out;
  }
  function create({request, present, save, render, apply, wait = ms => new Promise(r => setTimeout(r, ms))}) {
    const runs = new WeakMap();
    function cancel(source) {
      const run = runs.get(source);
      if (!run) return;
      runs.delete(source);
      if (run.job) request(`/${run.job}/cancel`, {}).catch(() => {});
    }
    function manual(source) {
      cancel(source); source.alignmentManual = true;
      source.alignmentStatus = 'manual';
    }
    async function start(source, {force = false, confirmed = false} = {}) {
      if (!present(source) || source.alignmentEnabled === false || source.alignmentManual || runs.has(source)) return;
      const run = {}; runs.set(source, run);
      const current = () => present(source) && runs.get(source) === run && source.alignmentEnabled !== false && !source.alignmentManual;
      source.alignmentStatus = 'queued'; render();
      try {
        let result = await request('', {source_id: source.id, force, confirmed});
        run.job = result.job_id;
        if (!current()) { if (run.job) request(`/${run.job}/cancel`, {}).catch(() => {}); return; }
        for (let polls = 0; ['queued','running'].includes(result.status); polls++) {
          source.alignmentStatus = result.status; render();
          if (polls >= 100) { cancel(source); source.alignmentStatus = 'unavailable'; render(); return; }
          await wait(1500);
          if (!current()) return;
          result = await request(`/${run.job}`);
          if (!current()) return;
        }
        source.alignmentStatus = result.status;
        source.alignmentMessage = typeof result.message === 'string' ? result.message.slice(0,200) : '';
        if (result.status === 'matched' && typeof result.offset === 'number' && Number.isFinite(result.offset) && Math.abs(result.offset) <= 3600) {
          source.offset = Math.round(result.offset * 10) / 10;
          source.alignmentManual = false;
          save(source); await apply();
        }
        render();
      } catch (_) { if (current()) { source.alignmentStatus = 'unavailable'; render(); } }
      finally { if (runs.get(source) === run) runs.delete(source); }
    }
    return {start, cancel, manual};
  }
  const api = {preferences, create};
  if (typeof module !== 'undefined') module.exports = api;
  else root.DanmuAlignment = api;
})(globalThis);
