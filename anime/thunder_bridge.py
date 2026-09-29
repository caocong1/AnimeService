"""Durable UI work queue. UI success is never a download acknowledgement."""
import json,os,secrets,time
from pathlib import Path
from .thunder import ThunderState
from .releases import bdecode

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
        if len(matches)!=1:raise ValueError('迅雷中同一hash有多个任务，需要核对')
        found=matches[0]
        if t['external_id'] and int(t['external_id'])!=found['TaskId']:raise ValueError('迅雷任务ID变化，不接管其他任务')
        if not t['external_id']:
            q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(t['hash'],))
            if not q or not q['committing'] or found['CreationTime']/1000<t['created']-5:raise ValueError('发现非本项目创建的迅雷任务')
        sel=json.loads(t['selection'])
        found=self.reader.verify(found['TaskId'],t['hash'],t['staging_path'],sel['files'],sel['ids'])
        self.db.execute('UPDATE tasks SET external_id=?,acknowledged=1 WHERE hash=?',(found['TaskId'],t['hash']))
        return found
    def reconcile(self,t):
        h=t['hash'];found=self.inspect(t)
        if not found:
            if t['acknowledged']:raise ValueError('迅雷已确认任务被移除，不自动重下')
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
                if not out.is_relative_to(dest) or not src.is_relative_to(stage):raise ValueError('归库路径越界')
                out.parent.mkdir(parents=True,exist_ok=True)
                if out.exists():
                    if not os.path.samefile(src,out):raise ValueError('归库目标已有不同文件，未覆盖')
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
                self.reconcile(t)
                t=self.db.one('SELECT * FROM tasks WHERE hash=?',(q['hash'],))
                q=self.db.one('SELECT * FROM thunder_queue WHERE hash=?',(q['hash'],))
                if q['state'] in ('done','cancelled'):continue
                if q['action'] in ('submit','resume'):
                    if not self.db.eligible(t['show_id']):self.hold(t);continue
                    self.engine.check_legacy_owner(t['show_id'])
                lease=secrets.token_urlsafe(24)
                self.db.execute("UPDATE thunder_queue SET state='claimed',lease=?,expires=?,updated=? WHERE hash=?",(lease,time.time()+600,time.time(),t['hash']))
                return self.manifest(t,q,lease)
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
