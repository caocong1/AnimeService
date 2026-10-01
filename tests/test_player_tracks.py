import copy
import hashlib
import json
import os
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from anime.app import create_app
from anime.db import Store
from anime.subtitles import Subtitles
from anime.webplayer import WebPlayer


@pytest.fixture
def library(tmp_path):
    db = Store(tmp_path / 'test.db')
    path = tmp_path / 'Episode13.mkv'
    path.write_bytes(b'fake media')
    db.execute("INSERT INTO shows(id,title,original,mapping) VALUES(1,?,?,?)",
               ('遭到流放的转生重骑士凭借游戏知识大开无双', 'Original Title', json.dumps({'season': 1})))
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(1,13,?,'complete',?)",
               (str(path), path.stat().st_size))
    player = WebPlayer(db)
    key = hashlib.sha256(str(path).lower().encode()).hexdigest()[:32]
    return db, player, key, path


def upstream_for(player):
    title = player.db.show(1)['title']
    candidate = {'animeId': 42, 'animeTitle': title+'(2026)【动漫】from bahamut'}
    episode = {'episodeTitle': '【bahamut】 第13集', 'episodeNumber': '13', 'url': '51081'}
    detail = {'bangumi': {**candidate, 'episodes': [episode]}}
    calls = []
    def upstream(path, params=None):
        calls.append((path, params))
        if path == 'search/anime':
            return {'animes': [] if params['keyword'] == title else [copy.deepcopy(candidate)]}
        if path.startswith('bangumi/'):
            return copy.deepcopy(detail)
        assert path == 'comment'
        assert params['url'] == 'https://ani.gamer.com.tw/animeVideo.php?sn=51081'
        return {'comments': [{'p': '20,1,16777215', 'm': 'test'}]}
    player.danmu = upstream
    return candidate, detail, calls


def test_automatic_match_fallback_exact_title_and_stable_episode_url(library):
    db, player, key, _ = library
    _, _, calls = upstream_for(player)
    before = db.show(1)
    result = player.auto_danmu(key)
    assert result['status'] == 'matched' and len(result['comments']) == 1
    count = len(calls)
    assert count >= 4
    assert player.auto_danmu(key) == result and len(calls) == count
    assert db.show(1) == before
    assert db.rows('SELECT * FROM watches') == []
    assert db.rows('SELECT * FROM web_progress') == []


@pytest.mark.parametrize('failure', ['wrong_season', 'wrong_episode', 'duplicate_episode', 'special', 'unsafe_url', 'unverified'])
def test_ambiguous_or_unverified_media_never_loads_comments(library, failure):
    db, player, key, _ = library
    candidate, detail, calls = upstream_for(player)
    episode = detail['bangumi']['episodes'][0]
    if failure == 'wrong_season':
        candidate['animeTitle'] += ' 第二季'
    if failure == 'wrong_episode':
        episode['episodeNumber'] = '12'
    if failure == 'duplicate_episode':
        detail['bangumi']['episodes'].append(copy.deepcopy(episode))
    if failure == 'special':
        episode['episodeTitle'] += ' SP'
    if failure == 'unsafe_url':
        episode['url'] = 'http://127.0.0.1:4870/'
    if failure == 'unverified':
        item = player.item(key)
        item['unverified'] = True
        player.item = lambda _: item
    assert player.auto_danmu(key)['status'] == 'needs_selection'
    assert not any(path == 'comment' for path, _ in calls)


def test_multiple_exact_candidates_require_manual_selection(library):
    _, player, key, _ = library
    title = player.db.show(1)['title']
    player.danmu = Mock(return_value={'animes': [{'animeId': i, 'animeTitle': title} for i in (1, 2)]})
    assert player.auto_danmu(key)['status'] == 'needs_selection'
    assert player.danmu.call_count >= 1


def test_failed_source_is_retryable(library):
    _, player, key, _ = library
    _, _, calls = upstream_for(player)
    original = player.danmu
    def unavailable(path, params=None):
        if path == 'comment':
            raise TimeoutError()
        return original(path, params)
    player.danmu = unavailable
    assert player.auto_danmu(key)['status'] == 'error'
    player.danmu = original
    assert player.auto_danmu(key)['status'] == 'matched'


