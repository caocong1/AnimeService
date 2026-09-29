"""Opt-out quarterly collection; downloading never implies a personal watch state."""
import datetime,time,re
from .net import safe_error
from .download_policy import submission_blocker,require_submission_ready,status as downloader_status


def install_season(app,db,engine,list_shows,prepare,save_mapping):
    def tick(today=None,force=False):
        today=today or datetime.date.today()
        start=db.get('auto_quarter_start','')
        if not start or not (db.get('auto_quarter_enabled',False) or db.get('auto_discovery_enabled',False)):return
        # Upcoming season opens three weeks early. Existing subscriptions have no quarter expiry.
        horizon=today+datetime.timedelta(days=21)
        quarter=f'{horizon.year}-{((horizon.month-1)//3)*3+1:02}'
        if quarter<start:return
        now=time.time()
        if now-db.get('auto_catalog_attempt_'+quarter,0)>86400:
            db.set('auto_catalog_attempt_'+quarter,now)
            try:engine.catalog.sync_quarter(int(quarter[:4]),int(quarter[-2:]))
            except Exception:
                db.set('auto_catalog_attempt_'+quarter,now-82800)
                db.event('season','季度目录刷新失败，1小时后重试','warning')
                return
        checked=0
        # Keep older cohorts eligible for late releases; never bulk-enrol pre-start summer shows.
        cohorts={r['quarter'] for r in db.rows('SELECT DISTINCT quarter FROM shows WHERE quarter>=? AND quarter<=?',(start,quarter))}
        cohorts.add(quarter)
        candidates={s['id']:s for q in sorted(cohorts) for s in list_shows(quarter=q,scope='catalog',q='',kind='series')
                    if s.get('quarter_kind')!='continuing'}
        for s in sorted(candidates.values(),key=lambda s:db.get('auto_retry_'+str(s['id']),0)):
            if engine.stop_event.is_set():return
            if re.search(r'特别篇|特別篇|剧场版|劇場版|\b(?:OVA|OAD)\b',s['title'],re.I):continue
            if s['state'] in ('dropped','paused','completed') or s['cleanup_hold'] or s['authorized']:continue
            if not force and now<db.get('auto_retry_'+str(s['id']),0):continue
            if checked>=(4 if force else 2):break
            checked+=1
            db.set('heartbeat',{'time':time.time(),'phase':'prepare','show_id':s['id']})
            db.set('auto_retry_'+str(s['id']),now+1800)
            try:
                result=prepare(s['id']);m=result['mapping']
                db.set('auto_preview_'+str(s['id']),{'time':time.time(),'titles':result['preview'],
                       'warning':result['warning'],'inventory_count':result['inventory_count']})
                if result['warning'] or not result['preview']:raise ValueError(result['warning'] or '尚无可核验发布')
                if result['inventory_count']:raise ValueError('已有动画匹配，需只读关联后再开启；不会自动重下')
                m.update(confirmed=True,inventory_checked=True)
                if submission_blocker(db) or not db.get('auto_quarter_enabled',False):
                    db.set('auto_error_'+str(s['id']),submission_blocker(db) or '发现可用发布；自动下载开关关闭')
                    continue
                with db.gate:
                    current=db.show(s['id'])
                    if current['state'] in ('dropped','paused','completed') or current['cleanup_hold']:continue
                    engine.check_legacy_owner(s['id'])
                    save_mapping(s['id'],m)
                    db.execute('UPDATE shows SET auto_download=1,authorized=1 WHERE id=?',(s['id'],))
                    db.set('auto_error_'+str(s['id']),'')
                    db.event('season',f"按用户季度规则准备了 {s['title']}，未改变个人追番状态")
            except ValueError as ex:db.set('auto_error_'+str(s['id']),str(ex)[:250])
            except Exception as ex:db.set('auto_error_'+str(s['id']),safe_error(ex)+'；30分钟后重试')
            if checked>=(4 if force else 2):break # Bounded batches keep source checks and completion tracking responsive.
    engine.auto_tick=tick
    @app.get('/api/season')
    def status():
        blocker=submission_blocker(db)
        if not blocker:
            try:engine.check_legacy_owner()
            except Exception as ex:blocker=safe_error(ex)
        return {'enabled':db.get('auto_quarter_enabled',False),'start':db.get('auto_quarter_start','2026-10'),
            'blocker':blocker,'downloader':downloader_status(db),'discovery_enabled':db.get('auto_discovery_enabled',False),
            'poll_seconds':db.get('poll_seconds',180),
            'scope':'日本TV/网络季度新番；不含电影、特别篇；一集一版',
            'shows':[{**{k:s[k] for k in ('id','title','state','authorized','auto_download','cleanup_hold')},
                      'error':db.get('auto_error_'+str(s['id']),''),'preview':db.get('auto_preview_'+str(s['id']))}
                     for s in db.rows('SELECT * FROM shows WHERE quarter>=? OR authorized=1 OR id IN (SELECT show_id FROM quarter_members WHERE quarter>=?)',
                                      (db.get('auto_quarter_start','2026-10'),db.get('auto_quarter_start','2026-10')))]}
    @app.post('/api/season')
    def settings(p:dict):
        if type(p.get('enabled'))!=bool:raise ValueError('开关无效')
        if p['enabled']:require_submission_ready(db)
        db.set('auto_quarter_enabled',p['enabled']);engine.wake.set()
        return {'ok':True,'message':'已更新后续季度自动收录；已有下载请在作品详情暂停'}
    return tick
