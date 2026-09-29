"""Conservative automatic matching using already verified local show/episode links."""
import json
import re
import unicodedata


def title_key(title):
    # Remove only the adapter's display decoration, never a season/part qualifier.
    title = re.sub(r'\(\d{4}\)【[^】]+】from \w+$', '', str(title)).strip()
    return ''.join(c for c in unicodedata.normalize('NFKC', title).casefold() if c.isalnum())


def match(player, item):
    fallback = {'status': 'needs_selection', 'message': '未找到唯一可核验的作品和集数，请展开弹幕设置手动选择。', 'comments': [], 'sources': []}
    if item.get('unverified') or not item.get('show_id') or not item.get('episode'):
        return {**fallback, 'message': '这份文件尚未核验作品和集数，请手动选择弹幕来源。'}
    show = player.db.one('SELECT title,original,mapping FROM shows WHERE id=?', (item['show_id'],))
    if not show:
        return fallback
    mapping = json.loads(show['mapping'] or '{}')
    if mapping.get('offset', 0):
        return {**fallback, 'message': '这部作品使用集数偏移，请手动核对弹幕集数。'}
    names = [show['title'], show['original'], *mapping.get('aliases', [])]
    names = [s for s in names if isinstance(s, str) and s.strip()]
    accepted = {title_key(s) for s in names}
    queries = [show['title'][:100]]
    # Some aggregators fail on full titles. Broader search is safe only because
    # every result is still checked against the complete verified title below.
    if len(show['title']) > 8:
        queries.append(show['title'][:8])
    if show['original']:
        queries.append(show['original'][:100])
    candidates = []
    for query in dict.fromkeys(queries):
        data = player.danmu('search/anime', {'keyword': query})
        candidates = [a for a in data.get('animes', []) if title_key(a.get('animeTitle', '')) in accepted]
        if candidates:
            break
    # Never guess between similarly named editions, dubs, seasons or providers.
    candidates = {str(a['animeId']): a for a in candidates if str(a.get('animeId', '')).isdigit()}
    if len(candidates) != 1:
        return fallback
    detail = player.bind_episodes(player.danmu('bangumi/' + next(iter(candidates))))
    bangumi = detail.get('bangumi', {})
    if title_key(bangumi.get('animeTitle', '')) not in accepted:
        return fallback
    seasons = bangumi.get('seasons', [])
    allowed_season = None
    if len(seasons) > 1:
        expected = int(mapping.get('season', 1))
        matches = [s for s in seasons if str(s.get('name', '')).casefold() == f'season {expected}']
        if len(matches) != 1:
            return fallback
        allowed_season = matches[0]['id']
    episodes = []
    for ep in bangumi.get('episodes', []):
        number = str(ep.get('episodeNumber', ''))
        if not re.fullmatch(r'\d+(?:\.0+)?', number) or float(number) != item['episode']:
            continue
        if re.search(r'\b(?:SP|OVA|OAD|PV|OP|ED)\b|特别|特別|预告|預告', ep.get('episodeTitle', ''), re.I):
            continue
        if allowed_season is not None and ep.get('seasonId') != allowed_season:
            continue
        if ep.get('source_key'):
            episodes.append(ep)
    if len(episodes) != 1:
        return fallback
    ep = episodes[0]
    result = player.comments([ep['source_key']])
    title = bangumi['animeTitle'] + ' · ' + ep.get('episodeTitle', str(item['episode']))
    if any(s.get('error') for s in result['sources']):
        return {**fallback, 'status': 'error', 'message': '已匹配本集，但弹幕来源暂时不可用，请重试。'}
    return {**result, 'status': 'matched', 'title': title,
            'selected': [{'id': ep['source_key'], 'title': title}],
            'message': f'已自动匹配第{item["episode"]}集 · {len(result["comments"])}条弹幕'}
