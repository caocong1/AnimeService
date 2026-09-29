"""Explicit, manifest-confirmed cleanup of completed project files only."""
import hashlib,json,time,os
from pathlib import Path


class Cleanup:
    def __init__(self,db,engine):self.db=db;self.engine=engine
    def preview(self,sid,now=None):
        now=now or time.time();s=self.db.show(sid)
        if not s:raise ValueError('作品不存在')
        episodes=self.db.rows("SELECT * FROM episodes WHERE show_id=? AND hash IS NOT NULL AND status='complete'",(sid,))
        if not episodes:return None
        first=min((e['first_completed'] for e in episodes if e['first_completed']),default=None)
        played=[]
        for e in episodes:
            key=hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32]
            w=self.db.one('SELECT last_played FROM web_progress WHERE media_id=?',(key,))
            if w and w['last_played']:played.append(w['last_played'])
        # Manual watched updates also protect files used through the desktop fallback.
        manual=self.db.one('SELECT max(updated) t FROM watches WHERE show_id=?',(sid,))
        if manual and manual['t']:played.append(manual['t'])
        activity=max(played) if played else first
        if s['state']!='dropped' and (not activity or now-activity<180*86400):return None
        files=[];hashes=set();root=self.engine.library.resolve()
        for e in episodes:
            p=Path(e['path']);resolved=p.resolve()
            task=self.db.one('SELECT * FROM tasks WHERE hash=? AND show_id=?',(e['hash'],sid))
            if not task or not resolved.is_relative_to(root) or resolved==root:raise ValueError('文件不在本项目动画目录或无任务所有权')
            # Reject junctions/symlinks and aliases, not merely lexical traversal.
            if p.absolute()!=resolved or any(x.is_symlink() or (hasattr(x,'is_junction') and x.is_junction()) for x in [p,*p.parents] if x!=root.parent):raise ValueError('链接路径不能自动清理')
            if not p.is_file() or p.stat().st_size!=e['size']:raise ValueError('文件已变化，请重新核查')
            st=p.stat();files.append({'path':str(resolved),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'episode':e['episode']});hashes.add(e['hash'])
        core={'id':sid,'state':s['state'],'activity':activity,'files':files,'hashes':sorted(hashes)}
        for h in sorted(hashes):
            task=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            if task.get('provider')!='thunder':continue
            observed=self.engine.thunder.inspect(task)
            if not observed or not observed['verified_complete']:raise ValueError('迅雷尚未核验全部选中文件完成')
            stage=Path(task['staging_path']).resolve();dest=Path(task['save_path']).resolve()
            if not stage.is_relative_to(self.engine.incomplete.resolve()):raise ValueError('迅雷暂存目录不属于项目')
            for f in observed['files']:
                if not f['selected']:continue
                source=(stage/f['name']).resolve();target=(dest/f['name']).resolve()
                if not source.is_relative_to(stage) or not target.is_relative_to(root) or not os.path.samefile(source,target):raise ValueError('迅雷归库链接发生变化')
                for p in (target,source):
                    if any(x['path']==str(p) for x in files):continue
                    st=p.stat();files.append({'path':str(p),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'episode':0,'hardlink':True})
        fingerprint=hashlib.sha256(json.dumps(core,sort_keys=True).encode()).hexdigest()
        unique_sizes={(Path(f['path']).stat().st_dev,Path(f['path']).stat().st_ino):f['size'] for f in files}
        return {**core,'title':s['title'],'reason':'已弃番' if s['state']=='dropped' else '180天未播放','bytes':sum(unique_sizes.values()),'fingerprint':fingerprint}
    def confirm(self,sid,fingerprint):
        with self.db.gate:
            plan=self.preview(sid)
            if not plan or plan['fingerprint']!=fingerprint:raise ValueError('清单或观看状态已变化，请重新预览')
            for h in plan['hashes']:
                task=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
                if task.get('provider')=='thunder':
                    if not self.engine.thunder.inspect(task)['verified_complete']:raise ValueError('迅雷任务状态已变化')
                    continue
                rows=self.engine.qb.info(h)
                if rows and (not self.engine.qb.owned(rows[0]) or Path(rows[0]['save_path']).resolve()!=Path(self.db.one('SELECT save_path FROM tasks WHERE hash=?',(h,))['save_path']).resolve()):raise ValueError('下载任务所有权或路径已变化，拒绝清理')
            # Durable tombstone first, so an interruption cannot trigger a redownload.
            self.db.execute('UPDATE shows SET cleanup_hold=1,authorized=0 WHERE id=?',(sid,))
            for h in plan['hashes']:
                task=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
                if task.get('provider')=='thunder':
                    self.db.execute("UPDATE thunder_queue SET state='cancelled',lease='' WHERE hash=?",(h,))
                elif self.engine.qb.info(h):
                    self.engine.qb.stop(h)
                    self.engine.qb.call('torrents/delete','POST',data={'hashes':h,'deleteFiles':'false'})
                self.db.execute("UPDATE tasks SET status='cleaned',error='用户确认清理，禁止自动重下' WHERE hash=?",(h,))
            for f in plan['files']:
                p=Path(f['path']);st=p.stat()
                if p.resolve()!=p or st.st_size!=f['size'] or st.st_mtime_ns!=f['mtime_ns']:raise ValueError('删除前文件变化，已停止，请重新核查')
                p.unlink() # Exact reviewed file only; never recursively remove directories.
                self.db.execute("UPDATE episodes SET status='cleaned',progress=0 WHERE show_id=? AND episode=?",(sid,f['episode']))
            self.db.event('cleanup',f'用户确认清理 {sid} 的 {len(plan["files"])} 个文件；历史保留')
            return {'ok':True,'bytes':plan['bytes']}


def register_cleanup(app,db,engine):
    cleaner=Cleanup(db,engine)
    @app.get('/api/cleanup')
    def candidates():
        out=[]
        for row in db.rows('SELECT DISTINCT show_id FROM episodes WHERE hash IS NOT NULL'):
            try:
                plan=cleaner.preview(row['show_id'])
                if plan:out.append(plan)
            except ValueError as ex:out.append({'id':row['show_id'],'blocked':str(ex)})
        return {'mode':'confirm_only','days':180,'items':out,'note':'只列出已完成且可核验的项目文件；旧动画、未完成文件和未知完成时间不会按时限删除。'}
    @app.post('/api/cleanup/{sid}/confirm')
    def confirm(sid:int,p:dict):
        if p.get('confirm')!='删除清单内文件':raise ValueError('请在页面核对清单并确认')
        return cleaner.confirm(sid,str(p.get('fingerprint','')))