@pytest.mark.parametrize('stage', ['search/anime', 'bangumi/'])
def test_lookup_transport_failure_is_retryable_but_wrong_identity_is_not(library, stage):
    _, player, key, _ = library
    _, _, _ = upstream_for(player)
    original = player.danmu
    def unavailable(path, params=None):
        if path.startswith(stage):raise TimeoutError()
        return original(path, params)
    player.danmu = unavailable
    result = player.auto_danmu(key)
    assert result['status'] == 'error' and result['retryable']
    player.danmu = original
    assert player.auto_danmu(key)['status'] == 'matched'


def test_partially_failed_automatic_result_does_not_freeze_retries(library, monkeypatch):
    _, player, key, _ = library
    source = {'id':'source','comments':[],'error':'temporary failure'}
    result = {'status':'matched','selected':[],'sources':[source],'comments':[]}
    matcher = Mock(return_value=result)
    monkeypatch.setattr('anime.webplayer.match_danmu', matcher)
    player.auto_danmu(key)
    source.pop('error');source['count']=0
    player.auto_danmu(key)
    player.auto_danmu(key)
    assert matcher.call_count == 2


def test_ugc_title_without_episode_evidence_never_loads_comments(library):
    _, player, key, _ = library
    title = player.db.show(1)['title']
    player.danmu = Mock(return_value={'animes': [
        {'animeId': 1, 'bangumiId': 'bvBV123', 'animeTitle': title},
        {'animeId': 2, 'type': 'B站视频·需核对版本', 'animeTitle': title},
    ]})
    assert player.auto_danmu(key)['status'] == 'needs_selection'
    assert all(call.args[0] != 'comment' for call in player.danmu.call_args_list)


def test_comments_preserve_independent_source_clocks_and_stable_identity(library):
    _, player, _, _ = library
    urls = ['https://www.bilibili.com/video/BV123?p=1', 'https://ani.gamer.com.tw/animeVideo.php?sn=51081']
    def bind():
        return player.bind_episodes({'bangumi': {'episodes': [{'url': u} for u in urls]}})['bangumi']['episodes']
    first, second = bind(), bind()
    assert first[0]['source_key'] == second[0]['source_key']
    assert first[0]['source_identity'] == second[0]['source_identity']
    player.danmu = lambda *args: {'comments': [{'p':'10,1,16777215','m':'same'}]}
    result = player.comments([x['source_key'] for x in first])
    assert len(result['comments']) == 1  # Legacy combined response remains compatible.
    assert len(result['sources']) == 2
    assert all(s['comments'][0]['time'] == 10 for s in result['sources'])
    assert result['sources'][0]['source_identity'] != result['sources'][1]['source_identity']
    assert [s['site'] for s in result['sources']] == ['bilibili', 'bahamut']


def fake_tools(monkeypatch):
    calls = []
    def run(self, args, timeout):
        calls.append(args)
        if args[0] == 'ffprobe':
            return json.dumps({'streams': [
                {'index': 3, 'codec_name': 'subrip', 'tags': {'title': 'Chinese Traditional', 'language': 'chi'}},
                {'index': 2, 'codec_name': 'subrip', 'tags': {'title': 'Chinese Simplified', 'language': 'chi'}},
                {'index': 4, 'codec_name': 'hdmv_pgs_subtitle', 'tags': {'language': 'eng'}}]}).encode()
        return b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nTest\n'
    monkeypatch.setattr(Subtitles, 'tool', lambda self, name: name)
    monkeypatch.setattr(Subtitles, 'run', run)
    return calls


def test_subtitle_cache_invalidates_on_file_change_and_rejects_unknown_tracks(library, monkeypatch):
    _, player, key, path = library
    calls = fake_tools(monkeypatch)
    item = player.item(key)
    assert player.subtitles.tracks(item)[0]['index'] == 2
    first = player.subtitles.extract(item, 2)
    assert player.subtitles.extract(item, 2) == first and len(calls) == 2
    for index in (4, 0, 99):
        with pytest.raises(ValueError):
            player.subtitles.extract(item, index)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    assert player.subtitles.extract(item, 2) != first
    assert path.read_bytes() == b'fake media'


