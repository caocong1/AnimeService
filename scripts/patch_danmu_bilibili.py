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
    old_marker = "import { UGC_TYPE, ugcLinks } from '../utils/animeservice-bilibili-ugc.js';"
    marker = "import { UGC_TYPE, ugcLinks, searchUgc } from '../utils/animeservice-bilibili-ugc.js';"
    already_ugc = old_marker in source or marker in source
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
    if not already_ugc:
        for old, new in replacements:
            if source.count(old) != 1:
                raise RuntimeError('Pinned Bilibili adapter changed; review patch before applying')
            source = source.replace(old, new, 1)
        source = marker + '\n' + source
    source = source.replace(old_marker, marker)
    improvements = [
        ('const ugc = this._searchByType(keyword, "video", mixinKey);',
         'const ugc = searchUgc(this, keyword, mixinKey);'),
        ('smartTitleReplace(sourceAnimes, cnAlias);',
         'smartTitleReplace(sourceAnimes.filter(anime => !anime.mediaId?.startsWith("bv")), cnAlias);'),
        ('return s === resolvedQuerySeason || (resolvedQuerySeason === 1 && s === null);',
         'return anime.mediaId?.startsWith("bv") || s === resolvedQuerySeason || (resolvedQuerySeason === 1 && s === null);'),
    ]
    for old, new in improvements:
        if source.count(new) == 1:
            continue
        if source.count(old) != 1:
            raise RuntimeError('Pinned Bilibili search changed; review patch before applying')
        source = source.replace(old, new, 1)
    # Search already includes the public uploader; preserve it without N detail requests.
    for old, new in [
        ('title: cleanedTitle,', 'title: cleanedTitle,\n          uploader: searchType === "video" && typeof item.author === "string" ? item.author.slice(0, 100) : "",\n          danmaku_total: searchType === "video" && Number.isSafeInteger(item.video_review) && item.video_review >= 0 ? item.video_review : null,'),
        ('bangumiId: anime.mediaId,', 'bangumiId: anime.mediaId,\n            uploader: anime.uploader || "",\n            danmaku_total: anime.danmaku_total ?? null,'),
    ]:
        if new in source:continue
        if source.count(old) != 1:raise RuntimeError('Pinned Bilibili uploader fields changed')
        source = source.replace(old, new, 1)
    path.write_text(source, encoding='utf-8')
    model = base / 'models/dandan-model.js'
    text = model.read_text(encoding='utf-8')
    for old, new in [
        ('mergedChildren = [], isHiddenChild = false } = {})', 'mergedChildren = [], isHiddenChild = false, uploader = "", danmaku_total = null } = {})'),
        ('episodeCount, rating, isFavorited, source, mergedChildren, isHiddenChild  });', 'episodeCount, rating, isFavorited, source, mergedChildren, isHiddenChild, uploader, danmaku_total  });'),
    ]:
        if new in text:continue
        if text.count(old) != 1:raise RuntimeError('Pinned Anime uploader model changed')
        text = text.replace(old, new, 1)
    model.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    patch()
