"""Match independently verified sources; never infer an anime episode from a UGC P index."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from .danmu_identity import Work, key, episode_number, seasons, unsafe, subject_names


def title_key(title):
    return key(title)


def is_ugc(row):
    return str(row.get('bangumiId', '')).lower().startswith('bv') or 'B站视频' in row.get('type', '')


def ugc_episode(title, names, episode, season=1):
    return Work(names,season).matches(title,True) and episode_number(title)==int(episode)


def match(player, item):
    fallback = {'status': 'needs_selection', 'message': '暂未找到可核验的本集来源，可稍后重试或补充来源。', 'comments': [], 'sources': [], 'candidates': []}
    if item.get('unverified') or not item.get('show_id') or not item.get('episode'):
        return {**fallback, 'message': '这份文件尚未核验作品和集数，请手动选择弹幕来源。'}
    show = player.db.one('SELECT title,original,mapping,metadata FROM shows WHERE id=?', (item['show_id'],))
    if not show:
        return fallback
    mapping = json.loads(show['mapping'] or '{}')
    # Download release offsets do not describe danmu providers. Match the already
    # verified local episode directly; never apply that offset to a source label.
    work = Work(subject_names(show,mapping),int(mapping.get('season',1)))
    names = work.names
    # Search names and structural work stems; acceptance separately checks season
    # and source-written episode evidence, never a search prefix or a P index.
    queries = list(dict.fromkeys(q.strip() for q in [*names, *work.queries, show['title'][:8], show['title'][:4], show['title'][:10] + ' ' + str(item['episode'])] if q.strip()))
    def search(query):
        try:
            return player.danmu('search/anime', {'keyword': query[:100]}).get('animes', []),False
        except Exception:
            return [],True
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(search, queries))
    search_failures=sum(failed for _,failed in batches)
    candidates = {str(a['animeId']): a for batch,_ in batches for a in batch if str(a.get('animeId', '')).isdigit()}
    verified = [a for a in candidates.values() if work.matches(a.get('animeTitle',''),is_ugc(a))]
    verified.sort(key=is_ugc)
    fallback['candidates'] = player.describe_candidates({'animes': verified})['animes']
    diagnostics = {'queries':len(queries),'search_results':len(candidates),'verified_works':len(verified),'episode_matches':0,'ambiguous_sources':0,'search_failures':search_failures}
    fallback['diagnostics'] = diagnostics
    def detail(candidate):
        try:return player.bind_episodes(player.danmu('bangumi/'+str(candidate['animeId']))).get('bangumi',{}),False
        except Exception:return {},True
    with ThreadPoolExecutor(max_workers=4) as pool:
        details = list(pool.map(detail,verified[:16]))
    selected = []
    diagnostics['detail_failures']=sum(failed for _,failed in details)
    for candidate,(bangumi,_) in zip(verified[:16],details):
        ugc = is_ugc(candidate)
        if not work.matches(bangumi.get('animeTitle',''),ugc):
            continue
        catalogue_seasons = bangumi.get('seasons', [])
        allowed = None
        if len(catalogue_seasons) > 1:
            matches = [s for s in catalogue_seasons if str(s.get('name', '')).casefold() == f'season {work.season}']
            if len(matches) != 1:
                continue
            allowed = matches[0]['id']
        eps = bangumi.get('episodes', [])
        matched = []
        for ep in eps:
            if not ep.get('source_key') or (allowed is not None and ep.get('seasonId') != allowed):
                continue
            if ugc:
                label=ep.get('episodeTitle','')
                n=episode_number(label)
                if n is None and len(eps)==1 and not unsafe(label):
                    n=episode_number(bangumi.get('animeTitle',''))
                valid=n==item['episode'] and not any(s!=work.season for s in seasons(label))
            else:
                n = str(ep.get('episodeNumber', ''))
                valid = bool(re.fullmatch(r'\d+(?:\.0+)?', n)) and float(n) == item['episode']
                valid = valid and not re.search(r'\b(?:SP|OVA|OAD|PV|OP|ED)\b|特别|特別|预告|預告', ep.get('episodeTitle', ''), re.I)
            if valid:
                matched.append(ep)
        if len(matched) != 1:
            diagnostics['ambiguous_sources'] += len(matched)>1
            continue
        ep = matched[0]
        if any(s['id'] == ep['source_key'] for s in selected):
            continue
        selected.append({'id': ep['source_key'], 'source_identity': ep.get('source_identity'), 'source_url': ep.get('source_url'), 'site': ep.get('site'), 'title': bangumi['animeTitle'] + ' · ' + ep.get('episodeTitle', str(item['episode']))})
    # Official/catalogue sources precede UGC, without throwing away other providers.
    selected.sort(key=lambda s: 'B站视频' in s['title'])
    selected = selected[:12]
    diagnostics['episode_matches']=len(selected)
    if not selected:
        if search_failures or diagnostics['detail_failures']:
            return {**fallback,'status':'error','retryable':True,'message':'弹幕来源查询暂时失败，可稍后重试或手动选择来源。'}
        if verified:fallback['message']='已找到作品来源，但本集编号尚无唯一证据，可查看候选分集核对。'
        return fallback
    # Empty sources must not crowd out a later source that actually has comments.
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches=list(pool.map(lambda s:player.comments([s['id']]),selected))
    fetched={s['id']:s for b in batches for s in b['sources']}
    selected.sort(key=lambda s:(not bool(fetched[s['id']].get('count')), bool(fetched[s['id']].get('error')), 'B站视频' in s['title']))
    selected=selected[:5]
    result={'sources':[fetched[s['id']] for s in selected],'comments':[]}
    seen=set()
    for source in result['sources']:
        for row in source.get('comments',[]):
            identity=(round(row['time'],1),row['text'],row['mode'])
            if identity not in seen:seen.add(identity);result['comments'].append(row)
    result['comments'].sort(key=lambda r:r['time'])
    if all(s.get('error') for s in result['sources']):
        return {**fallback, 'status': 'error', 'retryable':True, 'message': '已匹配本集，但弹幕来源暂时不可用，请重试。'}
    return {**result, 'status': 'matched', 'title': show['title'], 'selected': selected,
            'candidates': fallback['candidates'], 'diagnostics':diagnostics, 'message': f'已自动匹配第{item["episode"]}集 · {len(selected)}个来源'}
