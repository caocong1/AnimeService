/* Bounded retries for read-only danmu lookups, never for playback or downloads. */
(function(root) {
  'use strict';
  function create({delays = [5000, 15000, 45000], wait, changed = () => {}} = {}) {
    let closed = false, epoch = 0;
    const pending = new Map();
    const pause = wait || (ms => new Promise(resolve => {
      const timer = setTimeout(() => { pending.delete(timer); resolve(); }, ms);
      pending.set(timer, resolve);
    }));
    const aborted = () => Object.assign(new Error('请求已取消'), {name: 'AbortError'});
    async function request(run, {current = () => true, onRetry = changed} = {}) {
      const revision = epoch, valid = () => !closed && revision === epoch && current();
      for (let attempt = 0; ; attempt++) {
        if (!valid()) throw aborted();
        let result, failure;
        try {
          result = await run();
          if (!valid()) throw aborted();
          if (!result?.retryable && !result?.sources?.some(s => s.error)) return result;
        } catch (e) {
          if (e.name === 'AbortError' || (e.status && ![404,429,502,503,504].includes(e.status) && e.status < 500)) throw e;
          failure = e;
        }
        if (!valid()) throw aborted();
        if (attempt >= delays.length) { onRetry(null); if (failure) throw failure; return result; }
        onRetry(delays[attempt]);
        await pause(delays[attempt]);
      }
    }
    function close() {
      closed = true; epoch++;
      for (const [timer, resolve] of pending) { clearTimeout(timer); resolve(); }
      pending.clear();
    }
    return {request, close, resume: () => { closed = false; }};
  }
  const api = {create};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.DanmuRetry = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
