"""Restore only the original local qB process; never submit/control torrents."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import psutil

EXE = Path('C:/Program Files/qBittorrent/qbittorrent.exe')
PROFILE = Path('D:/MediaService/qbit-profile')


@contextmanager
def recovery_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: pass
            else: acquired = True
        else:
            import fcntl
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: pass
            else: acquired = True
        yield acquired
    finally:
        if acquired and os.name == 'nt':
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def absence():
    """Require positive process enumeration AND a refused loopback connection."""
    try:
        # psutil caches Process objects (including Windows names). Do not let
        # a reused PID inherit the previous process's cached name.
        clear_cache = getattr(psutil.process_iter,'cache_clear',None)
        if clear_cache is not None: clear_cache()
        for process in psutil.process_iter():
            try:
                if process.name().lower() == 'qbittorrent.exe':
                    return 'process_present'
            except psutil.NoSuchProcess: continue
    except (psutil.Error, OSError): return 'process_unknown'
    try:
        # Windows can take two seconds to return WSAECONNREFUSED even on
        # loopback. A timeout still cannot authorize starting the downloader.
        with socket.create_connection(('127.0.0.1', 4870), timeout=4): pass
    except ConnectionRefusedError: return 'absent'
    except OSError: return 'port_unknown'
    return 'port_occupied'


def launch():
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return subprocess.Popen([str(EXE), '--profile='+str(PROFILE), '--webui-port=4870'],
                            startupinfo=startup, close_fds=True)


def restore_original(state_dir):
    directory = Path(state_dir)
    with recovery_lock(directory/'qbit-restore.lock') as locked:
        if not locked: return {'state':'busy'}
        observed = absence()
        if observed != 'absent': return {'state':observed}
        if not EXE.is_file() or not (PROFILE/'qBittorrent/config/qBittorrent.ini').is_file():
            return {'state':'config_missing'}
        ledger = directory/'qbit-restore.json'
        try: history = json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else []
        except (OSError, ValueError): return {'state':'ledger_unknown'}
        if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
            return {'state':'ledger_unknown'}
        prior = history[-1] if history else {}
        if prior.get('state') in ('launching','unknown'): return {'state':'launch_unknown'}
        now = time.time()
        if now < prior.get('next_attempt', 0):
            return {'state':'cooldown','next_attempt':prior['next_attempt']}
        # Recheck after reading policy/receipt and immediately before the launch intent.
        observed = absence()
        if observed != 'absent': return {'state':observed}
        row = {'state':'launching','time':now,'next_attempt':now+min(900,60*2**min(len(history),4)),
               'profile':str(PROFILE),'port':4870}
        history.append(row)
        def save():
            temporary = ledger.with_suffix('.tmp')
            with temporary.open('w',encoding='utf-8') as stream:
                json.dump(history,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,ledger)
        save()  # Uncertain launches survive worker termination/restarts, never replay blindly.
        try:
            process = launch()
        except OSError:
            row['state'] = 'launch_failed'
        except Exception:
            row['state'] = 'unknown'
        else:
            row.update(state='launched',pid=process.pid)
        save()
        return dict(row)


def maintain_original(engine):
    db = engine.db
    if (not db.get('qbit_auto_restore_enabled',False) or db.get('downloader_preference','qbit')!='qbit'
            or engine.stop_event.is_set() or (db.path.parent/'stop.request').exists()): return
    result = restore_original(db.path.parent)
    previous = db.get('qbit_restore_status',{})
    db.set('qbit_restore_status',{**result,'checked_at':time.time()})
    if previous.get('state') != result['state']:
        db.event('qbit-recovery','原qB自动恢复：'+result['state'],
                 'info' if result['state'] in ('process_present','launched') else 'warning')
