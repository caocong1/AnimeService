"""Reproducible UGC search extension; no cookies or account configuration changes."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def patch():
    base = ROOT / 'vendor/danmuapi/danmu_api'
    shutil.copyfile(ROOT / 'integrations/bilibili-ugc.js', base / 'utils/animeservice-bilibili-ugc.js')
    path = base / 'sources/bilibili.js'
    source = path.read_text(encoding='utf-8')
    source = source.replace('imageUrl: item.cover || null,', 'imageUrl: item.cover || item.pic || "",')
    marker = "import { UGC_TYPE, ugcLinks } from '../utils/animeservice-bilibili-ugc.js';"
    if marker in source:
        path.write_text(source, encoding='utf-8')
        return
    replacements = [
        ('if (data.code !== 0) {\n        log("error", "[bilibili] 获取 WBI 密钥失败:',
         'if (!data.data?.wbi_img?.img_url || !data.data?.wbi_img?.sub_url) {\n        log("error", "[bilibili] 获取 WBI 密钥失败:'),
        ('const mediaType = this._extractMediaType(item.season_type_name);',
         'const mediaType = searchType === "video" ? UGC_TYPE : this._extractMediaType(item.season_type_name);'),
        ('const t2 = this._searchByType(keyword, "media_ft", mixinKey);',
         'const t2 = this._searchByType(keyword, "media_ft", mixinKey);\n      const ugc = this._searchByType(keyword, "video", mixinKey);'),
        ('Promise.all([t1, t2, t3])', 'Promise.all([t1, t2, t3, ugc])'),
        ('title: (page.part || `P${page.page}`).trim(),',
         'title: (page.part || `P${page.page}`).trim(),\n        duration: Math.max(0, Math.floor(Number(page.duration) || 0)),'),
        ('anime.isOversea || \n        titleMatches',
         'anime.mediaId?.startsWith("bv") || anime.isOversea || \n        titleMatches'),
        ('if (anime._eps && anime._eps.length > 0 && !isIncomplete) {',
         'if (anime.mediaId.startsWith("bv")) {\n             links = ugcLinks(await this.getEpisodes(anime.mediaId));\n          } else if (anime._eps && anime._eps.length > 0 && !isIncomplete) {'),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError('Pinned Bilibili adapter changed; review patch before applying')
        source = source.replace(old, new, 1)
    path.write_text(marker + '\n' + source, encoding='utf-8')


if __name__ == '__main__':
    patch()
