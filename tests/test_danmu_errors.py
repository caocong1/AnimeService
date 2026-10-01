"""Foreground source failures must not become a worker outage or leak URLs."""
import requests
import pytest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store


@pytest.fixture
def client_db(tmp_path):
    db=Store(tmp_path/'test.db')
    client=TestClient(create_app(db,Mock(),start_worker=False),base_url='http://localhost:4871')
    return client,db


@pytest.mark.parametrize('status,expected',[(404,404),(500,502),(403,502),(429,502)])
def test_source_http_failure_is_local_to_request_and_can_recover(client_db,monkeypatch,status,expected):
    client,db=client_db
    response=requests.Response();response.status_code=status
    response.url='https://example.test/private?token=must-not-leak'
    response._content=b'secret upstream response'
    get=Mock(return_value=response)
    monkeypatch.setattr('anime.webplayer.requests.get',get)
    failed=client.get('/api/web/danmu/show/49902')
    assert failed.status_code==expected and '弹幕' in failed.json()['detail']
    assert 'must-not-leak' not in failed.text and 'secret' not in failed.text
    assert db.rows('SELECT * FROM events')==[]
    response.status_code=200;response._content=b'{"bangumi":{"episodes":[]}}'
    recovered=client.get('/api/web/danmu/show/49902')
    assert recovered.status_code==200 and recovered.json()['bangumi']['episodes']==[]
    assert db.rows('SELECT * FROM watches')==[]
    assert db.rows('SELECT * FROM tasks')==[]


@pytest.mark.parametrize('error,expected',[(requests.ConnectionError,503),(requests.Timeout,504)])
def test_adapter_network_failure_returns_safe_retry_message(client_db,monkeypatch,error,expected):
    client,db=client_db
    monkeypatch.setattr('anime.webplayer.requests.get',Mock(side_effect=error('https://private.test/?token=must-not-leak')))
    response=client.get('/api/web/danmu/search?q=Example')
    assert response.status_code==expected and '重试' in response.json()['detail']
    assert 'must-not-leak' not in response.text
    assert db.rows('SELECT * FROM events')==[]


@pytest.mark.parametrize('content',[b'not-json',b'[]'])
def test_invalid_adapter_response_is_a_request_error(client_db,monkeypatch,content):
    client,db=client_db
    upstream=requests.Response();upstream.status_code=200;upstream._content=content
    monkeypatch.setattr('anime.webplayer.requests.get',Mock(return_value=upstream))
    response=client.get('/api/web/danmu/show/49902')
    assert response.status_code==502 and '格式异常' in response.json()['detail']
    assert db.rows('SELECT * FROM events')==[]


def test_api_history_does_not_hide_live_worker_or_task_failure(client_db):
    client,db=client_db
    db.event('api','HTTPError：请求失败，请检查网络或本机接口','error')
    db.event('service','worker failure','error')
    db.execute("INSERT INTO tasks(hash,show_id,status,error) VALUES('abc',42,'intent','task failure')")
    db.event('task:abc','task failure','error')
    # A heartbeat alone cannot turn a worker failure into a success.
    db.set('heartbeat',{'time':9999999999,'phase':'idle'})
    events=client.get('/api/status').json()['events']
    api=next(e for e in events if e['scope']=='api')
    assert api['level']=='warning' and api['request_failure'] and api['original_level']=='error'
    assert not api.get('historical')
    assert all(e['level']=='error' for e in events if e['scope'] in ('service','task:abc'))
    assert db.one("SELECT level FROM events WHERE scope='api'")['level']=='error'
