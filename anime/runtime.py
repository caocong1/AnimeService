"""Process-level facts for a cheap health check: version, start time and liveness from kv only."""
import os,subprocess,sys,threading,time
from .db import ROOT

STARTED_AT=time.time()


def code_version():
    try:
        return subprocess.run(['git','rev-parse','--short','HEAD'],cwd=ROOT,capture_output=True,text=True,timeout=5).stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


VERSION=code_version()


def health(db,now=None):
    """Liveness and version without table scans: /api/status stays the heavy, detailed view."""
    now=now or time.time();heartbeat=db.get('heartbeat') or {};maintenance=db.get('download_maintenance_at',0)
    age=round(now-heartbeat['time']) if heartbeat.get('time') else None
    worker=any(t.name=='anime-worker' and t.is_alive() for t in threading.enumerate())
    return {'service':'AnimeService','ok':age is not None and age<=max(600,3*db.get('poll_seconds',180)),'time':now,
            'pid':os.getpid(),'started_at':STARTED_AT,'uptime':round(now-STARTED_AT),'code_version':VERSION,
            'python':sys.version.split()[0],'worker_thread':worker,
            'heartbeat':{'phase':heartbeat.get('phase'),'age':age},
            'maintenance_age':round(now-maintenance) if maintenance else None,
            'qbit':(db.get('qbit_health') or {}).get('ok'),'subtitle_studio':(db.get('subtitle_studio_health') or {}).get('ok'),
            'cycle_errors':db.get('cycle_errors') or {}}
