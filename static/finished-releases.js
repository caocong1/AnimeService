'use strict';
/* Local finished media are deliverables; editorial acceptance remains independent. */
(function (root) {
  function normalize(data) {
    return (Array.isArray(data?.releases) ? data.releases : []).filter(r =>
      r.status === 'finished' && /^[a-z0-9][a-z0-9-]{0,79}$/.test(r.id || '') &&
      ['show_id', 'season', 'episode'].every(k => Number.isSafeInteger(r[k]) && r[k] > 0) &&
      Number.isSafeInteger(r.version) && r.version >= 0 &&
      (r.edition == null || ['v0','v1'].includes(r.edition)) &&
      Number.isFinite(r.duration_seconds) && r.duration_seconds > 0
    ).map(r => ({...r, title: String(r.title || ''),
      player_url: '/watch?release=' + encodeURIComponent(r.id),
      video_url: '/static/finished-media/' + r.id + '/episode.mp4',
      subtitles_url: '/static/finished-media/' + r.id + '/subtitles.zip',
      notes_url: '/static/finished-media/' + r.id + '/notes.txt'}));
  }
  function compare(a,b) { const rank=r=>r.edition==='v1'?2:r.edition==='v0'?0:1;return rank(a)-rank(b)||a.version-b.version; }
  function label(r) { return r.edition==='v0'?'v0 · 观看版':r.edition==='v1'?'v1 · 精校细调版':`v${r.version} · 既有版本`; }
  function merge(entries, releases) {
    const rows = entries.map(e => ({...e}));
    const key = r => `${r.show_id}/${r.season}/${r.episode}`;
    const latest = new Map();
    for (const r of releases) if (!latest.has(key(r)) || compare(latest.get(key(r)),r)<0) latest.set(key(r), r);
    for (const [k, release] of latest) {
      let row = rows.find(e => key(e) === k);
      if (!row) { row = {show_id: release.show_id, season: release.season, episode: release.episode,
        title: release.title, versions: [], build_id: null, active: false}; rows.push(row); }
      row.release = release;
      row.release_versions = releases.filter(r=>key(r)===k).sort((a,b)=>compare(b,a));
    }
    return rows;
  }
  // Viewing availability includes delivered files, independently of download tasks.
  // Keep the raw episode/download records intact; this is a read-only projection.
  function applyBoard(show, board, releases) {
    const season = Number(show.mapping?.season || 1);
    const available = normalize({releases}).filter(r => r.show_id === show.id && r.season === season);
    if (!available.length) return board;
    const rows = new Map((board.eps || []).map(e => [e.n, {...e}]));
    for (const r of available) {
      if (!rows.has(r.episode)) rows.set(r.episode, {n: r.episode, s: 'ready'});
      const row = rows.get(r.episode);
      row.release_versions = available.filter(v => v.episode === r.episode).sort((a,b) => compare(b,a));
      const seen = typeof r.finished === 'boolean' ? r.finished : row.s === 'watched';
      const resumable = row.release_versions.filter(v => v.progress?.last_played && v.progress.position > 5 &&
        v.progress.position < v.progress.duration - 30).sort((a,b) => b.progress.last_played-a.progress.last_played);
      row.release = !seen && resumable.length ? resumable[0] : row.release_versions[0];
      row.s = seen ? 'watched' : resumable.length ? 'resume' : 'ready';
      // Progress in the original media does not mean this separate MP4 was played.
      delete row.position; delete row.duration; delete row.progress; delete row.date;
      if (row.s === 'resume') Object.assign(row, {position: row.release.progress.position, duration: row.release.progress.duration});
    }
    const eps = [...rows.values()].sort((a,b) => a.n-b.n);
    const resumes = eps.filter(e => e.s === 'resume');
    resumes.sort((a,b) => (b.release?.progress?.last_played || 0)-(a.release?.progress?.last_played || 0));
    return {...board, eps, next: resumes[0] || eps.find(e => e.s === 'ready') || null};
  }
  function applyList(shows, releases) {
    return shows.map(show => {
      const board = applyBoard(show, show, releases);
      if (board === show) return show;
      const playable = board.eps.filter(e => e.media || e.release);
      return {...board, state: playable.find(e => e.release?.show_state)?.release.show_state || show.state,
        watched: board.eps.filter(e => e.s === 'watched').length, downloaded: playable.length,
        unwatched: playable.filter(e => e.s !== 'watched').length,
        downloading: board.eps.filter(e => e.s === 'downloading').length,
        download_updated: Math.max(show.download_updated || 0, ...playable.filter(e => e.release)
          .map(e => Date.parse(e.release.finished_at) / 1000 || 0))};
    });
  }
  async function load({verifyMedia = false} = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/static/finished-media/index.json', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
      if (response.status === 404) return [];
      if (!response.ok) throw Error('成品清单暂时无法读取');
      const releases = normalize(await response.json());
      if (releases.length) {
        const tracking = await fetch('/api/finished-viewing', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
        if (tracking.status !== 404) {
          if (!tracking.ok) throw Error('观看记录暂时无法同步');
          const records = (await tracking.json()).releases || [];
          for (const r of releases) {
            const record = records.find(p => p.id === r.id && p.video_sha256 === r.video_sha256);
            if (record) Object.assign(r, {progress: record.progress, finished: record.finished, show_state: record.show_state});
          }
        }
      }
      if (!verifyMedia) return releases;
      // Do not turn a stale index entry or a removed output into a play button.
      const available = new Set(); let cursor = 0, verificationFailed = false;
      await Promise.all(Array.from({length: Math.min(4, releases.length)}, async () => {
        while (cursor < releases.length) {
          const r = releases[cursor++];
          try {
            const file = await fetch(r.video_url, {method: 'HEAD', cache: 'no-store',
              credentials: 'same-origin', signal: controller.signal});
            if (file.status >= 500) throw Error('Media temporarily unavailable');
            if (file.ok && Number(file.headers.get('content-length')) > 0 &&
                /video\/mp4/i.test(file.headers.get('content-type') || '')) available.add(r.id);
          } catch { verificationFailed = true; }
        }
      }));
      if (verificationFailed) throw Error('成品文件暂时无法核验');
      return releases.filter(r => available.has(r.id));
    } finally { clearTimeout(timer); }
  }
  // Only viewing lists observe new deliveries. An active player is never changed.
  function observe(onChange, {initial = [], intervalMs = 60000} = {}) {
    const signature = rows => JSON.stringify([...rows].sort((a,b) => a.id.localeCompare(b.id)));
    let previous = signature(initial), busy = false, stopped = false;
    async function check() {
      if (busy || stopped || root.document.hidden) return;
      busy = true;
      try {
        const releases = await load({verifyMedia: true}), current = signature(releases);
        if (!stopped && current !== previous) {
          onChange(releases);
          previous = current;
        }
      } catch { /* Retain the current view on transient failures; retry next time. */ }
      finally { busy = false; }
    }
    const timer = root.setInterval(check, intervalMs);
    root.document.addEventListener('visibilitychange', check);
    root.addEventListener('focus', check);
    return () => {
      stopped = true; root.clearInterval(timer);
      root.document.removeEventListener('visibilitychange', check);
      root.removeEventListener('focus', check);
    };
  }
  const api = {normalize, merge, load, compare, label, applyBoard, applyList, observe};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.FinishedReleases = api;
})(typeof window === 'undefined' ? globalThis : window);