def test_track_routes_only_expose_registered_media_and_never_write_progress(library, monkeypatch):
    db, _, key, path = library
    calls = fake_tools(monkeypatch)
    engine = Mock()
    app = create_app(db, engine, start_worker=False)
    client = TestClient(app, base_url='http://127.0.0.1:4871')
    result = client.get(f'/api/web/media/{key}/subtitles')
    assert result.status_code == 200 and result.json()['default'] == 2
    assert str(path) not in result.text
    url = result.json()['tracks'][0]['url']
    response = client.get(url)
    assert response.status_code == 200 and response.text.startswith('WEBVTT')
    assert response.headers['content-type'].startswith('text/vtt')
    assert client.get(f'/api/web/media/{key}/subtitles/99.vtt').status_code == 400
    assert client.get('/api/web/media/not-registered/subtitles').status_code == 404
    assert not engine.mock_calls
    assert not db.rows('SELECT * FROM watches') and not db.rows('SELECT * FROM web_progress')

def test_saved_danmu_source_survives_restart_and_rejects_unbound_urls(library):
    db, player, _, _ = library
    before = db.show(1)
    source = player.bind_episodes({'bangumi': {'episodes': [
        {'url': 'https://www.bilibili.com/video/BV1234567890/?p=2'},
        {'url': 'http://127.0.0.1:4870/'},
    ]}})['bangumi']['episodes']
    assert 'source_key' not in source[1]
    restored = WebPlayer(db)
    calls = []
    def upstream(path, params):
        calls.append(params['url'])
        return {'comments': [{'p': '495,1,16777215', 'm': 'saved'}]}
    restored.danmu = upstream
    result = restored.comments([source[0]['source_key']])
    assert result['sources'][0]['source_identity'] == source[0]['source_identity']
    assert result['comments'][0]['text'] == 'saved'
    assert calls == ['https://www.bilibili.com/video/BV1234567890/?p=2']
    for invalid in ['unknown', 'http://127.0.0.1:4870/']:
        with pytest.raises(ValueError):
            restored.comments([invalid])
    assert len(calls) == 1
    assert db.show(1) == before
    assert db.rows('SELECT * FROM watches') == []
    assert db.rows('SELECT * FROM web_progress') == []


def test_auto_merges_providers_ugc_uses_title_not_part_index(library):
    _, player, key, _ = library
    title = player.db.show(1)['title']
    catalog = {'animeId': 1, 'animeTitle': title + '(2026)【动漫】from bahamut'}
    ugc = {'animeId': 2, 'bangumiId': 'bvBV123', 'animeTitle': title + ' 第13集(2026)【B站视频·需核对版本】from bilibili'}
    wrong = {**ugc, 'animeId': 3, 'animeTitle': title + ' 第12集'}
    seen = []
    def upstream(path, params=None):
        if path == 'search/anime':
            # Simulate a failed original-language search and a short-query-only catalogue.
            if params['keyword'] == 'Original Title':
                raise TimeoutError()
            return {'animes': [catalog] if params['keyword'] == title[:8] else [ugc, wrong]}
        if path == 'bangumi/1':
            return {'bangumi': {**catalog, 'episodes': [{'episodeNumber':'13', 'episodeTitle':'【bahamut】 第13集', 'url':'51081'}]}}
        if path == 'bangumi/2':
            return {'bangumi': {**ugc, 'episodes': [{'episodeNumber':'1', 'episodeTitle':title+' 第13集 · 43:59', 'url':'https://www.bilibili.com/video/BV123?p=1'}]}}
        assert path == 'comment'
        seen.append(params['url'])
        return {'comments':[{'p':'20,1,16777215','m':'test'}]}
    player.danmu = upstream
    result = player.auto_danmu(key)
    assert result['status'] == 'matched'
    assert len(result['selected']) == len(seen) == 2
    assert 'bahamut' in result['selected'][0]['title']
    assert len(result['comments']) == 1


@pytest.mark.parametrize('suffix', [' 第12集', ' 第13集 解说', ' 第1-13集', ' 第13集 PV', ''])
def test_ugc_rejects_wrong_episode_and_commentary(library, suffix):
    from anime.auto_danmu import ugc_episode, title_key
    title = library[1].db.show(1)['title']
    assert not ugc_episode(title + suffix, {title_key(title)}, 13)


