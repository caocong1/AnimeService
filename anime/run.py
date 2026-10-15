"""Single local uvicorn process; startup supervisor handles process restarts."""
import contextlib,logging,logging.handlers,os,socket
from .db import ROOT
@contextlib.contextmanager
def listen_sockets(config):
    """Keep the configured listener and serve localhost's IPv6 loopback as well."""
    primary=config.bind_socket();sockets=[primary]
    try:
        if primary.family==socket.AF_INET:
            extra=None
            try:
                extra=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
                extra.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                extra.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                extra.bind(('::1',primary.getsockname()[1]))
                extra.set_inheritable(True)
            except OSError:
                if extra is not None:extra.close()
                logging.getLogger(__name__).warning('IPv6 loopback unavailable; keeping configured listener')
            else:
                sockets.append(extra)
                logging.getLogger(__name__).info('Localhost IPv6 loopback listener enabled')
        yield sockets
    finally:
        for sock in sockets:sock.close()

def main():
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p];kernel.CreateMutexW.restype=ctypes.c_void_p
    mutex=kernel.CreateMutexW(None,False,'Local\\AnimeService-Worker-v1')
    if ctypes.get_last_error()==183:return
    (ROOT/'data/service.pid').write_text(str(os.getpid()))
    (ROOT/'logs').mkdir(exist_ok=True)
    handler=logging.handlers.RotatingFileHandler(ROOT/'logs/service.log',maxBytes=2_000_000,backupCount=4,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(handlers=[handler],level=logging.INFO)
    import uvicorn
    from .deployment import load as load_deployment
    config=uvicorn.Config('anime.app:app',host=load_deployment().get('bind_host','127.0.0.1'),port=4871,
                access_log=False,log_config=None,proxy_headers=True,forwarded_allow_ips='127.0.0.1')
    with listen_sockets(config) as sockets:uvicorn.Server(config).run(sockets=sockets)
if __name__=='__main__':main()
