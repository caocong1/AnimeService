"""Lightweight process health and small operator summaries from the local database."""
import os,shutil,subprocess,sys,threading,time
from .db import ROOT,DB_ERRORS

STARTED_AT=time.time()


def code_version():
    try:
        return subprocess.run(['git','rev-parse','--short','HEAD'],cwd=ROOT,capture_output=True,text=True,timeout=5).stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


VERSION=code_version()


def problems(db,now):
    """What the health dot shows: review count, uncertain submissions, the newest actionable error and a downloader blocker."""
    from .download_policy import submission_blocker
    reviews=db.rows("""SELECT t.hash,CASE WHEN t.provider='thunder' AND q.action='submit' AND q.committing=1
        AND t.acknowledged=0 AND t.external_id IS NULL THEN 1 ELSE 0 END uncertain
        FROM tasks t LEFT JOIN thunder_queue q ON q.hash=t.hash WHERE t.status='review'""")
    history=db.get('dandan_history_health') or {};error=None
    for event in db.rows("""SELECT e.scope,e.message,e.time,t.status task_status,t.error task_error FROM events e
        LEFT JOIN tasks t ON substr(e.scope,1,5)='task:' AND t.hash=substr(e.scope,6) WHERE e.level='error' ORDER BY e.id DESC LIMIT 60"""):
        if event['scope']=='api' or now-event['time']>=3600:continue
        if event['scope']=='dandan-history' and history.get('ok') is True and history.get('success',0)>event['time']:continue
        if event['scope'].startswith('task:') and (event['task_status'] in ('review','complete','cleaned','linked') or not event['task_error']) and event['task_status'] is not None:continue
        error={'scope':event['scope'],'message':event['message'],'time':event['time']};break
    return {'reviews':len(reviews),'uncertain':sum(r['uncertain'] for r in reviews),'error':error,'blocker':submission_blocker(db)}


def health(db,now=None,*,library=None):
    """Liveness, version and operator summaries; /api/status stays the detailed view."""
    now=now or time.time();heartbeat=db.get('heartbeat') or {};maintenance=db.get('download_maintenance_at',0)
    age=round(now-heartbeat['time']) if heartbeat.get('time') else None
    worker=any(t.name=='anime-worker' and t.is_alive() for t in threading.enumerate())
    storage=None;data=None;reserve=db.get('minimum_free_gb',10)
    if library is not None:
        try:
            available=shutil.disk_usage(library.anchor or library.parent).free
            storage={'library':str(library),'free_gib':round(available/2**30,2),'reserve_gib':reserve,'below_reserve':available<reserve*2**30}
        except OSError:pass
    try:  # the volume holding the database, its WAL and the subtitle/source archives
        available=shutil.disk_usage(db.path.parent).free
        data={'free_gib':round(available/2**30,2),'reserve_gib':reserve,'below_reserve':available<reserve*2**30}
    except OSError:pass
    return {'service':'AnimeService','ok':age is not None and age<=max(600,3*db.get('poll_seconds',180)),'time':now,
            'pid':os.getpid(),'started_at':STARTED_AT,'uptime':round(now-STARTED_AT),'code_version':VERSION,'download_storage':storage,
            'data_storage':data,'db_errors':dict(DB_ERRORS),
            'python':sys.version.split()[0],'worker_thread':worker,'download_gap_reconciliation':3,
            'tracking_inbox':any(t.name=='tracking-inbox' and t.is_alive() for t in threading.enumerate()),
            'heartbeat':{'phase':heartbeat.get('phase'),'age':age},
            'maintenance_age':round(now-maintenance) if maintenance else None,
            'qbit':(db.get('qbit_health') or {}).get('ok'),'subtitle_studio':(db.get('subtitle_studio_health') or {}).get('ok'),
            'cycle_errors':db.get('cycle_errors') or {},'problems':problems(db,now)}
