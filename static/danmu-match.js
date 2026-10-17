/* Conservative automatic matching while an older worker protects active playback. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DanmuMatch = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const normalize = value => String(value || '').normalize('NFKC').toLowerCase()
    .replace(/\((?:\d{4}|n\/a)\)【[^】]+】from \w+$/, '').trim();
  const key = value => normalize(value).replace(/[^\p{L}\p{N}]/gu, '');
  const number = '[零〇一二两三四五六七八九十百\\d]+';
  function integer(value) {
    if (/^\d+$/.test(value)) return Number(value);
    const digits = {'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9};
    if (value in digits) return digits[value];
    const parts = value.split('十');
    if (parts.length === 2 && parts.every(v => !v || v in digits)) return (parts[0] ? digits[parts[0]] : 1)*10+(digits[parts[1]] || 0);
    return null;
  }
  function seasons(value) {
    return [...normalize(value).matchAll(new RegExp(`(?:第\\s*)?(${number})\\s*[季期]|\\b(?:season\\s*|s)(\\d+)\\b|\\bs(\\d+)e\\d+\\b`, 'g'))]
      .map(m => integer(m[1] || m[2] || m[3]));
  }
  function unsafe(value, collection = false) {
    const v = normalize(value);
    if (/解说|解說|小说|小說|漫画|漫畫|预告|預告|次回予告|一口气|一口氣|剪辑|剪輯|混剪|广播剧|廣播劇|音乐剧|音樂劇|小剧场|小劇場|剧透|劇透|外传|外傳|番外|总集篇|總集篇|\b(?:reaction|review|trailer|pv|op|ed|ova|oad|sp)\b/.test(v)) return true;
    if (new Set(seasons(v)).size > 1 || /\d\s*[-+~至&]\s*\d\s*[季期]|\bs\d+\s*[+&-]\s*s?\d+/.test(v)) return true;
    return !collection && /合集|全集|全\s*\d+\s*[集话話]|\d\s*[-~至]\s*\d/.test(v);
  }
  function episodeNumber(value) {
    const v = normalize(value).replace(/^【(?:bilibili\d*|bahamut)】\s*/, '').replace(/\s*·\s*\d+:\d{2}(?::\d{2})?\s*$/, '').trim();
    if (unsafe(v) || /\d+\.\d+\s*[集话話]|上半|下半|前半|后半|後半/.test(v)) return null;
    let labels = [...v.matchAll(new RegExp(`(?:第\\s*)?(${number})\\s*[集话話]`, 'g'))].map(m => integer(m[1]));
    labels.push(...[...v.matchAll(/\b(?:episode\s*|ep\s*)(\d+)\b|\bs\d+e(\d+)\b/g)].map(m => Number(m[1] || m[2])));
    if (!labels.length) {
      const m = v.match(/^(?:第\s*)?(\d{1,3})$/) || v.match(/(?:--| - |\s)(\d{1,3})$/);
      if (m) labels = [Number(m[1])];
    }
    return labels.length === 1 && labels[0] > 0 ? labels[0] : null;
  }
  function workMatches(show, title, ugc) {
    const mapping = show.mapping || {}, season = Number(mapping.season || 1), marks = seasons(title);
    if (marks.some(n => n !== season) || unsafe(title, true)) return false;
    const names = [show.title, show.original, ...(mapping.aliases || [])].filter(n => typeof n === 'string' && n.trim());
    const chapter = /第\s*[零〇一二两三四五六七八九十百\d]+\s*(?:部分|部(?!分)|篇)|\b(?:part|cour)\s*\d+\b|上半|下半|前半|后半|後半/g;
    const chapters = v => [...normalize(v).matchAll(chapter)].map(m => key(m[0]));
    const expected = new Set(names.flatMap(chapters)), actual = chapters(title);
    if (actual.some(c => !expected.has(c)) || (expected.size && !actual.length)) return false;
    // Require a full locally verified alias. Short search prefixes are never identity evidence.
    return names.some(name => {
      const n = key(name), k = key(title);
      const distinct = season === 1 || marks.length || actual.length || (k === n && new RegExp(`${season}$`).test(normalize(title)));
      return distinct && n.length >= 4 && (ugc ? k.includes(n) : k === n);
    });
  }
  const isUgc = row => /^bv/i.test(row.bangumiId || '') || (row.type || '').includes('B站视频');
  function matchingEpisode(show, episode, bangumi, ugc) {
    if (!workMatches(show, bangumi.animeTitle, ugc)) return null;
    let allowed;
    if ((bangumi.seasons || []).length > 1) {
      const matches = bangumi.seasons.filter(s => s.name?.toLowerCase() === `season ${Number(show.mapping?.season || 1)}`);
      if (matches.length !== 1) return null;
      allowed = matches[0].id;
    }
    const eps = bangumi.episodes || [], season = Number(show.mapping?.season || 1);
    const matches = eps.filter(ep => {
      if (!ep.source_key || (allowed !== undefined && ep.seasonId !== allowed)) return false;
      const label = ep.episodeTitle || '';
      if (ugc) {
        let n = episodeNumber(label);
        if (n === null && eps.length === 1 && !unsafe(label)) n = episodeNumber(bangumi.animeTitle);
        return n === episode && seasons(label).every(s => s === season);
      }
      return /^\d+(?:\.0+)?$/.test(String(ep.episodeNumber)) && Number(ep.episodeNumber) === episode && !unsafe(label);
    });
    return matches.length === 1 ? matches[0] : null;
  }
  async function match(show, episode, web) {
    const search = await web('/danmu/search?q=' + encodeURIComponent(show.title));
    const candidates = (search.animes || []).filter(c => workMatches(show, c.animeTitle, isUgc(c)));
    let failures = 0;
    const selected = [], sources = [];
    // Bounded concurrency; the detail and the source-written episode must both agree.
    for (let i = 0; i < Math.min(candidates.length, 16); i += 4) {
      const rows = await Promise.all(candidates.slice(i, i+4).map(async c => {
        try {
          const {bangumi} = await web('/danmu/show/' + encodeURIComponent(c.animeId));
          const ep = matchingEpisode(show, episode, bangumi || {}, isUgc(c));
          return ep && {id:ep.source_key, source_identity:ep.source_identity, source_url:ep.source_url, site:ep.site, title:bangumi.animeTitle+' · '+ep.episodeTitle};
        } catch (_) { failures++; return null; }
      }));
      for (const row of rows) if (row && !selected.some(s => s.source_identity === row.source_identity)) selected.push(row);
    }
    for (let i = 0; i < Math.min(selected.length, 12); i += 4) {
      const rows = await Promise.all(selected.slice(i, Math.min(i+4, 12)).map(async s => {
        try { return {selected:s, source:(await web('/danmu/comments', {episodes:[s.id]})).sources[0]}; }
        catch (_) { failures++; return null; }
      }));
      sources.push(...rows.filter(r => r?.source));
    }
    sources.sort((a,b) => Number(!a.source.count)-Number(!b.source.count) || Number(!!a.source.error)-Number(!!b.source.error));
    const chosen = sources.slice(0,5);
    const matched = chosen.some(r => !r.source.error);
    return {status:matched ? 'matched' : failures || chosen.length ? 'error' : 'needs_selection',
      message:failures || chosen.length ? '弹幕来源暂时不可用，可重试或手动选择。' : '已搜索来源，本集尚无唯一编号证据，可手动核对。',
      title:show.title, candidates, selected:chosen.map(r => r.selected), sources:chosen.map(r => r.source)};
  }
  return {episodeNumber, workMatches, matchingEpisode, match};
});