def test_ugc_quoted_alias_and_duration_independent():
    from anime.auto_danmu import ugc_episode, title_key
    names = {title_key('被追放的转生重骑士用游戏知识开无双')}
    assert ugc_episode('七月新番：《转生重骑士用游戏知识开无双》13集 · 43:59', names, 13)
    assert not ugc_episode('【重骑士】第13话', names, 13)

def test_ugc_does_not_confuse_seasons():
    from anime.auto_danmu import ugc_episode, title_key
    names = {title_key('测试动画作品')}
    assert not ugc_episode('测试动画作品 第二季 第13集', names, 13)
    assert not ugc_episode('测试动画作品 Season 2 第13集', names, 13)
    assert ugc_episode('测试动画作品 第二季 第13集', names, 13, 2)


def test_bookworm_short_discovery_keeps_verified_sequel_and_episode_gates(library):
    db, player, key, _ = library
    title = '小书痴的下克上 〜为了成为图书管理员而不择手段〜 领主的养女'
    alias = '小书痴的下克上：为了成为图书管理员不择手段！领主的养女'
    db.execute('UPDATE shows SET title=?,mapping=? WHERE id=1',
               (title, json.dumps({'season': 4, 'aliases': [alias]})))
    candidate = {'animeId': 48642, 'bangumiId': '48642', 'source': 'bahamut',
                 'animeTitle': '小书痴的下克上  为了成为图书管理员不择手段！领主的养女(2026)【动漫】from bahamut'}
    old_season = {**candidate, 'animeId': 28805,
                  'animeTitle': '小书痴的下克上：为了成为图书管理员不择手段！第三季(2022)【动漫】from bahamut'}
    calls = []
    def upstream(path, params=None):
        calls.append((path, params))
        if path == 'search/anime':
            assert params['keyword'] == params['keyword'].strip()
            return {'animes': [copy.deepcopy(candidate), old_season] if params['keyword'] == title[:4] else []}
        assert path != 'bangumi/28805'
        if path == 'bangumi/48642':
            return {'bangumi': {**candidate, 'seasons': [{'id': 'season-48642', 'name': 'Season 1'}],
                    'episodes': [{'episodeNumber': '13', 'episodeTitle': '【bahamut】 第13集', 'url': '51081'},
                                 {'episodeNumber': '12', 'episodeTitle': '【bahamut】 第12集', 'url': '51080'}]}}
        assert params['url'] == 'https://ani.gamer.com.tw/animeVideo.php?sn=51081'
        return {'comments': [{'p': '20,1,16777215', 'm': 'test'}]}
    player.danmu = upstream
    before = db.show(1)
    result = player.auto_danmu(key)
    assert result['status'] == 'matched'
    assert len(result['selected']) == len(result['candidates']) == 1
    assert result['selected'][0]['source_url'].endswith('sn=51081')
    assert result['sources'][0]['source_url'].endswith('sn=51081')
    assert result['candidates'][0]['source_url'].endswith('sn=48642')
    assert db.show(1) == before
    assert not db.rows('SELECT * FROM watches') and not db.rows('SELECT * FROM web_progress')


def test_source_page_links_use_verified_provider_ids_and_strip_private_query(library):
    from anime.webplayer import source_page_url
    _, player, _, _ = library
    rows = player.describe_candidates({'animes': [
        {'source': 'bilibili', 'bangumiId': 'bvBV123', 'source_url': 'javascript:alert(1)'},
        {'source': 'bilibili', 'bangumiId': 'ss33050'},
        {'source': 'bahamut', 'bangumiId': '48642'},
        {'source': 'bahamut', 'bangumiId': 'https://evil.test/'},
    ]})['animes']
    assert [r['site'] for r in rows] == ['bilibili', 'bilibili', 'bahamut', 'bahamut']
    assert rows[0]['source_url'] == 'https://www.bilibili.com/video/BV123'
    assert rows[1]['source_url'] == 'https://www.bilibili.com/bangumi/play/ss33050'
    assert rows[2]['source_url'] == 'https://ani.gamer.com.tw/animeVideo.php?sn=48642'
    assert rows[3]['source_url'] is None
    assert source_page_url('https://www.bilibili.com/video/BV123?p=2&token=private#secret') == 'https://www.bilibili.com/video/BV123?p=2'
    for url in ['javascript:alert(1)', 'https://www.bilibili.com.evil.test/video/BV123',
                'https://user:secret@www.bilibili.com/video/BV123', 'https://www.bilibili.com:8443/video/BV123',
                'https://ani.gamer.com.tw/other?sn=48642']:
        assert source_page_url(url) is None


