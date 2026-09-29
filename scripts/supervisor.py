"""Per-user Windows mutex + restart supervisor. No credentials and no downloader reconfiguration."""
import ctypes,json,os,subprocess,time,urllib.request
import psutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
kernel.CreateMutexW.restype=ctypes.c_void_p
mutex=kernel.CreateMutexW(None,False,'Local\\AnimeService-Supervisor-v1')
if ctypes.get_last_error()==183:raise SystemExit(0)
(ROOT/'data').mkdir(exist_ok=True);(ROOT/'logs').mkdir(exist_ok=True)
stop=ROOT/'data/stop.request'
if stop.exists():stop.unlink()
(ROOT/'data/supervisor.pid').write_text(str(os.getpid()))
while not stop.exists():
    # An independently launched valid service is allowed to keep running.
    try:
        with urllib.request.urlopen('http://127.0.0.1:4871/api/status',timeout=3) as r:
            state=json.load(r)
        if state.get('service')=='AnimeService':time.sleep(10);continue
        raise RuntimeError('port occupied')
    except RuntimeError:
        with (ROOT/'logs/supervisor.log').open('a',encoding='utf-8') as f:f.write(time.ctime()+' port 4871 occupied by another service\n')
        time.sleep(30);continue
    except Exception:pass
    with (ROOT/'logs/process.log').open('a',encoding='utf-8') as log:
        p=subprocess.Popen([str(ROOT/'.venv/Scripts/pythonw.exe'),'-m','anime.run'],cwd=ROOT,
             creationflags=subprocess.CREATE_NO_WINDOW,stdout=log,stderr=log)
        while p.poll() is None and not stop.exists():time.sleep(2)
        if stop.exists() and p.poll() is None:
            for child in psutil.Process(p.pid).children(recursive=True):
                try:child.terminate()
                except psutil.NoSuchProcess:pass
            p.terminate();p.wait(timeout=10)
    if not stop.exists():time.sleep(5)
