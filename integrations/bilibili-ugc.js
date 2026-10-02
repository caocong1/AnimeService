// Narrow extension for the pinned adapter. UGC stays manual: P1 is not episode 1.
export const UGC_TYPE = 'B站视频·需核对版本';

const normalized = text => String(text).toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
const episodePattern = /第?\s*[0-9零〇一二三四五六七八九十百]+\s*[季期集话話]/gu;
const noisePattern = /官中|简中|簡中|繁中|中字|字幕|周更|週更|无删减|無刪減|未删减|未刪減|完整版|超清|高清|1080p|2160p|4k/giu;

export function ugcQuery(keyword) {
  const original = keyword.trim();
  const cleaned = original.replace(noisePattern, '').replace(/[「」『』【】（）()\[\]“”"《》]/g, ' ').replace(/\s+/g, ' ').trim();
  const core = cleaned.replace(episodePattern, '').replace(/\bS\d+(?:E\d+)?\b|\bE[P]?\d+\b/gi, '').replace(/\s+\d+(?:\.\d+)?\s*$/, '').replace(/\s+/g, ' ').trim();
  // Preserve the input first. Two bounded fallbacks reduce title decoration noise.
  return {core: normalized(core), terms: [...new Set([original, cleaned, core].filter(Boolean))].slice(0, 3)};
}

export async function searchUgc(source, keyword, mixinKey) {
  const {core, terms} = ugcQuery(keyword), rows = new Map();
  for (const term of terms) {
    for (const row of await source._searchByType(term, 'video', mixinKey)) {
      // Bilibili sometimes returns recommendations with no title match.
      if (core.length >= 2 && !normalized(row.title).includes(core)) continue;
      if (!rows.has(row.mediaId)) rows.set(row.mediaId, row);
    }
  }
  const season=keyword.match(/第?\s*(\d+)\s*季/)?.[1];
  const episode=keyword.match(/第?\s*(\d+)\s*[集话話]/)?.[1] || keyword.match(/\s+(\d+)\s*$/)?.[1];
  const rank = row => {
    const title=String(row.title), n=normalized(title);
    let score=n.includes(normalized(keyword)) ? 100 : 0;
    if(season && new RegExp(`第?\\s*0*${Number(season)}\\s*季`).test(title)) score+=10;
    if(episode && new RegExp(`第?\\s*0*${Number(episode)}\\s*[集话話]`).test(title)) score+=20;
    if(/预告|預告|PV|片段|剪辑|剪輯/i.test(title)) score-=5;
    return score;
  };
  return [...rows.values()].sort((a,b)=>rank(b)-rank(a));
}

export function ugcLinks(pages) {
  return pages.map(ep => ({
    name: '',
    url: ep.link,
    title: `【bilibili1】 ${ep.title} · ${Math.floor(ep.duration / 60)}:${String(ep.duration % 60).padStart(2, '0')}`,
    _id: Number(ep.id) || 0,
  }));
}