def test_missing_year_metadata_does_not_break_verified_title():
    from anime.auto_danmu import title_key
    assert title_key('测试番剧(N/A)【动漫】from bahamut') == title_key('测试番剧')


@pytest.mark.parametrize('variant',['correct','duplicate','missing_label','wrong_season','commentary'])
def test_season_collection_matches_part_title_not_p_order(library,variant):
    db,player,key,_=library
    db.execute('UPDATE shows SET title=?,mapping=? WHERE id=1',('无职转生 第三季 ～到了异世界就拿出真本事～',json.dumps({'season':3,'aliases':['无职转生 3期']})))
    candidate={'animeId':7,'bangumiId':'bvBV123','animeTitle':'【无职转生 第三季】全14话 超清中字(2026)【B站视频·需核对版本】from bilibili'}
    label={'correct':'13','duplicate':'13','missing_label':'P13','wrong_season':'无职转生 第二季 第13话','commentary':'13集 reaction'}[variant]
    eps=[{'episodeNumber':'1','episodeTitle':'【bilibili1】 '+label+' · 32:38','url':'https://www.bilibili.com/video/BV123?p=1'},
         {'episodeNumber':'13','episodeTitle':'【bilibili1】 12 · 32:38','url':'https://www.bilibili.com/video/BV123?p=13'}]
    if variant=='duplicate':eps.append({**eps[0],'url':'https://www.bilibili.com/video/BV123?p=3'})
    calls=[]
    def upstream(path,params=None):
        calls.append((path,params))
        if path=='search/anime':return {'animes':[copy.deepcopy(candidate)]}
        if path=='bangumi/7':return {'bangumi':{**candidate,'episodes':copy.deepcopy(eps)}}
        assert params['url']=='https://www.bilibili.com/video/BV123?p=1'
        return {'comments':[{'p':'20,1,16777215','m':'episode13'}]}
    player.danmu=upstream;before=db.show(1);result=player.auto_danmu(key)
    assert result['status']==('matched' if variant=='correct' else 'needs_selection')
    assert any(p=='comment' for p,_ in calls)==(variant=='correct')
    assert db.show(1)==before and not db.rows('select * from watches')


def test_download_offset_is_not_applied_to_danmu_episode_labels(library):
    db,player,key,_=library
    db.execute('UPDATE shows SET mapping=?',(json.dumps({'season':1,'offset':12}),))
    _,detail,_=upstream_for(player)
    assert player.auto_danmu(key)['status']=='matched'  # Both local and source say 13.
    player.auto_cache.clear();detail['bangumi']['episodes'][0]['episodeNumber']='25'
    assert player.auto_danmu(key)['status']=='needs_selection'  # Never add download offset.


def test_empty_source_does_not_crowd_out_later_nonempty_source(library):
    _,player,key,_=library
    title=player.db.show(1)['title'];candidates=[{'animeId':i,'animeTitle':title} for i in range(1,7)]
    def upstream(path,params=None):
        if path=='search/anime':return {'animes':copy.deepcopy(candidates)}
        if path.startswith('bangumi/'):
            i=path.split('/')[1];return {'bangumi':{'animeTitle':title,'episodes':[{'episodeNumber':'13','episodeTitle':'第13集','url':'https://www.bilibili.com/bangumi/play/ep'+i}]}}
        return {'comments':[{'p':'20,1,16777215','m':'available'}] if params['url'].endswith('ep6') else []}
    player.danmu=upstream;result=player.auto_danmu(key)
    assert result['status']=='matched' and len(result['selected'])==5
    assert result['sources'][0]['source_url'].endswith('ep6') and result['sources'][0]['count']==1
