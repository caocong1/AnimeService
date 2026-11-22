"""Single local uvicorn process; startup supervisor handles process restarts."""
import contextlib,logging,logging.handlers,os,socket,sys,threading
from .db import ROOT
# After stop.request uvicorn waits this long for in-flight responses (a paused player never
# finishes its range), then cancels them and runs the lifespan shutdown. Well under the
# supervisor's 30 s grace, which otherwise ends in a hard kill without that cleanup.
SHUTDOWN_TIMEOUT=10
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

def watch_stop(server,flag=None,interval=2):
    """Stop the server cleanly when the supervisor or the Stop script writes data/stop.request."""
    import threading,time
    flag=flag or ROOT/'data/stop.request'
    def poll():
        while not server.should_exit:
            if flag.exists():server.should_exit=True;break
            time.sleep(interval)
    threading.Thread(target=poll,daemon=True,name='stop-watch').start()

def server_config(host):
    import uvicorn
    return uvicorn.Config('anime.app:app',host=host,port=4871,access_log=False,log_config=None,proxy_headers=True,
                          forwarded_allow_ips='127.0.0.1',timeout_graceful_shutdown=SHUTDOWN_TIMEOUT)

def install_log_hygiene():
    """Drop asyncio's repeated send-after-lost warning; send uncaught thread and main errors to service.log too."""
    from .loop_hygiene import SendAfterLostFilter
    logging.getLogger('asyncio').addFilter(SendAfterLostFilter())
    log=logging.getLogger('anime')
    previous_thread,previous_main=threading.excepthook,sys.excepthook
    def thread_hook(args):
        if not issubclass(args.exc_type,SystemExit):
            name=args.thread.name if args.thread else '?'
            log.error('Unhandled exception in thread %s',name,exc_info=(args.exc_type,args.exc_value,args.exc_traceback))
        previous_thread(args)
    def main_hook(kind,value,traceback):
        if not issubclass(kind,KeyboardInterrupt):log.critical('Unhandled exception',exc_info=(kind,value,traceback))
        previous_main(kind,value,traceback)
    threading.excepthook,sys.excepthook=thread_hook,main_hook

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
    logging.basicConfig(handlers=[handler],level=logging.INFO);install_log_hygiene()
    import uvicorn
    from .deployment import load as load_deployment
    config=server_config(load_deployment().get('bind_host','127.0.0.1'))
    server=uvicorn.Server(config);watch_stop(server)
    with listen_sockets(config) as sockets:server.run(sockets=sockets)
if __name__=='__main__':main()
