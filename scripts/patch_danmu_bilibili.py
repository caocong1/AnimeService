"""Reproducible UGC search extension; no cookies or account configuration changes."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def patch():
    base = ROOT / 'vendor/danmuapi/danmu_api'
    shutil.copyfile(ROOT / 'integrations/bilibili-ugc.js', base / 'utils/animeservice-bilibili-ugc.js')
    shutil.copyfile(ROOT / 'integrations/bilibili-danmaku.js', base / 'utils/animeservice-bilibili-danmaku.js')
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
    # HTTP 200 may be a risk-control JSON/HTML response, never an empty segment.
    source = source.replace('import { parseDanmakuBase64, md5,', 'import { md5,')
    decoder_import = "import { parseDanmakuBase64 } from '../utils/animeservice-bilibili-danmaku.js';"
    if decoder_import not in source:source = decoder_import + '\n' + source
    old_headers = '''"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
          "Cookie": globals.bilibliCookie'''
    new_headers = '''"User-Agent": "Mozilla/5.0",
          "Referer": "https://www.bilibili.com/",
          "Cookie": globals.bilibliCookie'''
    if new_headers not in source:
        if source.count(old_headers)!=1:raise RuntimeError('Pinned Bilibili segment headers changed')
        source=source.replace(old_headers,new_headers,1)
    source=source.replace('''                stop = true;''', '''                throw res.error || new Error('Bilibili segment request failed');''',1)
    source=source.replace('''        let stop = false;''', '')
    source=source.replace('''        if (stop) break;''', '')
    source=source.replace('// 请求失败视为视频结束（熔断机制）', '// A failed segment must not become a successful empty or partial result.')
    source=source.replace('捕获到分段请求出错，说明请求完毕，停止后续请求', '弹幕分段请求失败，本次获取失败，可重试')
    source=source.replace('''      if (response && response.data) {
        contents = parseDanmakuBase64(response.data);
      }''', '''      if (!response || typeof response.data !== 'string') throw new Error('Invalid Bilibili segment response');
      contents = parseDanmakuBase64(response.data);''',1)
    xml_import = "import { parseBilibiliCommentXml } from '../utils/animeservice-bilibili-danmaku.js';"
    helper_import = "import { parseBilibiliCommentXml, mergeBilibiliComments } from '../utils/animeservice-bilibili-danmaku.js';"
    source = source.replace(xml_import, helper_import)
    if helper_import not in source:
        source = helper_import + '\n' + source
    # Both endpoints can omit comments. Read both for the exact part.
    start = '    const segmentList = segmentResult.segmentList;'
    end = '    log("info", `[bilibili] 弹幕分段数量: ${segmentList.length}`);'
    new = r'''    const segmentList = segmentResult.segmentList;
    const singleVideo = /^https?:\/\/www\.bilibili\.com\/video\/(?:BV[0-9A-Za-z]+|av[0-9]+)\/?(?:[?#]|$)/.test(id);
    if (!singleVideo) return this.collectDanmuSegments(segmentList);

    const cid = new URL(segmentList[0].url).searchParams.get('oid');
    if (!/^[1-9][0-9]{0,19}$/.test(cid ?? '')) return this.collectDanmuSegments(segmentList);

    let segmentError;
    const [segments, xml] = await Promise.allSettled([
      this.collectDanmuSegments(segmentList, error => { segmentError ??= error; }),
      this.getPublicCommentXml(cid),
    ]);
    if (xml.status === 'rejected' && (segmentError || segments.status === 'rejected')) throw xml.reason;
    return mergeBilibiliComments(segments.value ?? [], xml.value ?? []);
  }

  async getPublicCommentXml(cid) {
    const response = await httpGet(`https://comment.bilibili.com/${cid}.xml`, {
      headers: { "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/" },
      timeout: 5000,
      allow_redirects: false,
    });
    return parseBilibiliCommentXml(response.data, cid);
  }

  async collectDanmuSegments(segmentList, onSegmentError) {
''' + end
    if new not in source:
        if source.count(start) != 1 or source.count(end) != 1:
            raise RuntimeError('Pinned Bilibili segment collector changed')
        first = source.index(start)
        last = source.index(end, first) + len(end)
        source = source[:first] + new + source[last:]
    old = "                throw res.error || new Error('Bilibili segment request failed');"
    new = """                const error = res.error || new Error('Bilibili segment request failed');
                if (!onSegmentError) throw error;
                onSegmentError(error);"""
    if new not in source:
        if source.count(old) != 1:
            raise RuntimeError('Pinned Bilibili segment error handling changed')
        source = source.replace(old, new, 1)
    path.write_text(source, encoding='utf-8')
    formatter = base / 'utils/danmu-util.js'
    formatter_source = formatter.read_text(encoding='utf-8')
    old = '  const groupedDanmus = groupDanmusByMinute(filteredDanmus, globals.groupMinute, isMultiSource);'
    new = """  const groupMinute = platform === 'bilibili1' && items.some(item => item.preserveBilibiliTiming)
    ? 0 : globals.groupMinute;
  const groupedDanmus = groupDanmusByMinute(filteredDanmus, groupMinute, isMultiSource);"""
    if new not in formatter_source:
        if formatter_source.count(old) != 1:
            raise RuntimeError('Pinned danmaku grouping changed')
        formatter.write_text(formatter_source.replace(old, new, 1), encoding='utf-8')
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
