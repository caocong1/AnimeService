"""Media file responses stop reading once the client disconnects; full, ranged and cached replies are unchanged."""
import socket,struct,threading,time

import anyio,pytest,uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from anime.media_response import MediaFileResponse,MediaStaticFiles

CHUNK=64*1024


@pytest.fixture
def media(tmp_path):
    path=tmp_path/'episode.mkv'
    path.write_bytes(bytes(range(256))*(16*1024*1024//256))  # 16 MiB, every offset distinguishable
    return path


def sends_after_disconnect(response_class,path,headers=()):
    """Run the response with a client that disconnects after the first body chunk; count later body sends."""
    gone=anyio.Event();counts={'before':0,'after':0}
    async def receive():
        if not counts.get('started'):
            counts['started']=True;return {'type':'http.request','body':b'','more_body':False}
        await gone.wait();return {'type':'http.disconnect'}
    async def send(message):
        if message['type']=='http.response.body':
            counts['after' if gone.is_set() else 'before']+=1
            gone.set()
        await anyio.sleep(0)  # a real transport yields on every write
    scope={'type':'http','method':'GET','path':'/','headers':list(headers),'asgi':{'spec_version':'2.3'}}
    anyio.run(response_class(path),scope,receive,send)
    return counts


def test_aborted_range_stops_reading(media):
    full=media.stat().st_size//CHUNK
    assert sends_after_disconnect(FileResponse,media,[(b'range',b'bytes=0-')])['after']==full-1  # the defect
    counts=sends_after_disconnect(MediaFileResponse,media,[(b'range',b'bytes=0-')])
    assert counts['before']==1 and counts['after']<=2
    assert sends_after_disconnect(MediaFileResponse,media)['after']<=2


def test_real_server_stops_after_client_reset(tmp_path):
    path=tmp_path/'sparse.bin'
    with path.open('wb') as f:f.truncate(64*1024*1024)
    sent={'bytes':0,'done':False}
    async def app(scope,receive,send):
        if scope['type']!='http':return
        async def counting(message):
            if message['type']=='http.response.body':sent['bytes']+=len(message.get('body',b''))
            await send(message)
        try:await MediaFileResponse(path)(scope,receive,counting)
        finally:sent['done']=True
    config=uvicorn.Config(app,host='127.0.0.1',port=0,lifespan='off',log_config=None,access_log=False)
    server=uvicorn.Server(config);sock=config.bind_socket();port=sock.getsockname()[1]
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        client=socket.create_connection(('127.0.0.1',port))
        client.sendall(b'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nRange: bytes=0-\r\n\r\n')
        got=0
        while got<1024*1024:got+=len(client.recv(65536))
        client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));client.close()
        for _ in range(100):
            if sent['done']:break
            time.sleep(.05)
        assert sent['done'] and sent['bytes']<32*1024*1024
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()


@pytest.fixture
def client(media):
    app=FastAPI()
    @app.get('/plain')
    def plain():return FileResponse(media,media_type='video/x-matroska',headers={'Cross-Origin-Resource-Policy':'same-origin'})
    @app.get('/media')
    def streamed():return MediaFileResponse(media,media_type='video/x-matroska',headers={'Cross-Origin-Resource-Policy':'same-origin'})
    @app.get('/missing')
    def missing():return MediaFileResponse(media.with_name('missing.mkv'))
    app.mount('/old',StaticFiles(directory=media.parent))
    app.mount('/new',MediaStaticFiles(directory=media.parent))
    return TestClient(app)


@pytest.mark.parametrize('method,headers',[
    ('GET',{}),('GET',{'Range':'bytes=100-2000099'}),('GET',{'Range':'bytes=0-'}),('HEAD',{}),
    ('GET',{'Range':'bytes=0-9,20-29'}),('GET',{'Range':'bytes=99999999-'}),('GET',{'Range':'chars=1-2'})])
def test_replies_are_identical_to_file_response(client,method,headers):
    for old,new in (('/plain','/media'),('/old/episode.mkv','/new/episode.mkv')):
        a=client.request(method,old,headers=headers);b=client.request(method,new,headers=headers)
        assert a.status_code==b.status_code
        if 'multipart' in a.headers.get('content-type',''):
            assert a.headers['content-length']==b.headers['content-length']  # random boundary per reply
        else:
            assert a.content==b.content and dict(a.headers)==dict(b.headers)


def test_static_not_modified_is_kept(client):
    etag=client.get('/new/episode.mkv').headers['etag']
    reply=client.get('/new/episode.mkv',headers={'If-None-Match':etag})
    assert reply.status_code==304 and reply.content==b''


def test_missing_file_error_is_not_wrapped(client):
    with pytest.raises(RuntimeError,match='does not exist'):client.get('/missing')


def test_app_serves_static_media_through_the_disconnect_aware_mount(tmp_path):
    from unittest.mock import Mock
    from anime.app import create_app
    from anime.db import Store
    app=create_app(Store(tmp_path/'test.db'),Mock(),start_worker=False)
    mount=next(route for route in app.routes if getattr(route,'path',None)=='/static')
    assert isinstance(mount.app,MediaStaticFiles)
