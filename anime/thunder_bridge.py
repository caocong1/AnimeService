"""Durable UI work queue. UI success is never a download acknowledgement."""
import json,os,secrets,time
from pathlib import Path
from .thunder import ThunderState,ThunderReviewRequired
from .releases import bdecode,validate_title

class ThunderBridge:
    def __init__(self,engine,reader=None):
        self.engine=engine;self.db=engine.db;self.reader=reader or ThunderState()
    def enqueue(self,t,action='submit'):
        old=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(t['hash'],))
        if old and old['action']==action and old['state'] in ('pending','claimed','review'):return
        self.db.execute('''INSERT INTO thunder_queue(hash,action,state,updated) VALUES(?,?,'pending',?)
            ON CONFLICT(hash) DO UPDATE SET action=excluded.action,state='pending',lease='',expires=0,
            committing=0,error='',updated=excluded.updated''',(t['hash'],action,time.time()))
    def hold(self,t):
        if t['status']=='linked':return
        self.db.execute("UPDATE tasks SET status='held' WHERE hash=?",(t['hash'],))
        self.db.execute("UPDATE episodes SET status='held' WHERE hash=? AND status<>'complete'",(t['hash'],))
        if t['external_id']:self.enqueue(t,'pause')
        else:
            # A commit can already be in flight. Keep its record for reconciliation.
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(t['hash'],))
            if q and q['committing']:
                self.db.execute("UPDATE thunder_queue SET action='pause',state='pending',lease='' WHERE hash=?",(t['hash'],))
            else:self.db.execute("UPDATE thunder_queue SET state='cancelled',lease='' WHERE hash=?",(t['hash'],))
    def inspect(self,t):
        matches=self.reader.by_hash(t['hash'])
        if not matches:return None
        if len(matches)!=1:raise ThunderReviewRequired('迅雷中同一hash有多个任务，需要核对')
        found=matches[0]
        if t['external_id'] and int(t['external_id'])!=found['TaskId']:raise ThunderReviewRequired('迅雷任务ID变化，不接管其他任务')
        if not t['external_id']:
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(t['hash'],))
            if not q or not q['committing'] or found['CreationTime']/1000<t['created']-5:raise ThunderReviewRequired('迅雷已有同hash任务，但缺少本项目提交凭据；禁止接管或重复下载，请核查')
        sel=json.loads(t['selection'])
        found=self.reader.verify(found['TaskId'],t['hash'],t['staging_path'],sel['files'],sel['ids'])
        self.db.execute('UPDATE tasks SET external_id=?,acknowledged=1 WHERE hash=?',(found['TaskId'],t['hash']))
        return found
    def reconcile(self,t):
        if t['status'] in ('review','cleaned','linked'):return
        try:self._reconcile(t)
        except ThunderReviewRequired as e:self.mark_review(t,str(e))
    def mark_review(self,t,message):
        with self.db.gate:
            current=self.db.one('SELECT status,error FROM tasks WHERE hash=?',(t['hash'],))
            self.db.execute("UPDATE tasks SET status='review',error=?,updated=? WHERE hash=?",(message,time.time(),t['hash']))
            # Retain committing as evidence of an uncertain click. No new UI
            # lease is issued until this particular task is explicitly reviewed.
            self.db.execute("UPDATE thunder_queue SET state='review',error=?,lease='',expires=0,updated=? WHERE hash=?",
                (message,time.time(),t['hash']))
            if current['status']!='review' or current['error']!=message:
                self.db.event('task:'+t['hash'],message,'warning')
    def link_reviewed_completed_file(self,h,task_id):
        """Explicit read-only association after human review, never task adoption."""
        with self.db.gate:
            t=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(h,))
            if not t or t['provider']!='thunder' or t['status']!='review' or t['acknowledged'] or t['external_id']:
                raise ValueError('只允许核查未接管的迅雷待核查任务')
            if not q or q['state']!='review' or q['committing'] or q['lease']:
                raise ValueError('仍有执行租约或不确定提交，禁止关联')
            if self.db.show(t['show_id'])['cleanup_hold']:raise ValueError('已清理作品禁止补回')
            selection=json.loads(t['selection'])
            cached_hash,files=bdecode((self.engine.cache/(h+'.torrent')).read_bytes())
            if cached_hash!=h or files!=selection['files']:raise ValueError('缓存种子与待核查任务不符')
            if len(files)!=1 or selection['ids']!=[0] or len(selection['selected'])!=1:
                raise ValueError('只读关联只支持已核验的单集单文件')
            episode=int(next(iter(selection['selected'])))
            if validate_title(files[0]['name'],self.db.mapping(t['show_id']),{},False)!=[episode]:
                raise ValueError('文件作品或集数与当前映射不符')
            matches=self.reader.by_hash(h)
            if len(matches)!=1 or matches[0]['TaskId']!=int(task_id):raise ValueError('迅雷任务不唯一或ID变化')
            found=self.reader.verify(task_id,h,matches[0]['SavePath'],files,selection['ids'])
            if not found['verified_complete']:raise ValueError('尚未核验下载完成')
            root=Path(found['SavePath']).resolve();path=(root/found['files'][0]['name']).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size!=files[0]['size']:
                raise ValueError('完成文件已变化，不能关联')
            current=self.db.one('SELECT * FROM episodes WHERE show_id=? AND episode=?',(t['show_id'],episode))
            if not current or current['hash']!=h or current['path'] or current['status']=='complete':
                raise ValueError('分集已有其他记录，禁止覆盖')
            evidence={'task_id':int(task_id),'hash':h,'path':str(path),'size':files[0]['size'],
                'completed':found.get('CompletionTime',0)/1000,'reviewed_at':time.time(),'mode':'read_only'}
            with self.db.connect() as c:
                # NULL hash deliberately excludes this outside task's file from
                # project downloader control and every project cleanup preview.
                c.execute("UPDATE episodes SET hash=NULL,path=?,status='complete',progress=1,first_completed=NULL WHERE show_id=? AND episode=?",
                    (str(path),t['show_id'],episode))
                c.execute("UPDATE tasks SET status='linked',error='',updated=? WHERE hash=?",(time.time(),h))
                c.execute("UPDATE thunder_queue SET state='done',error='',lease='',expires=0,updated=? WHERE hash=?",(time.time(),h))
                c.execute('INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)',('thunder_readonly_'+h,json.dumps(evidence,ensure_ascii=False)))
            self.db.event('task:'+h,'已核查完整单集文件并原位只读关联；未接管迅雷任务，不重下、不参与项目清理')
            return evidence
    def _reconcile(self,t):
        h=t['hash'];found=self.inspect(t)
        if not found:
            if t['acknowledged']:raise ThunderReviewRequired('迅雷已确认任务被移除，不自动重下')
            if not self.db.eligible(t['show_id']):self.hold(t);return
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(h,))
            if q and q['committing']:
                self.db.execute("UPDATE tasks SET error='迅雷提交结果尚未确认，等待界面核对；不会重复添加' WHERE hash=?",(h,));return
            self.enqueue(t);return
        if not self.db.eligible(t['show_id']) and found['Status'] not in (7,8):
            self.enqueue({**t,'external_id':found['TaskId']},'pause');return
        if self.db.eligible(t['show_id']) and found['Status']==7:self.enqueue(t,'resume')
        if found['verified_complete']:
            # NTFS hard links publish completed files without moving the files
            # referenced by Thunder. Both aliases belong to this task and cleanup.
            stage=Path(t['staging_path']).resolve();dest=Path(t['save_path']).resolve()
            for f in found['files']:
                if not f['selected']:continue
                src=(stage/f['name']).resolve();out=(dest/f['name']).resolve()
                if not out.is_relative_to(dest) or not src.is_relative_to(stage):raise ThunderReviewRequired('归库路径越界')
                out.parent.mkdir(parents=True,exist_ok=True)
                if out.exists():
                    if not os.path.samefile(src,out):raise ThunderReviewRequired('归库目标已有不同文件，未覆盖')
                else:os.link(src,out)
            selection=json.loads(t['selection'])
            for ep,f in selection['selected'].items():
                self.db.execute("UPDATE episodes SET status='complete',progress=1,path=?,first_completed=coalesce(first_completed,?) WHERE hash=? AND episode=?",
                    (str(dest/f['name']),time.time(),h,int(ep)))
            state='complete'
        else:
            state='held' if found['Status']==7 else 'downloading'
            for f in found['files']:
                self.db.execute('UPDATE episodes SET status=?,progress=? WHERE hash=? AND file_index=?',
                    (state,min(1,f['received']/max(1,f['size'])),h,f['index']))
        self.db.execute('UPDATE tasks SET status=?,error=?,updated=? WHERE hash=?',
            (state,('迅雷错误码 '+str(found['FailureErrorCode'])) if found['FailureErrorCode'] else '',time.time(),h))
        q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(h,))
        if q and (q['action']=='submit' or q['action']=='pause' and found['Status'] in (7,8) or q['action']=='resume' and found['Status'] in (5,8)):
            self.db.execute("UPDATE thunder_queue SET state='done',lease='' WHERE hash=?",(h,))
    def claim(self):
        with self.db.gate:
            self.db.set('thunder_ui_last_seen',time.time())
            if self.db.one("SELECT 1 FROM thunder_queue WHERE state='claimed' AND expires>?",(time.time(),)):return None
            for q in self.db.rows("SELECT * FROM thunder_queue WHERE state IN ('pending','claimed','review') ORDER BY CASE action WHEN 'pause' THEN 0 ELSE 1 END,CASE state WHEN 'claimed' THEN 0 ELSE 1 END,updated"):
                t=self.db.one('SELECT * FROM tasks WHERE hash=?',(q['hash'],))
                if q['state']=='claimed' and q['expires']>time.time():continue
                if t['status'] in ('review','linked'):continue
                self.reconcile(t)
                t=self.db.one('SELECT * FROM tasks WHERE hash=?',(q['hash'],))
                q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(q['hash'],))
                if q['state'] in ('done','cancelled') or t['status']=='review':continue
                if q['action'] in ('submit','resume'):
                    if not self.db.eligible(t['show_id']):self.hold(t);continue
                    self.engine.check_legacy_owner(t['show_id'])
                lease=secrets.token_urlsafe(24)
                manifest=self.manifest(t,q,lease)
                self.db.execute("UPDATE thunder_queue SET state='claimed',lease=?,expires=?,updated=? WHERE hash=?",(lease,time.time()+600,time.time(),t['hash']))
                return manifest
        return None
    def manifest(self,t,q,lease):
        sel=json.loads(t['selection']);torrent=self.engine.cache/(t['hash']+'.torrent')
        h,files=bdecode(torrent.read_bytes())
        if h!=t['hash'] or files!=sel['files']:raise ValueError('缓存种子与任务不符')
        return {'hash':h,'show_id':t['show_id'],'title':t['title'],'action':q['action'],
            'external_id':t['external_id'],'torrent_path':str(torrent),'save_path':t['staging_path'],
            'files':files,'selected_ids':sel['ids'],'lease':lease,'uncertain':bool(q['committing'])}
    def preflight(self,h,lease):
        with self.db.gate:
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(h,));t=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            if not q or q['state']!='claimed' or q['lease']!=lease or q['expires']<time.time():raise ValueError('执行租约已失效')
            if q['action']=='submit':
                if not self.db.eligible(t['show_id']):self.hold(t);raise ValueError('已暂停或弃番，取消提交')
                self.engine.check_legacy_owner(t['show_id'])
                if self.reader.by_hash(h):raise ValueError('迅雷已存在此hash，先核对回执')
                if q['committing']:raise ValueError('前次提交结果不确定，禁止再次点击下载')
            elif q['action']=='resume' and not self.db.eligible(t['show_id']):raise ValueError('已暂停或弃番，禁止恢复下载')
            if q['action'] in ('pause','resume'):
                if not self.inspect(t):raise ValueError('迅雷任务不存在，禁止操作其他任务')
            self.db.execute('UPDATE thunder_queue SET committing=1 WHERE hash=?',(h,))
            return {'ok':True,'action':q['action']}
    def report(self,h,lease,error=''):
        with self.db.gate:
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(h,))
            if not q or q['lease']!=lease:raise ValueError('执行租约不匹配')
            t=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            if error:
                # Before the final click, releasing is safe. Afterwards retain
                # uncertainty and require hash-based recovery instead of re-add.
                self.db.execute("UPDATE thunder_queue SET state=?,error=?,lease='',expires=0 WHERE hash=?",
                    ('review' if q['committing'] else 'pending',str(error)[:300],h))
            self.reconcile(t)
            return {'ok':True,'status':self.db.one('SELECT status,error FROM tasks WHERE hash=?',(h,))}

def register_thunder(app,engine):
    bridge=engine.thunder
    @app.get('/api/thunder')
    def status():
        return {'enabled':engine.db.get('thunder_ui_enabled',False),'last_seen':engine.db.get('thunder_ui_last_seen'),
            'jobs':engine.db.rows('''SELECT q.hash,q.action,q.state,q.error,t.title,t.status,t.external_id
                FROM thunder_queue q JOIN tasks t ON t.hash=q.hash ORDER BY q.updated DESC LIMIT 100'''),
            'note':'后台每3分钟查源；界面执行每15分钟，需桌面解锁。打开页面不会标记已看。'}
    @app.post('/api/thunder/claim')
    def claim():return {'job':bridge.claim()}
    @app.post('/api/thunder/preflight')
    def preflight(p:dict):return bridge.preflight(p['hash'],p['lease'])
    @app.post('/api/thunder/report')
    def report(p:dict):return bridge.report(p['hash'],p['lease'],p.get('error',''))
