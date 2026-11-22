"""anime.run's server options bound a graceful stop; its log hygiene keeps real errors visible."""
import contextlib,logging,socket,sys,threading,time

import pytest
from fastapi import FastAPI

from anime import run
from anime.media_response import MediaFileResponse


def test_shutdown_timeout_fits_inside_the_supervisor_grace():
    import scripts.supervisor as supervisor
    config=run.server_config('127.0.0.1')
    assert config.timeout_graceful_shutdown==run.SHUTDOWN_TIMEOUT
    assert 0<run.SHUTDOWN_TIMEOUT<supervisor.GRACE
    assert (config.host,config.port,config.access_log,config.proxy_headers,config.forwarded_allow_ips)==('127.0.0.1',4871,False,True,'127.0.0.1')


def test_paused_player_no_longer_blocks_stop(tmp_path):
    path=tmp_path/'paused.bin'
    with path.open('wb') as f:f.truncate(256*1024*1024)
    state={'shutdown':False}
    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        state['shutdown']=True
    app=FastAPI(lifespan=lifespan)
    @app.get('/stream')
    def stream():return MediaFileResponse(path)
    config=run.server_config('127.0.0.1');config.app=app;config.port=0;config.timeout_graceful_shutdown=1
    import uvicorn
    server=uvicorn.Server(config);sock=config.bind_socket();port=sock.getsockname()[1]
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        client=socket.create_connection(('127.0.0.1',port))
        client.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
        client.sendall(b'GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\nRange: bytes=0-\r\n\r\n')
        client.recv(4096)  # then stop reading, like a paused player with a full buffer
        time.sleep(.3)
        started=time.monotonic();server.should_exit=True;thread.join(timeout=8)
        assert not thread.is_alive() and time.monotonic()-started<5 and state['shutdown']
        client.close()
    finally:
        server.should_exit=True;sock.close()


def test_log_hygiene_keeps_errors_and_drops_only_send_noise(monkeypatch):
    seen=[];forwarded=[]
    monkeypatch.setattr(threading,'excepthook',lambda args:forwarded.append(args.exc_type))
    monkeypatch.setattr(sys,'excepthook',lambda *a:forwarded.append(a[0]))
    asyncio_log=logging.getLogger('asyncio')
    monkeypatch.setattr(asyncio_log,'filters',[])
    class Keep(logging.Handler):
        def emit(self,record):seen.append((record.name,record.levelname,record.getMessage()))
    keep=Keep();root=logging.getLogger()
    root.addHandler(keep);old_level=root.level;root.setLevel(logging.INFO)
    try:
        run.install_log_hygiene()
        asyncio_log.warning('socket.send() raised exception.')
        asyncio_log.error('Exception in callback something_else()')
        worker=threading.Thread(target=lambda:1/0,name='anime-worker');worker.start();worker.join()
        threading.Thread(target=sys.exit,name='quiet').start()
        try:raise OSError('disk I/O error')
        except OSError:sys.excepthook(*sys.exc_info())
    finally:root.removeHandler(keep);root.setLevel(old_level)
    assert ('asyncio','ERROR','Exception in callback something_else()') in seen
    assert not any(m=='socket.send() raised exception.' for _,_,m in seen)
    assert ('anime','ERROR','Unhandled exception in thread anime-worker') in seen
    assert ('anime','CRITICAL','Unhandled exception') in seen
    assert not any('quiet' in m for _,_,m in seen)
    assert ZeroDivisionError in forwarded and OSError in forwarded  # process.log still gets the original output
