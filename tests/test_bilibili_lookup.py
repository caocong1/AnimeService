import copy
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from anime.app import create_app
from anime.db import Store


BV = 'BV1fHh86tEug'
TITLE = '「冲晕过去了」第12话 官中（周更）无删减'
INFO = {'code': 0, 'data': {'bvid': BV, 'title': TITLE, 'owner': {'name': '测试UP主'}, 'pages': [
    {'cid': 42138796484, 'page': 1, 'part': '12', 'duration': 2020},
    {'cid': 42138862456, 'page': 2, 'part': '每周更新 感谢观看', 'duration': 11}]}}


@pytest.fixture
def lookup(tmp_path, monkeypatch):
    db = Store(tmp_path / 'lookup.db')
    client = TestClient(create_app(db, Mock(), start_worker=False), base_url='http://localhost:4871')
    upstream = Mock(status_code=200, ok=True, is_redirect=False)
    upstream.json.return_value = copy.deepcopy(INFO)
    get = Mock(return_value=upstream)
    monkeypatch.setattr('anime.bilibili_lookup.requests.get', get)
    return client, db, upstream, get


@pytest.mark.parametrize('ref,part', [(BV, 1), ('bv'+BV[2:], 1),
    ('https://www.bilibili.com/video/'+BV, 1), ('bilibili.com/video/'+BV+'?p=2', 2),
    ('https://m.bilibili.com/video/'+BV+'/?p=2&token=do-not-leak#secret', 2)])
def test_direct_lookup_is_independent_of_search_and_playback(lookup, ref, part):
    client, db, _, get = lookup
    result = client.get('/api/web/danmu/resolve', params={'q': ref})
    assert result.status_code == 200
    b = result.json()['bangumi']
    assert b['animeTitle'] == TITLE and b['episodeCount'] == 2
    assert b['uploader'] == '测试UP主'
    assert b['preferred_url'].endswith(f'?p={part}')
    assert b['episodes'][0]['episodeNumber'] == ''
    assert '33:40' in b['episodes'][0]['episodeTitle']
    assert '0:11' in b['episodes'][1]['episodeTitle']
    assert all(e['source_key'] and e['site'] == 'bilibili' for e in b['episodes'])
    assert 'do-not-leak' not in result.text
    args, kwargs = get.call_args
    assert args == ('https://api.bilibili.com/x/web-interface/view',)
    assert kwargs['params'] == {'bvid': BV} and kwargs['allow_redirects'] is False
    assert not any(k.lower() == 'cookie' for k in kwargs['headers'])
    for table in ('watches', 'web_progress', 'web_danmu_matches', 'tasks', 'shows'):
        assert db.rows(f'SELECT * FROM {table}') == []
    again = client.get('/api/web/danmu/resolve', params={'q': ref}).json()['bangumi']
    assert [x['source_key'] for x in again['episodes']] == [x['source_key'] for x in b['episodes']]


@pytest.mark.parametrize('ref', ['', '  ', 'BVbad', 'https://evil.test/video/'+BV,
    'https://www.bilibili.com.evil.test/video/'+BV, 'https://secret@www.bilibili.com/video/'+BV,
    'https://www.bilibili.com:4870/video/'+BV, 'http://127.0.0.1:4870/',
    'https://www.bilibili.com/video/'+BV+'?p=0', 'https://www.bilibili.com/video/'+BV+'?p=',
    'https://www.bilibili.com/video/'+BV+'?p=1&p=2', 'https://www.bilibili.com/video/'+BV+'?p=-1',
    'https://www.bilibili.com/video/'+BV+'?p=1.5', 'https://www.bilibili.com/video/'+BV+'?p=100000'])
def test_invalid_reference_is_rejected_before_network(lookup, ref):
    client, _, _, get = lookup
    result = client.get('/api/web/danmu/resolve', params={'q': ref})
    assert result.status_code == 400 and 'secret' not in result.text
    get.assert_not_called()


def test_original_search_box_accepts_bv_and_url(lookup):
    client, _, _, _ = lookup
    for ref in (BV, 'https://www.bilibili.com/video/'+BV):
        assert client.get('/api/web/danmu/search', params={'q': ref}).json()['bangumi']['animeTitle'] == TITLE


def test_nonexistent_part_is_not_silently_replaced_with_p1(lookup):
    client, _, _, _ = lookup
    result = client.get('/api/web/danmu/resolve', params={'q': f'https://www.bilibili.com/video/{BV}?p=3'})
    assert result.status_code == 400


@pytest.mark.parametrize('code,status', [(-404,404), (62002,404), (-412,502), (-101,502)])
def test_upstream_codes_are_safe_request_errors(lookup, code, status):
    client, db, upstream, _ = lookup
    upstream.json.return_value = {'code': code, 'message': 'private-url?token=secret'}
    result = client.get('/api/web/danmu/resolve', params={'q': BV})
    assert result.status_code == status and 'secret' not in result.text
    assert db.rows('SELECT * FROM web_danmu_sources') == []


@pytest.mark.parametrize('error,status', [(requests.Timeout,504), (requests.ConnectionError,503)])
def test_transient_failure_can_recover(lookup, error, status):
    client, _, _, get = lookup
    get.side_effect = error('private-url?token=secret')
    result = client.get('/api/web/danmu/resolve', params={'q': BV})
    assert result.status_code == status and 'secret' not in result.text
    get.side_effect = None
    assert client.get('/api/web/danmu/resolve', params={'q': BV}).status_code == 200


