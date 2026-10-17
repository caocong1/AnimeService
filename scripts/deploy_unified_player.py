"""One bounded idle reload. Never interrupts an active playback or download lease."""
import hashlib
import json
import sqlite3
import os
import shutil
import subprocess
import time
from pathlib import Path
import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/unified-player-20261016'
CODE = ('anime/finished_viewing.py', 'anime/webplayer.py', 'static/watch.js',
        'static/watch.html', 'static/core.js', 'static/finished-releases.js')


def binding():
    return {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in CODE}


def save(state, **extra):
    OUT.mkdir(exist_ok=True)
    (OUT/'deployment.json').write_text(json.dumps({'state':state, 'time':time.time(), **extra},
                                                 ensure_ascii=False, indent=2), encoding='utf-8')


def idle(c):
    now = time.time()
    if c.execute('SELECT 1 FROM subtitle_viewing_leases WHERE expires>?', (now,)).fetchone(): return False
    if c.execute('SELECT 1 FROM web_progress WHERE last_played>?', (now-180,)).fetchone(): return False
    if c.execute("SELECT 1 FROM thunder_queue WHERE lease!='' AND expires>?", (now,)).fetchone(): return False
    heartbeat = c.execute("SELECT value FROM kv WHERE key='heartbeat'").fetchone()
    if not heartbeat or json.loads(heartbeat[0]).get('phase') != 'idle': return False
    return not any((p.info['name'] or '').lower() in ('dandanplay.exe','mpv.exe','vlc.exe')
                   for p in psutil.process_iter(['name']))


def main():
    expected = binding()
    session = requests.Session(); session.trust_env = False
    identity = json.loads((ROOT/'static/finished-media/index.json').read_text(encoding='utf-8'))['releases'][0]['id']
    def ready():
        response = session.get('http://127.0.0.1:4871/api/web/media/finished:' + identity, timeout=5)
        if response.status_code == 404: return False
        response.raise_for_status()
        row = response.json()
        return row.get('release_id') == identity and row.get('viewing_session_supported') is True
    def finish(old_pid=None):
        save('backend_loaded_browser_check_running', old_pid=old_pid, code=expected)
        env = dict(os.environ)
        env['PLAYER_QA_BASE'] = 'http://127.0.0.1:4871'
        env['NODE_PATH'] = str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules')
        with (OUT/'live-verification.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([shutil.which('node'), 'scripts/verify_unified_player_live.cjs'],
                                    cwd=ROOT, env=env, stdout=log, stderr=log, timeout=300,
                                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        save('loaded_and_verified' if result.returncode == 0 else 'loaded_browser_check_failed',
             old_pid=old_pid, code=expected, browser_exit_code=result.returncode)
    c = sqlite3.connect('file:D:/AnimeService/data/anime.db?mode=ro', uri=True)
    save('waiting_for_idle', code=expected)
    deadline = time.time() + 4*3600
    while time.time() < deadline:
        if binding() != expected: save('stopped_code_changed'); return
        try:
            if ready(): finish(); return
            if not idle(c): time.sleep(15); continue
            supervisor = psutil.Process(int((ROOT/'data/supervisor.pid').read_text()))
            assert Path(supervisor.cwd()).resolve() == ROOT and any(Path(a).name == 'supervisor.py' for a in supervisor.cmdline())
            listeners = {x.pid for x in psutil.net_connections('tcp') if x.status == 'LISTEN' and x.laddr.port == 4871}
            assert len(listeners) == 1
            worker = psutil.Process(listeners.pop())
            assert Path(worker.cwd()).resolve() == ROOT and 'anime.run' in worker.cmdline()
            time.sleep(2)
            if not idle(c): continue
            if binding() != expected: save('stopped_code_changed'); return
            save('reloading_idle_worker', old_pid=worker.pid, code=expected)
            worker.terminate()
            for _ in range(45):
                time.sleep(2)
                try:
                    if ready(): finish(worker.pid); return
                except requests.RequestException: pass
            save('verification_failed_after_one_reload', code=expected); return
        except requests.RequestException: time.sleep(15)
    save('expired_without_interrupting_playback', code=expected)


if __name__ == '__main__':
    try: main()
    except Exception as exc: save('stopped_error', error_type=type(exc).__name__)
