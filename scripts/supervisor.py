"""Per-user Windows mutex + restart supervisor. No credentials and no downloader reconfiguration.

One supervisor per user keeps one service process alive: it probes the cheap /api/health while
the child runs and restarts a hung child, stops the child cleanly on data/stop.request, spaces
restarts out with backoff, and keeps its own logs bounded. A valid service started independently
is left alone. Everything Windows-specific happens inside main().
"""
import json,os,subprocess,sys,time,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
STOP=ROOT/'data/stop.request'
PROBE_INTERVAL=30   # seconds between health probes while the child runs
PROBE_FAILURES=6    # consecutive failed probes before the child counts as hung (about three minutes)
GRACE=30            # seconds the child gets to leave on its own after stop.request
HEALTHY_RUN=600     # a child alive this long resets the restart backoff
LOG_LIMIT=5*1024*1024


def authority(root=ROOT):
    """Where the service listens: the deployment's bind_host, loopback for the wildcard or when unset."""
    try:host=json.loads((root/'data/deployment.json').read_text(encoding='utf-8')).get('bind_host') or '127.0.0.1'
    except Exception:host='127.0.0.1'
    if host in ('0.0.0.0','::'):host='127.0.0.1'
    return f'http://[{host}]:4871' if ':' in host else f'http://{host}:4871'


def probe(base,opener=None):
    """'ours' when AnimeService answers, 'other' when something else holds the port, 'down' otherwise."""
    opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    for path in ('/api/health','/api/status'):  # local health does not depend on outbound proxies
        try:
            with opener(base+path,timeout=3) as r:state=json.load(r)
            return 'ours' if isinstance(state,dict) and state.get('service')=='AnimeService' else 'other'
        except urllib.error.HTTPError as error:
            if error.code!=404:return 'other'
        except (urllib.error.URLError,OSError,ValueError):return 'down'
    return 'other'


def backoff(restarts):
    """Spacing between restarts: five seconds doubling to five minutes."""
    return min(300,5*2**min(max(restarts,1)-1,6))


def rotate(path,limit=LOG_LIMIT):
    """Keep one previous generation of a log once it grows past the limit."""
    try:
        if path.exists() and path.stat().st_size>limit:
            backup=path.with_name(path.name+'.1');backup.unlink(missing_ok=True);path.rename(backup)
    except OSError:pass


def log(message,path=None):
    path=path or ROOT/'logs/supervisor.log';rotate(path)
    with path.open('a',encoding='utf-8') as f:f.write(time.strftime('%Y-%m-%d %H:%M:%S')+' '+message+'\n')


def watch(process,base,stop=STOP,prober=probe,sleep=time.sleep,now=time.time,terminate=None,
          interval=PROBE_INTERVAL,failures=PROBE_FAILURES,grace=GRACE):
    """Follow one child until it exits, hangs or is asked to stop; returns 'exited', 'hung' or 'stopped'."""
    terminate=terminate or terminate_tree;started=now();next_probe=started+interval;misses=0
    while process.poll() is None:
        if stop.exists():
            deadline=now()+grace
            while process.poll() is None and now()<deadline:sleep(1)  # anime.run watches the same flag
            if process.poll() is None:terminate(process)
            return 'stopped'
        if now()>=next_probe:
            next_probe=now()+interval;misses=0 if prober(base)=='ours' else misses+1
            if misses>=failures:terminate(process);return 'hung'
        sleep(2)
    return 'exited'


def terminate_tree(process):
    import psutil
    try:
        for child in psutil.Process(process.pid).children(recursive=True):
            try:child.terminate()
            except psutil.NoSuchProcess:pass
    except psutil.NoSuchProcess:pass
    process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:process.kill()


def acquire_mutex(timeout=60):
    """Hold the single-supervisor mutex; while a stop is in progress, wait for the old supervisor to leave."""
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p];kernel.CreateMutexW.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    deadline=time.time()+timeout
    while True:
        handle=kernel.CreateMutexW(None,False,'Local\\AnimeService-Supervisor-v1')
        if ctypes.get_last_error()!=183:return handle
        kernel.CloseHandle(handle)
        if not STOP.exists() or time.time()>=deadline:return None
        time.sleep(2)


def main():
    if acquire_mutex() is None:return 0
    (ROOT/'data').mkdir(exist_ok=True);(ROOT/'logs').mkdir(exist_ok=True)
    STOP.unlink(missing_ok=True);(ROOT/'data/supervisor.pid').write_text(str(os.getpid()))
    base=authority();restarts=0;log(f'supervisor start pid={os.getpid()} probe={base}')
    while not STOP.exists():
        state=probe(base)
        if state=='ours':restarts=0;time.sleep(10);continue
        if state=='other':log('port 4871 occupied by another service');time.sleep(30);continue
        rotate(ROOT/'logs/process.log')
        with (ROOT/'logs/process.log').open('a',encoding='utf-8') as out:
            started=time.time()
            process=subprocess.Popen([str(ROOT/'.venv/Scripts/pythonw.exe'),'-m','anime.run'],cwd=ROOT,
                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdout=out,stderr=out)
            log(f'started anime.run pid={process.pid}')
            outcome=watch(process,base)
        log(f'anime.run pid={process.pid} {outcome} code={process.returncode}')
        if outcome=='stopped':break
        restarts=0 if time.time()-started>=HEALTHY_RUN else restarts+1
        wait=backoff(restarts) if restarts else 5
        log(f'restart in {wait}s (consecutive restarts {restarts})')
        for _ in range(wait):
            if STOP.exists():break
            time.sleep(1)
    log('supervisor exit')
    return 0


if __name__=='__main__':sys.exit(main())
