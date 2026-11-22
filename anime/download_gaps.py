"""Explain missing ordinary episodes and retry them through the existing Engine.

No downloader instance, source policy, review, viewing flag or writer is created
here. Discovery still belongs to the normal rate-limited source scheduler.
"""
import time
from .native_indexes import candidate_mapping
from .releases import validate_title,Rejected
from .net import safe_error

def qbit_retry(record):
    # Exact legacy diagnostic only; unrelated site failures keep their cooldown.
    return record.get('dependency')=='qbit_unavailable' or record.get('reason')=='qBittorrent请求超时或连接不可达'


def inspect_missing(engine,sid):
    missing=engine.gaps(sid)['missing']
    if not missing:return {},[]
    db=engine.db;now=time.time();policy=engine.source_review.policy(sid)
    sources=db.rows('SELECT id,enabled,last_success,last_attempt,error FROM sources WHERE show_id=?',(sid,))
    enabled={s['id'] for s in sources if s['enabled']}
    candidates=db.rows('SELECT * FROM candidates WHERE show_id=? ORDER BY id DESC',(sid,))
    mapping=db.mapping(sid);by_episode={}
    for c in candidates:
        if c['source_id'] not in enabled:continue
        try:
            eps=validate_title(c['title'],candidate_mapping(mapping,c),{},False)
        except Rejected:continue
        for ep in eps:by_episode.setdefault(ep,[]).append(c)
    reports={};ready=[]
    for ep in missing:
        rows=sorted(by_episode.get(ep,[]),key=engine.source_review.priority)
        choices=[];eligible=[]
        for c in rows:
            d=engine.source_review.decision(c,ordinary=True)
            if d['allowed']:
                try:validate_title(c['title'],candidate_mapping(mapping,c),engine.settings(),quality=policy.get('mode')!='selected')
                except Rejected as exc:d={'allowed':False,'code':'quality_required','reason':str(exc)}
            if d['allowed']:
                if c['status'] in ('discovered','retry','source_wait'):
                    record=db.get('candidate_retry_'+str(c['id']),{})
                    retry=record.get('next',0)
                    health=db.get('qbit_health',{})
                    fresh=0<=now-health.get('time',0)<90
                    uses_qbit=db.get('downloader_preference','qbit')=='qbit'
                    if uses_qbit and fresh and health.get('ok') is False:
                        d={'allowed':False,'code':'downloader_unavailable',
                           'reason':health.get('error') or 'qBittorrent本机接口不可用；任务与回执已保留'}
                        choices.append({**d,'candidate_id':c['id']});continue
                    if uses_qbit and fresh and health.get('ok') is True and qbit_retry(record):retry=0
                    if retry>now:
                        d={'allowed':False,'code':'download_retry','reason':c['reason'] or '获取种子失败，等待自动重试','retry_at':retry}
                    else:eligible.append(c)
                else:d={'allowed':False,'code':'candidate_review','reason':c['reason'] or '候选需要核验；保留原任务和回执'}
            choices.append({**d,'candidate_id':c['id']})
        if eligible:
            best={'code':'download_ready','reason':'已发现符合当前选源规则的候选，后台正在核验种子并入队','candidate_id':eligible[0]['id']}
            if not db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1',(sid,ep)):
                ready.append(eligible[0])
        elif choices:
            # A matching primary/provisional candidate's actual failure is more
            # useful than a generic refusal for an unrelated publisher.
            best=next((d for d in choices if d['code'] in ('downloader_unavailable','download_retry','candidate_review','quality_required','primary_window')),choices[0])
        else:
            best={'code':'source_not_found','reason':'尚未发现本集资源；后台继续查源'}
            errors=[s['error'] for s in sources if s['enabled'] and s['error']]
            if errors:best['source_error']=errors[0]
            if not enabled:best={'code':'sources_disabled','reason':'没有启用的订阅源，需要检查下载设置'}
        reports[str(ep)]={k:v for k,v in best.items() if k!='allowed'}
        reports[str(ep)].update(checked_at=now,candidate_count=len(rows),
            last_source_success=max((s['last_success'] or 0 for s in sources if s['enabled']),default=0))
    return reports,ready


def reconcile_missing(engine):
    """Bounded maintenance, independent of the long outbound feed scan."""
    started=time.monotonic();db=engine.db;reports={};attempts=0
    health=db.get('qbit_health',{})
    if db.get('downloader_preference','qbit')=='qbit' and health and not 0<=time.time()-health.get('time',0)<90:
        # Full reconciliation can outlast the freshness window on a large library.
        # Sample once for this batch, never once per show or from a page request.
        try:
            tasks=engine.qb.info()
            db.set('qbit_health',{'ok':True,'time':time.time(),'tasks':len(tasks),'standby':False})
        except Exception as error:
            db.set('qbit_health',{'ok':False,'time':time.time(),'error':safe_error(error)})
    for show in db.rows("SELECT id FROM shows WHERE selected=1 AND cleanup_hold=0 AND authorized=1 AND state IN ('wish','trial','watching') ORDER BY id"):
        sid=show['id']
        if not db.eligible(sid):continue
        rows,ready=inspect_missing(engine,sid)
        if ready and attempts<4 and not engine.stop_event.is_set():
            # Engine rechecks scope, reservations, policy, manifest and ownership
            # under its existing gate before any irreversible submission.
            engine.ingest(ready[0]);attempts+=1
            rows,_=inspect_missing(engine,sid)
        if rows:reports[str(sid)]=rows
    db.set('download_gaps',{'version':1,'checked_at':time.time(),'duration':round(time.monotonic()-started,3),'shows':reports})
