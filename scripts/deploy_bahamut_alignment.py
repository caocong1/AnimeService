"""One bounded supervised reload, only after playback and the backend are idle."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import psutil
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.deploy_unified_player import idle

CODE=('anime/audio_alignment.py','anime/bahamut_alignment.py','anime/webplayer.py',
      'static/watch.js','static/watch.html','scripts/deploy_bahamut_alignment.py')
OUT=ROOT/'data/diagnostics/bahamut-alignment-20261016'


def ready(session):
    r=session.get('http://127.0.0.1:4871/api/web/alignment-capabilities',timeout=5)
    if r.status_code==404:return False
    r.raise_for_status()
    return r.json().get('bahamut_reference')=='bounded-hls-v1'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--timeout',type=int,default=1800)
    args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    lock=OUT/'deployment.lock'
    try:
        with lock.open('x') as f:f.write(str(os.getpid()))
    except FileExistsError:return
    binding=lambda:{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in CODE}
    expected=binding()
    def save(state,**extra):
        (OUT/'deployment.json').write_text(json.dumps({'state':state,'time':time.time(),
            'code':expected,**extra},ensure_ascii=False,indent=2),encoding='utf-8')
    s=requests.Session();s.trust_env=False
    c=sqlite3.connect((ROOT/'data/anime.db').as_uri()+'?mode=ro',uri=True)
    try:
        end=time.monotonic()+args.timeout
        save('waiting_for_idle')
        while time.monotonic()<end:
            if binding()!=expected:save('stopped_code_changed');return
            try:
                if ready(s):save('loaded_verified');return
            except requests.RequestException:time.sleep(5);continue
            if not idle(c):time.sleep(10);continue
            supervisor=psutil.Process(int((ROOT/'data/supervisor.pid').read_text()))
            assert Path(supervisor.cwd()).resolve()==ROOT and any(Path(a).name=='supervisor.py' for a in supervisor.cmdline())
            listeners={x.pid for x in psutil.net_connections('tcp') if x.status=='LISTEN' and x.laddr.port==4871}
            assert len(listeners)==1
            worker=psutil.Process(listeners.pop())
            assert Path(worker.cwd()).resolve()==ROOT and 'anime.run' in worker.cmdline()
            time.sleep(2)
            if binding()!=expected:save('stopped_code_changed');return
            if not idle(c):continue
            save('reloading_idle_backend',old_pid=worker.pid)
            worker.terminate()
            for _ in range(45):
                time.sleep(2)
                try:
                    if ready(s):save('loaded_verified',old_pid=worker.pid);return
                except requests.RequestException:pass
            save('verification_failed_after_one_reload',old_pid=worker.pid);return
        save('expired_without_interrupting_playback')
    except Exception as exc:
        save('stopped_error',error_type=type(exc).__name__)
        raise
    finally:
        s.close();c.close();lock.unlink(missing_ok=True)


if __name__=='__main__':main()
