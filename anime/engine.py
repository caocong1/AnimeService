import base64,datetime,json,os,re,shutil,threading,time
import psutil
from pathlib import Path
from .db import ROOT,STATES
from .net import safe_error,NetworkError
from .qbit import Qbit
from .download_policy import require_submission_ready
from .sources import Sources,Catalog
from .releases import Rejected,validate_title,bdecode,select_files,norm,episodes_of,season_of

RELIABLE_GROUPS=['喵萌奶茶屋','动漫国字幕组','LoliHouse','桜都字幕组','ANi','绿茶字幕组','悠哈璃羽字幕社']

def release_priority(candidate):
    title=norm(candidate['title'][:150])
    group=next((i for i,g in enumerate(RELIABLE_GROUPS) if norm(g) in title),len(RELIABLE_GROUPS))
    return candidate['source_id'],group,candidate['id']

class Engine:
    def __init__(self,db,qbit=None,sources=None,library=None):
        self.db=db;self.qb=qbit or Qbit();self.sources=sources or Sources(db);self.catalog=Catalog(db)
        self.library=Path(library or 'D:/MediaLibrary/Anime');self.cache=db.path.parent/'torrents';self.cache.mkdir(exist_ok=True)
        self.wake=threading.Event();self.stop_event=threading.Event();self.cycle_lock=threading.Lock()
        self.incomplete=Path('D:/MediaDownloads/Incomplete/AnimeService')
        from .thunder_bridge import ThunderBridge
        self.thunder=ThunderBridge(self)
    def settings(self):return {k:self.db.get(k) for k in ('resolution','subtitle','groups')}
    def request_check(self):
        # Persistent sequence: clicks during a cycle survive that cycle and a restart.
        with self.db.gate:
            sequence=self.db.get('check_requested',0)+1
            self.db.set('check_requested',sequence)
            self.db.set('check_requested_at',time.time())
        self.wake.set()
        return sequence
    def check_legacy_owner(self,sid=None):
        # A running legacy instance with the old catch-all RSS rule may own the same anime.
        # Do not disable or migrate it implicitly. Guard every submission, not just UI setup.
        legacy=Path(os.environ.get('APPDATA',''))/'qBittorrent/rss/download_rules.json'
        if not legacy.is_file():return
        ini=legacy.parent.parent/'qBittorrent.ini'
        if ini.is_file():
            import configparser
            config=configparser.ConfigParser(interpolation=None,strict=False)
            config.read(ini,encoding='utf-8-sig')
            if config.get('RSS',r'AutoDownloader\EnableProcessing',fallback='').lower()=='false':return
        rules=json.loads(legacy.read_text(encoding='utf-8-sig'))
        if not any(r.get('enabled') for r in rules.values()):return
        for p in psutil.process_iter(['name','cmdline']):
            if (p.info['name'] or '').lower()=='qbittorrent.exe':
                args=' '.join(p.info['cmdline'] or []).replace('\\','/').lower()
                if '--profile=d:/mediaservice/qbit-profile' not in args:
                    raise NetworkError('旧qBittorrent实例及RSS规则可能同时接管订阅，已阻止提交；请先处理旧订阅所有权')
    def state(self,sid,state):
        if state not in STATES:raise ValueError('未知追番状态')
        with self.db.gate:
            if not self.db.show(sid):raise ValueError('条目不存在')
            self.db.execute('UPDATE shows SET selected=1,state=? WHERE id=?',(state,sid))
            if state in ('watching','trial'):
                self.db.execute("UPDATE candidates SET status='discovered' WHERE show_id=? AND status IN ('covered','reserved')",(sid,))
            self.db.event('show:'+str(sid),'用户将状态设置为 '+state)
            # State is durable BEFORE stop request. Reconciliation retries even if qB is offline.
            if not self.db.eligible(sid):
                for t in self.db.rows("SELECT * FROM tasks WHERE show_id=? AND status<>'cleaned'",(sid,)):
                    try:self.hold(t)
                    except Exception as e:self.db.event('task:'+t['hash'],safe_error(e),'error')
            self.wake.set()
    def hold(self,t):
        if t.get('provider')=='thunder':return self.thunder.hold(t)
        rows=self.qb.info(t['hash'])
        if not rows and t.get('acknowledged'):
            self.db.execute("UPDATE tasks SET status='review',error='已确认任务从下载器移除，不自动重下',updated=? WHERE hash=?",(time.time(),t['hash']));return
        if rows and self.qb.owned(rows[0]):
            if rows[0].get('progress',0)<1:self.qb.stop(t['hash'])
        self.db.execute("UPDATE tasks SET status='held',updated=? WHERE hash=?",(time.time(),t['hash']))
        self.db.execute("UPDATE episodes SET status='held' WHERE hash=? AND status NOT IN ('complete','file_missing','error')",(t['hash'],))
    def inventory_matches(self,sid,mapping=None):
        p=self.db.path.parent/'inventory.json'
        if not p.exists():return []
        m=mapping or self.db.mapping(sid);aliases=m.get('aliases',[])
        return [f for f in json.loads(p.read_text(encoding='utf-8')) if any(norm(a) in norm(f['path']) for a in aliases if len(norm(a))>=2)]
    def ingest(self,candidate):
        sid=candidate['show_id'];s=self.db.show(sid)
        if not self.db.eligible(sid):return
        m=json.loads(s['mapping'])
        try:
            require_submission_ready(self.db)
            title_eps=validate_title(candidate['title'],m,self.settings())
            wanted=[ep for ep in title_eps if self.db.allowed(sid,ep) and not self.db.one('SELECT 1 FROM episodes WHERE show_id=? AND episode=?',(sid,ep))]
            if not wanted:
                self.db.execute("UPDATE candidates SET status='covered',reason='已有文件/任务或不在授权范围' WHERE id=?",(candidate['id'],));return
            data=self.sources.torrent(candidate);h,files=bdecode(data)
            expected=re.search(r'(?i)([0-9a-f]{40})\.torrent',candidate['url'])
            magnet=re.search(r'(?i)urn:btih:([A-Z2-7]{32}|[0-9a-f]{40})(?:&|$)',candidate['url'])
            expected_hash=expected[1].lower() if expected else None
            if magnet:expected_hash=base64.b32decode(magnet[1].upper()).hex() if len(magnet[1])==32 else magnet[1].lower()
            if expected_hash and expected_hash!=h:raise Rejected('种子infohash与来源标识不符')
            selected,ids=select_files(files,m,wanted,title_eps)
            with self.db.gate:
                # Network fetch happened outside the gate. Re-read current intent and reservations now.
                if not self.db.eligible(sid):return
                self.check_legacy_owner(sid)
                wanted=[ep for ep in wanted if self.db.allowed(sid,ep) and not self.db.one('SELECT 1 FROM episodes WHERE show_id=? AND episode=?',(sid,ep))]
                if not wanted:return
                prior=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
                if prior:
                    if prior.get('provider')=='thunder':raise Rejected('同一种子的已选集数已固定，新增合集选集需要核对；不会重复添加')
                    if prior['show_id']!=sid or prior['status']=='review':raise Rejected('此hash已属于其他作品或待核查任务')
                    # An explicitly expanded episode range can reuse the same batch, never add it twice.
                    old=json.loads(prior['selection']);union=sorted(set(wanted)|{int(ep) for ep in old['selected']})
                    expanded,expanded_ids=select_files(files,m,union,title_eps)
                    selection={'files':files,'ids':expanded_ids,'selected':{str(k):v for k,v in expanded.items()}}
                    with self.db.connect() as con:
                        con.execute('UPDATE tasks SET selection=? WHERE hash=?',(json.dumps(selection,ensure_ascii=False),h))
                        for ep in wanted:
                            f=expanded[ep];con.execute('INSERT INTO episodes(show_id,episode,hash,file_index,status,size) VALUES(?,?,?,?,?,?)',(sid,ep,h,f['index'],'pending',f['size']))
                        con.execute("UPDATE candidates SET status='reserved',hash=?,reason='扩展已有合集中的缺集' WHERE id=?",(h,candidate['id']))
                    self.dispatch(self.db.one('SELECT * FROM tasks WHERE hash=?',(h,)));return
                thunder=self.db.get('downloader_preference','qbit')=='thunder_first'
                existing=self.thunder.reader.by_hash(h) if thunder else self.qb.info(h)
                if existing:
                    raise Rejected('下载器已有此hash；不接管其他任务，请关联已有文件')
                selected,ids=select_files(files,m,wanted,title_eps)
                if shutil.disk_usage(self.library.anchor or self.library.parent).free<sum(f['size'] for f in selected.values())+self.db.get('minimum_free_gb',10)*1024**3:
                    raise NetworkError('磁盘可用空间不足，保留至少10GiB')
                safe_title=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',s['title']).strip('. ')[:80] or str(sid)
                save=self.library/(safe_title+f' [bgm-{sid}]')/f'Season {m.get("season",1):02}'
                self.library.mkdir(parents=True,exist_ok=True);save.mkdir(parents=True,exist_ok=True)
                (self.cache/(h+'.torrent')).write_bytes(data)
                selection={'files':files,'ids':ids,'selected':{str(k):v for k,v in selected.items()}}
                with self.db.connect() as c:
                    c.execute('INSERT INTO tasks(hash,show_id,title,status,selection,save_path,error,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',(h,sid,candidate['title'],'intent',json.dumps(selection,ensure_ascii=False),str(save),'',time.time(),time.time()))
                    if thunder:
                        stage=self.incomplete/h;stage.mkdir(parents=True,exist_ok=True)
                        c.execute("UPDATE tasks SET provider='thunder',staging_path=? WHERE hash=?",(str(stage),h))
                    for ep,f in selected.items():c.execute('INSERT INTO episodes(show_id,episode,hash,file_index,status,size) VALUES(?,?,?,?,?,?)',(sid,ep,h,f['index'],'pending',f['size']))
                    c.execute("UPDATE candidates SET status='reserved',hash=?,reason='' WHERE id=?",(h,candidate['id']))
                self.dispatch(self.db.one('SELECT * FROM tasks WHERE hash=?',(h,)))
        except Rejected as e:
            self.db.execute("UPDATE candidates SET status='rejected',reason=? WHERE id=?",(str(e),candidate['id']))
        except ValueError as e:
            self.db.execute("UPDATE candidates SET status='review',reason=? WHERE id=?",(str(e)[:300],candidate['id']))
        except Exception as e:
            self.db.execute("UPDATE candidates SET status='retry',reason=? WHERE id=?",(safe_error(e),candidate['id']))
            self.db.event('candidate:'+str(candidate['id']),safe_error(e),'error')
    def dispatch(self,t):
        h=t['hash'];sid=t['show_id'];selection=json.loads(t['selection'])
        with self.db.gate:
            if not self.db.eligible(sid):self.hold(t);return
            require_submission_ready(self.db)
            self.check_legacy_owner(sid)
            if t.get('provider')=='thunder':
                self.thunder.reconcile(t);return
            if self.db.get('downloader_preference','qbit')=='thunder_first':raise Rejected('迅雷优先，未核准的旧qB任务不会自动启动')
            desired={int(ep):f for ep,f in selection['selected'].items() if self.db.allowed(sid,int(ep))}
            if not desired:self.hold(t);return
            rows=self.qb.info(h)
            if not rows:
                if t.get('acknowledged') or t['status'] not in ('intent','uncertain','held'):
                    raise Rejected('下载器任务已被移除，保留台账，禁止自动重下')
                self.db.execute("UPDATE tasks SET status='uncertain',updated=? WHERE hash=?",(time.time(),h))
                self.qb.add(h,(self.cache/(h+'.torrent')).read_bytes(),t['save_path'])
                rows=self.qb.info(h)
                if not rows:raise NetworkError('提交后尚未确认入队，下一轮先按hash查询')
            qbtask=rows[0]
            if not self.qb.owned(qbtask):raise Rejected('任务分类/标签不匹配，禁止修改')
            self.db.execute('UPDATE tasks SET acknowledged=1 WHERE hash=?',(h,))
            if Path(qbtask['save_path']).resolve()!=Path(t['save_path']).resolve():raise Rejected('下载目录发生变化，禁止自动操作')
            files=self.qb.files(h)
            expected={f['name'].replace('\\','/'):f for f in selection['files']}
            if len(files)!=len(expected) or any(f['name'].replace('\\','/') not in expected or f['size']!=expected[f['name'].replace('\\','/')]['size'] for f in files):
                self.qb.stop(h);raise Rejected('下载器实际文件与种子清单不一致')
            indices={f['name'].replace('\\','/'):f['index'] for f in files}
            wanted_ids=[]
            for f in selection['files']:
                if f['index'] in selection['ids']:
                    try:eps=episodes_of(Path(f['name']).name,self.db.mapping(sid).get('offset',0))
                    except Rejected:eps=list(desired) if len(selection['selected'])==1 else []
                    if any(e in desired for e in eps):wanted_ids.append(indices[f['name']])
            if not wanted_ids:raise Rejected('无可安全选择文件')
            # Always stop before reprioritizing. Read-only status refresh never uses this branch.
            self.qb.stop(h);self.qb.priorities(h,[f['index'] for f in files],0);self.qb.priorities(h,wanted_ids,1)
            actual=self.qb.files(h)
            if {f['index'] for f in actual if f['priority']>0}!=set(wanted_ids):raise Rejected('文件选择校验失败，保持停止')
            if not self.db.eligible(sid):self.hold(t);return
            self.qb.start(h)
            with self.db.connect() as c:
                c.execute("UPDATE tasks SET status='downloading',error='',updated=? WHERE hash=?",(time.time(),h))
                for ep,f in desired.items():c.execute('UPDATE episodes SET file_index=?,status=? WHERE show_id=? AND episode=?',(indices[f['name']],'downloading',sid,ep))
            self.db.event('task:'+h,'已核验文件选择并确认启动：'+','.join(map(str,desired)))
    def reconcile(self):
        tasks=self.db.rows("SELECT * FROM tasks WHERE status<>'cleaned' ORDER BY created")
        all_tasks=self.qb.info() if any(t.get('provider')!='thunder' for t in tasks) or self.db.get('downloader_preference','qbit')=='qbit' else []
        self.db.set('qbit_health',{'ok':True,'time':time.time(),'tasks':len(all_tasks),'standby':self.db.get('downloader_preference')=='thunder_first'})
        byhash={t['hash']:t for t in all_tasks}
        for t in tasks:
            h=t['hash'];sid=t['show_id']
            try:
                with self.db.gate:
                    if t.get('provider')=='thunder':
                        if t['status']!='review':self.thunder.reconcile(t)
                        continue
                    if not self.db.eligible(sid):self.hold(t);continue
                    if t['status'] in ('intent','uncertain','held'):
                        self.dispatch(t);continue
                    qt=byhash.get(h)
                    if not qt:raise Rejected('下载器中未找到已确认任务；不会自动重下')
                    if not self.qb.owned(qt):raise Rejected('项目任务所有权不匹配')
                    if Path(qt['save_path']).resolve()!=Path(t['save_path']).resolve():raise Rejected('任务保存目录与台账不符')
                    if t['status']=='review':continue
                    eps=self.db.rows('SELECT * FROM episodes WHERE hash=?',(h,));files=self.qb.files(h);idx={f['index']:f for f in files}
                    # Apply the current explicit episode bounds to already queued files too.
                    if any(not self.db.allowed(sid,e['episode']) and idx.get(e['file_index'],{}).get('priority',0)>0 for e in eps):
                        self.dispatch(t);continue
                    completed=True
                    for e in eps:
                        f=idx.get(e['file_index'])
                        if not f:raise Rejected('任务文件索引丢失')
                        p=Path(t['save_path'])/f['name'];root=Path(t['save_path']).resolve()
                        if not p.resolve().is_relative_to(root):raise Rejected('实际文件越界')
                        done=f.get('progress',0)>=1 and p.is_file() and p.stat().st_size==f['size']
                        status='complete' if done else ('held' if not self.db.allowed(sid,e['episode']) else 'downloading')
                        if f.get('progress',0)>=1 and not done:status='file_missing'
                        if qt.get('state') in ('error','missingFiles'):status='error'
                        completed=completed and done
                        self.db.execute('UPDATE episodes SET status=?,progress=?,path=? WHERE show_id=? AND episode=?',
                            (status,f.get('progress',0),str(p) if done else '',sid,e['episode']))
                    self.db.execute("UPDATE episodes SET first_completed=coalesce(first_completed,?) WHERE hash=? AND status='complete'",(time.time(),h))
                    self.db.execute('UPDATE tasks SET status=?,updated=?,error=? WHERE hash=?',('complete' if completed else 'downloading',time.time(),'' if qt.get('state') not in ('error','missingFiles') else qt['state'],h))
            except Exception as e:
                message=str(e) if isinstance(e,Rejected) else safe_error(e)
                if isinstance(e,Rejected):self.db.execute("UPDATE tasks SET status='review',error=?,updated=? WHERE hash=?",(message,time.time(),h))
                else:self.db.execute('UPDATE tasks SET error=?,updated=? WHERE hash=?',(message,time.time(),h))
                self.db.event('task:'+h,message,'error')
    def cycle(self,force=False):
        if not self.cycle_lock.acquire(blocking=False):return
        try:
            requested=self.db.get('check_requested',0)
            force=force or requested>self.db.get('check_completed',0)
            self.db.set('check_started_at',time.time())
            self.db.set('heartbeat',{'time':time.time(),'phase':'checking'})
            if self.db.get('downloader_preference','qbit')=='qbit' and self.db.one("SELECT 1 FROM shows WHERE authorized=1 AND cleanup_hold=0 AND (state IN ('trial','watching') OR (auto_download=1 AND state='wish'))") and hasattr(self.qb,'ensure_running'):
                try:require_submission_ready(self.db);self.check_legacy_owner();self.qb.ensure_running()
                except Exception as e:self.db.event('downloader',safe_error(e),'error')
            try:self.reconcile()
            except Exception as e:self.db.set('qbit_health',{'ok':False,'time':time.time(),'error':safe_error(e)})
            for source in self.db.rows("SELECT sources.* FROM sources JOIN shows ON shows.id=sources.show_id WHERE sources.enabled=1 AND shows.authorized=1 AND shows.cleanup_hold=0 AND (shows.state IN ('trial','watching') OR (shows.auto_download=1 AND shows.state='wish'))"):
                if not self.db.eligible(source['show_id']):continue
                self.db.set('heartbeat',{'time':time.time(),'phase':'source','source':source['id']})
                if force or source['next_attempt']<=time.time():
                    ok=self.sources.rss(source)
                    if ok and (force or source['failures'] or not source['archive_success'] or time.time()-source['archive_success']>21600):self.sources.archive(source)
                    if ok:
                        candidates=self.db.rows("SELECT * FROM candidates WHERE show_id=? AND status IN ('discovered','retry') ORDER BY source_id,id DESC",(source['show_id'],))
                        candidates.sort(key=release_priority)
                        for c in candidates[:300]:
                            if self.stop_event.is_set():break
                            self.ingest(c)
                if not self.db.get('details_'+str(source['show_id'])) or time.time()-self.db.get('details_'+str(source['show_id']),0)>21600:
                    try:self.catalog.details(source['show_id'])
                    except Exception as e:self.db.event('airings',safe_error(e),'warning')
            if hasattr(self,'auto_tick'):self.auto_tick(force=force)
            self.db.set('heartbeat',{'time':time.time(),'phase':'idle'})
            self.db.set('check_completed',requested)
            self.db.set('check_finished_at',time.time())
        finally:self.cycle_lock.release()
    def run(self):
        self.db.event('service','后台启动，立即查询下载器与订阅并补查历史')
        force=True
        while not self.stop_event.is_set():
            try:self.cycle(force)
            except Exception as e:self.db.event('service',safe_error(e),'error')
            force=False;self.wake.wait(30);self.wake.clear()
    def gaps(self,sid):
        s=self.db.show(sid);m=json.loads(s['mapping']);today=datetime.date.today().isoformat()
        air=self.db.rows("SELECT episode FROM airings WHERE show_id=? AND airdate<>'' AND airdate<=?",(sid,today))
        airing_offset=m.get('airing_offset',0)
        observed=[]
        limit=m.get('end',s['total'] or 999)
        for r in self.db.rows('SELECT title FROM candidates WHERE show_id=?',(sid,)):
            try:observed.extend(ep for ep in validate_title(r['title'],m,self.settings(),False) if m.get('start',1)<=ep<=limit)
            except Rejected:pass
        expected=set(e['episode']-airing_offset for e in air)|set(range(m.get('start',1),min(max(observed,default=0),m.get('end',s['total'] or 999))+1))
        existing=self.db.rows('SELECT episode,status FROM episodes WHERE show_id=?',(sid,));reserved={e['episode'] for e in existing}
        limit=m.get('end',s['total'] or 999)
        return {'missing':sorted(e for e in expected-reserved if m.get('start',1)<=e<=limit),
            'unverified_files':[e['episode'] for e in existing if e['status'] in ('file_missing','error')],
            'basis':'Bangumi已播出日期及已发现发布集号；未来/未知日期不推算', 'active':self.db.eligible(sid)}
