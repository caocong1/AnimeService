"""Match independently verified sources; never infer an anime episode from a UGC P index."""
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor


def title_key(title):
    title = re.sub(r'\(\d{4}\)【[^】]+】from \w+$', '', str(title)).strip()
    return ''.join(c for c in unicodedata.normalize('NFKC', title).casefold() if c.isalnum())


def is_ugc(row):
    return str(row.get('bangumiId', '')).lower().startswith('bv') or 'B站视频' in row.get('type', '')


def ugc_episode(title, names, episode, season=1):
    # Require a full known title, or a substantial quoted title contained in a known alias.
    # Short nicknames alone are ambiguous. Duration is deliberately NOT a matching gate.
    if re.search(r'解说|解說|小说|小說|漫画|漫畫|预告|預告|一口气|一口氣|剪辑|剪輯|合集|\b(?:PV|OP|ED|OVA|OAD|SP)\b|\d\s*[-~～至]\s*\d', title, re.I):
        return False
    season_numbers = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    markers = re.findall(r'第\s*([一二三四五六七八九十]|\d+)\s*[季期]|\bS(?:eason\s*)?(\d+)\b', title, re.I)
    if any((season_numbers.get(a or b) or int(a or b)) != season for a, b in markers):
        return False
    key = title_key(title)
    known = any(len(n) >= 4 and n in key for n in names)
    quoted = re.findall(r'[《「](.*?)[》」]', title)
    known = known or any(len(title_key(q)) >= 8 and any(title_key(q) in n for n in names) for q in quoted)
    numbers = re.findall(r'(?:第\s*)?(?<!\d)(\d+)\s*[集话話]', title)
    return known and numbers == [str(int(episode))]


def match(player, item):
    fallback = {'status': 'needs_selection', 'message': '暂未找到可核验的本集来源，可稍后重试或补充来源。', 'comments': [], 'sources': [], 'candidates': []}
    if item.get('unverified') or not item.get('show_id') or not item.get('episode'):
        return {**fallback, 'message': '这份文件尚未核验作品和集数，请手动选择弹幕来源。'}
    show = player.db.one('SELECT title,original,mapping FROM shows WHERE id=?', (item['show_id'],))
    if not show:
        return fallback
    mapping = json.loads(show['mapping'] or '{}')
    if mapping.get('offset', 0):
        return {**fallback, 'message': '这部作品使用集数偏移，请手动核对弹幕集数。'}
    names = list(dict.fromkeys(s.strip() for s in [show['title'], show['original'], *mapping.get('aliases', [])] if isinstance(s, str) and s.strip()))
    accepted = {title_key(s) for s in names}
    queries = list(dict.fromkeys([*names, show['title'][:8], show['title'][:10] + ' ' + str(item['episode'])]))
    def search(query):
        try:
            return player.danmu('search/anime', {'keyword': query[:100]}).get('animes', [])
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(search, queries))
    candidates = {str(a['animeId']): a for batch in batches for a in batch if str(a.get('animeId', '')).isdigit()}
    fallback['candidates'] = [a for a in candidates.values() if title_key(a.get('animeTitle', '')) in accepted or ugc_episode(a.get('animeTitle', ''), accepted, item['episode'], int(mapping.get('season', 1)))]
    selected = []
    for sid, candidate in candidates.items():
        ugc = is_ugc(candidate)
        if not (ugc_episode(candidate.get('animeTitle', ''), accepted, item['episode'], int(mapping.get('season', 1))) if ugc else title_key(candidate.get('animeTitle', '')) in accepted):
            continue
        try:
            bangumi = player.bind_episodes(player.danmu('bangumi/' + sid)).get('bangumi', {})
        except Exception:
            continue
        if not (ugc_episode(bangumi.get('animeTitle', ''), accepted, item['episode'], int(mapping.get('season', 1))) if ugc else title_key(bangumi.get('animeTitle', '')) in accepted):
            continue
        seasons = bangumi.get('seasons', [])
        allowed = None
        if len(seasons) > 1:
            matches = [s for s in seasons if str(s.get('name', '')).casefold() == f'season {int(mapping.get("season", 1))}']
            if len(matches) != 1:
                continue
            allowed = matches[0]['id']
        eps = bangumi.get('episodes', [])
        matched = []
        for ep in eps:
            if not ep.get('source_key') or (allowed is not None and ep.get('seasonId') != allowed):
                continue
            if ugc:
                # Single-part video inherits an explicit episode in its title. Multi-part
                # uploads must identify the episode in each part's title, never its P index.
                valid = ugc_episode(ep.get('episodeTitle', ''), accepted, item['episode'], int(mapping.get('season', 1))) if len(eps) != 1 else True
            else:
                n = str(ep.get('episodeNumber', ''))
                valid = bool(re.fullmatch(r'\d+(?:\.0+)?', n)) and float(n) == item['episode']
                valid = valid and not re.search(r'\b(?:SP|OVA|OAD|PV|OP|ED)\b|特别|特別|预告|預告', ep.get('episodeTitle', ''), re.I)
            if valid:
                matched.append(ep)
        if len(matched) != 1:
            continue
        ep = matched[0]
        if any(s['id'] == ep['source_key'] for s in selected):
            continue
        selected.append({'id': ep['source_key'], 'source_identity': ep.get('source_identity'), 'title': bangumi['animeTitle'] + ' · ' + ep.get('episodeTitle', str(item['episode']))})
    # Official/catalogue sources precede UGC, without throwing away other providers.
    selected.sort(key=lambda s: 'B站视频' in s['title'])
    selected = selected[:5]
    if not selected:
        return fallback
    result = player.comments([s['id'] for s in selected])
    if all(s.get('error') for s in result['sources']):
        return {**fallback, 'status': 'error', 'message': '已匹配本集，但弹幕来源暂时不可用，请重试。'}
    return {**result, 'status': 'matched', 'title': show['title'], 'selected': selected,
            'candidates': fallback['candidates'], 'message': f'已自动匹配第{item["episode"]}集 · {len(selected)}个来源'}
