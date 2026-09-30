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


@pytest.mark.parametrize('failure', ['wrong_season', 'wrong_episode', 'duplicate_episode', 'special', 'unsafe_url', 'unverified', 'offset'])
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
    if failure == 'offset':
        db.execute('UPDATE shows SET mapping=?', (json.dumps({'offset': 12}),))
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


def test_ugc_never_auto_matches_even_with_exact_title(library):
    _, player, key, _ = library
    title = player.db.show(1)['title']
    player.danmu = Mock(return_value={'animes': [
        {'animeId': 1, 'bangumiId': 'bvBV123', 'animeTitle': title},
        {'animeId': 2, 'type': 'B站视频·需核对版本', 'animeTitle': title},
    ]})
    assert player.auto_danmu(key)['status'] == 'needs_selection'
    assert all(call.args[0] == 'search/anime' for call in player.danmu.call_args_list)


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
