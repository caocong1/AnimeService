import socket,threading,time
from unittest.mock import Mock

import requests,uvicorn
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store
from anime.engine import Engine
from anime.run import listen_sockets


def test_real_ipv4_and_ipv6_localhost_listeners():
    async def fixture(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'fixture'})
    config=uvicorn.Config(fixture,host='127.0.0.1',port=0,lifespan='off',log_config=None,access_log=False)
    with listen_sockets(config) as sockets:
        assert len(sockets)==2
        assert sockets[1].getsockname()[0]=='::1'
        assert sockets[1].getsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY)==1
        port=sockets[0].getsockname()[1]
        server=uvicorn.Server(config)
        thread=threading.Thread(target=server.run,kwargs={'sockets':sockets},daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            assert server.started
            for url in (f'http://127.0.0.1:{port}',f'http://localhost:{port}',f'http://[::1]:{port}'):
                with requests.Session() as session:
                    session.trust_env=False
                    assert session.get(url,timeout=3).content==b'fixture'
        finally:
            server.should_exit=True;thread.join(timeout=5)
            assert not thread.is_alive()
    assert all(s.fileno()==-1 for s in sockets)


def test_ipv6_unavailable_keeps_ipv4_and_closes_sockets(monkeypatch):
    original=socket.socket
    def unavailable(family=socket.AF_INET,*args,**kwargs):
        if family==socket.AF_INET6:raise OSError('fixture disabled IPv6')
        return original(family,*args,**kwargs)
    monkeypatch.setattr('anime.run.socket.socket',unavailable)
    config=uvicorn.Config(Mock(),host='127.0.0.1',port=0,log_config=None)
    with listen_sockets(config) as sockets:
        assert len(sockets)==1 and sockets[0].getsockname()[0]=='127.0.0.1'
    assert sockets[0].fileno()==-1


def test_existing_ipv6_listener_is_not_bound_twice():
    config=uvicorn.Config(Mock(),host='::1',port=0,log_config=None)
    with listen_sockets(config) as sockets:
        assert len(sockets)==1 and sockets[0].getsockname()[0]=='::1'


def test_ipv6_localhost_retains_write_and_authority_checks(tmp_path):
    db=Store(tmp_path/'fixture.db');db.upsert_subject({'id':1,'name':'Example'})
    engine=Engine(db,qbit=Mock(),sources=Mock(),library=tmp_path/'library')
    engine.request_check=Mock(return_value=1)
    app=create_app(db,engine,start_worker=False)
    client=TestClient(app,base_url='http://localhost:4871',client=('::1',1234))
    assert client.get('/api/shows/1').status_code==200
    assert client.post('/api/check').status_code==403
    token=client.get('/api/bootstrap').json()['token']
    assert client.post('/api/check',headers={'X-Anime-Token':token,'Origin':'https://other.example'}).status_code==403
    assert client.post('/api/check',headers={'X-Anime-Token':token,'Origin':'http://localhost:4871'}).status_code==200
    engine.request_check.assert_called_once()
    assert client.get('/api/shows/1',headers={'Host':'other.example:4871'}).status_code==403
    assert not engine.qb.mock_calls
