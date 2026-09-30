// Narrow extension for the pinned adapter. UGC stays manual: P1 is not episode 1.
export const UGC_TYPE = 'B站视频·需核对版本';

export function ugcLinks(pages) {
  return pages.map(ep => ({
    name: '',
    url: ep.link,
    title: `【bilibili1】 ${ep.title} · ${Math.floor(ep.duration / 60)}:${String(ep.duration % 60).padStart(2, '0')}`,
    _id: Number(ep.id) || 0,
  }));
}