@pytest.mark.parametrize('data', [[], {'code':0}, {'code':0,'data':None},
    {'code':0,'data':{'title':TITLE,'pages':[]}}, {'code':0,'data':{'title':TITLE,'pages':[{}]}}])
def test_malformed_metadata_does_not_register_sources(lookup, data):
    client, db, upstream, _ = lookup
    upstream.json.return_value = data
    assert client.get('/api/web/danmu/resolve', params={'q': BV}).status_code == 502
    assert db.rows('SELECT * FROM web_danmu_sources') == []


@pytest.mark.parametrize('fault', ['wrong_video', 'duplicate_page'])
def test_inconsistent_metadata_is_not_bound(lookup, fault):
    client, db, upstream, _ = lookup
    info = copy.deepcopy(INFO)
    if fault == 'wrong_video':
        info['data']['bvid'] = 'BV1EVap6yE6B'
    else:
        info['data']['pages'][1]['page'] = 1
    upstream.json.return_value = info
    assert client.get('/api/web/danmu/resolve', params={'q': BV}).status_code == 502
    assert db.rows('SELECT * FROM web_danmu_sources') == []


def test_source_metadata_counts_only_the_selected_part_and_caches(lookup):
    client, db, upstream, get = lookup
    b = client.get('/api/web/danmu/resolve', params={'q': BV}).json()['bangumi']
    before = {table: db.rows(f'SELECT * FROM {table}') for table in
              ('watches', 'web_progress', 'web_danmu_matches', 'tasks', 'shows')}
    response = Mock(ok=True, is_redirect=False, content=b'\x40\x7b')
    get.return_value = response
    for _ in range(2):
        info = client.get('/api/web/danmu/source/'+b['episodes'][1]['source_key']+'/info')
        assert info.status_code == 200
        assert info.json() == {'uploader': '测试UP主', 'count': 123}
    assert get.call_count == 2  # one video lookup + one small count; no comments
    args, kwargs = get.call_args
    assert args == ('https://api.bilibili.com/x/v2/dm/web/view',)
    assert kwargs['params'] == {'type': 1, 'oid': 42138862456}
    assert kwargs['allow_redirects'] is False
    assert 'Cookie' not in kwargs['headers']
    assert before == {table: db.rows(f'SELECT * FROM {table}') for table in before}


def test_source_metadata_failure_is_unknown_and_retains_uploader(lookup):
    client, _, _, get = lookup
    b = client.get('/api/web/danmu/resolve', params={'q': BV}).json()['bangumi']
    get.side_effect = requests.Timeout('token=secret')
    for _ in range(2):
        info = client.get('/api/web/danmu/source/'+b['episodes'][0]['source_key']+'/info')
        assert info.status_code == 200 and info.json()['count'] is None
        assert info.json()['uploader'] == '测试UP主' and 'secret' not in info.text
    assert get.call_count == 2  # failure backoff also avoids repeated calls


def test_source_metadata_requires_registered_safe_url(lookup):
    client, db, _, get = lookup
    assert client.get('/api/web/danmu/source/unknown/info').status_code == 404
    for i, url in enumerate(('https://evil.test/video/'+BV, 'https://secret@www.bilibili.com/video/'+BV,
                              'https://www.bilibili.com:4870/video/'+BV, 'https://ani.gamer.com.tw/animeVideo.php?sn=1')):
        db.execute('INSERT INTO web_danmu_sources VALUES(?,?)', (str(i), url))
        assert client.get(f'/api/web/danmu/source/{i}/info').json()['count'] is None
    get.assert_not_called()


def test_pgc_count_matches_episode_id_without_inventing_uploader(lookup):
    client, db, _, get = lookup
    db.execute('INSERT INTO web_danmu_sources VALUES(?,?)', ('pgc', 'https://www.bilibili.com/bangumi/play/ep42'))
    season = Mock(ok=True, is_redirect=False)
    season.json.return_value = {'code': 0, 'result': {'episodes': [{'id':41,'cid':100}, {'id':42,'cid':200}]}}
    count = Mock(ok=True, is_redirect=False, content=b'\x40\x00')
    get.side_effect = [season, count]
    assert client.get('/api/web/danmu/source/pgc/info').json() == {'uploader':'', 'count':0}
    assert get.call_args.kwargs['params'] == {'type':1, 'oid':200}


@pytest.mark.parametrize('body,expected', [(b'\x40\x00',0), (b'\x40\x7b',123),
    (b'\x22\x00',0), (b'\x12\x03abc\x40\x09',9),
    (b'\x59'+b'0'*8+b'\x65'+b'0'*4+b'\x40\x03',3)])
def test_count_parser_handles_zero_and_skips_unknown_fields(body, expected):
    from anime.bilibili_lookup import danmaku_count
    assert danmaku_count(body) == expected


@pytest.mark.parametrize('body', [b'', b'<html>blocked</html>', b'\x40\x80', b'\x12\x05abc',
    b'\x00', b'\x0f', b'\x08\x01\x40\x00', b'\x12\x03abc', b'x'*262145], ids=lambda body: f'payload-{len(body)}')
def test_count_parser_never_turns_unavailable_data_into_zero(body):
    from anime.bilibili_lookup import danmaku_count
    with pytest.raises(ValueError):danmaku_count(body)
