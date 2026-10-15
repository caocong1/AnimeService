"""Audited handoff of safe unsubmitted personal work when the UI worker fails."""
import json,re,time,shutil
from pathlib import Path
from .fidelity import fidelity_blocker
from .releases import bdecode,validate_title,select_files
from .net import safe_error
from .research_tasks import is_sample,sample_allowed

UI_FAILURES=('computer use','computer-use','active request','screenshotid','helper','工具故障','工具不可用','枚举','激活超时')

def record_ui_failure(db,error):
    if not db.get('qbit_fallback_enabled',False):return
    if any(word in error.lower() for word in UI_FAILURES):
        db.set('thunder_ui_fault',{'active':True,'reason':'Computer Use 执行故障，使用已授权qB备用',
                                  'reported_at':time.time()})

def recover_downloads(engine,limit=4):
    db=engine.db;fault=db.get('thunder_ui_fault',{})
    qbit_first=db.get('downloader_preference')=='qbit'
    if not qbit_first:
        if not db.get('qbit_fallback_enabled',False) or not fault.get('active'):return
        if db.get('downloader_preference')!='thunder_first':return
    if time.time()<db.get('qbit_fallback_retry_at',0):return
    tasks=db.rows("""SELECT t.* FROM tasks t JOIN shows s ON s.id=t.show_id
        JOIN thunder_queue q ON q.hash=t.hash
        WHERE t.provider='thunder' AND t.status IN ('intent','held') AND t.acknowledged=0
        AND t.external_id IS NULL AND q.action='submit' AND q.state IN ('pending','cancelled')
        AND q.committing=0 AND q.lease='' AND s.state IN ('wish','trial','watching')
        AND s.cleanup_hold=0 ORDER BY
        CASE WHEN EXISTS(SELECT 1 FROM episodes e WHERE e.show_id=t.show_id AND e.status='complete') THEN 1 ELSE 0 END,
        CASE t.purpose WHEN 'episode' THEN 0 ELSE 1 END,t.created""")
    # Give each not-yet-playable series its first task before extra research versions.
    tasks.sort(key=lambda t:(bool(db.one("SELECT 1 FROM episodes WHERE show_id=? AND status='complete'",(t['show_id'],))),
                              t['purpose']!='episode',t['created']))
    seen=set();ordered=[]
    for t in tasks:
        if t['show_id'] not in seen:ordered.append(t);seen.add(t['show_id'])
    ordered.extend(t for t in tasks if t not in ordered)
    processed=0
    for t in ordered:
        if processed>=limit:break
        try:
            with db.gate:
                if db.one("SELECT 1 FROM thunder_queue WHERE lease<>'' AND expires>?",(time.time(),)):return
                current=db.one('SELECT * FROM tasks WHERE hash=?',(t['hash'],))
                q=db.one('SELECT * FROM thunder_queue WHERE hash=?',(t['hash'],))
                if current!=t or not q or q['committing'] or q['lease']:continue
                sid=t['show_id'];m=db.mapping(sid)
                if not db.eligible(sid):continue
                if (not qbit_first or is_sample(t)) and not db.review_wanted(sid):continue
                if not sample_allowed(db,t):continue
                if db.get('thunder_dialog_hold_'+t['hash'],{}).get('unconfirmed'):continue
                if fidelity_blocker(db,t['title'],t['selection']):continue
                selection=json.loads(t['selection']);eps=[int(x) for x in selection['selected']]
                if not eps or any(not db.allowed(sid,ep) for ep in eps):continue
                if t['purpose']=='source_sample' and eps!=[1]:continue
                raw=(engine.cache/(t['hash']+'.torrent')).read_bytes();h,files=bdecode(raw)
                if h!=t['hash'] or files!=selection['files']:raise ValueError('备用交接种子清单不一致')
                title_eps=validate_title(t['title'],m,{},False)
                chosen,ids=select_files(files,m,eps,title_eps)
                if ids!=selection['ids'] or {str(k):v for k,v in chosen.items()}!=selection['selected']:
                    raise ValueError('备用交接季集或文件选择已变化')
                engine.check_legacy_owner(sid)
                # Read errors are not proof of absence. Existing/uncertain tasks never hand off.
                if engine.thunder.reader.by_hash(h):continue
                if engine.qb.info(h):continue
                engine.qb.check_storage()
                save=t['save_path']
                if is_sample(t):
                    title=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',db.show(sid)['title']).strip('. ')[:80]
                    label='Subtitle samples' if t['purpose']=='subtitle_sample' else 'Source samples'
                    save=str(engine.library/(title+f' [bgm-{sid}]')/f'Season {m.get("season",1):02}'/label/h)
                if shutil.disk_usage(engine.library.anchor or engine.library.parent).free<sum(f['size'] for f in chosen.values())+db.get('minimum_free_gb',10)*1024**3:
                    raise ValueError('备用下载磁盘空间不足，稍后自动重试')
                Path(save).mkdir(parents=True,exist_ok=True)
                reason='用户改为qB优先；核验原迅雷意图未提交' if qbit_first else fault['reason']
                receipt={'hash':h,'show_id':sid,'reason':reason,'thunder_absent':True,
                         'created_at':time.time(),'previous_provider':'thunder'}
                with db.connect() as con:
                    con.execute('BEGIN IMMEDIATE')
                    if con.execute("SELECT 1 FROM thunder_queue WHERE lease<>'' AND expires>?",(time.time(),)).fetchone():return
                    row=con.execute('SELECT status,acknowledged,external_id,provider FROM tasks WHERE hash=?',(h,)).fetchone()
                    queue=con.execute('SELECT committing,lease,state FROM thunder_queue WHERE hash=?',(h,)).fetchone()
                    if tuple(row)!=(t['status'],0,None,'thunder') or queue['committing'] or queue['lease']:continue
                    con.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',('qbit_fallback_'+h,json.dumps(receipt)))
                    con.execute("UPDATE tasks SET provider='qbit',status='intent',save_path=?,error='',updated=? WHERE hash=?",(save,time.time(),h))
                    con.execute("UPDATE thunder_queue SET state='cancelled',error=?,updated=? WHERE hash=?",('已安全交接qB；不再执行迅雷提交',time.time(),h))
                db.event('task:'+h,reason+'；保留原用途、文件选择及授权')
                processed+=1
                engine.dispatch(db.one('SELECT * FROM tasks WHERE hash=?',(h,)))
        except ValueError as error:
            # A bad manifest is local to this work item, not a downloader outage.
            # Keep its provider/intent and inspect the next safe item this round.
            db.set('qbit_handoff_check_'+t['hash'],{'time':time.time(),'error':str(error)[:300]})
            continue
        except Exception as error:
            message=str(error)[:300] if isinstance(error,ValueError) else safe_error(error)
            db.set('qbit_fallback_health',{'ok':False,'time':time.time(),'error':message})
            db.set('qbit_fallback_retry_at',time.time()+60)
            db.event('recovery',message,'warning');return
    db.set('qbit_fallback_health',{'ok':True,'time':time.time()})
