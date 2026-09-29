"""One-way local history import. No credentials, remote server, or player writes."""
import datetime, hashlib, json, math, ntpath, shutil, subprocess, tempfile, threading, time
from pathlib import Path
from fastapi.responses import FileResponse
from .db import ROOT
from .webplayer import WebPlayer


def path_key(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def timestamp(value):
    try:
        t=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
        if t.tzinfo is None:return None
        value=t.timestamp()
        return value if 946684800<=value<=time.time()+300 else None
    except (ValueError,TypeError,AttributeError,OverflowError):return None


def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and 0<=n<=86400 else 0
    except (ValueError,TypeError):return 0


class DandanHistory:
    def __init__(self,db):
        self.db=db;self.player=WebPlayer(db);self.lock=threading.Lock()
        db.execute('''CREATE TABLE IF NOT EXISTS dandan_history(
            media_id TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,name TEXT,title TEXT,
            last_played REAL,position REAL,duration REAL,source_watched REAL,updated REAL)''')

    def extract(self):
        root=Path(self.db.get('dandan_exe')).parent
        source=root/'Cache/dandanplay.litedb';library=root/'LiteDB.dll'
        if not source.is_file() or not library.is_file():raise ValueError('未找到弹弹play本机历史数据库或读取组件')
        paths=[source,source.with_name('dandanplay-log.litedb')]
        def signature():
            return [(p.stat().st_size,p.stat().st_mtime_ns) if p.exists() else None for p in paths]
        # Copy DB and WAL together. A changing source retries on the next poll.
        # The source is only ever opened for reads; LiteDB never receives its path.
        with tempfile.TemporaryDirectory(prefix='dandan-',dir=self.db.path.parent) as folder:
            before=signature()
            for p in paths:
                if p.exists():shutil.copyfile(p,Path(folder)/p.name)
            if before!=signature():raise ValueError('弹弹play正在保存历史，本轮暂缓，稍后自动重试')
            shell=shutil.which('pwsh') or shutil.which('powershell')
            if not shell:raise ValueError('缺少本机 PowerShell 读取环境')
            result=subprocess.run([shell,'-NoProfile','-NonInteractive','-File',str(ROOT/'scripts/Read-DandanHistory.ps1'),
                '-Database',str(Path(folder)/source.name),'-Library',str(library)],capture_output=True,
                timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:raise ValueError('弹弹play历史读取失败；原记录未修改，稍后自动重试')
            data=json.loads(result.stdout.decode('utf-8-sig'))
        if data.get('schema')!=1 or any(not isinstance(data.get(k),list) for k in ('recent','files','seen')):
            raise ValueError('弹弹play历史格式不兼容，已停止导入')
        # Retain a private, local-only snapshot, including unmatched history.
        target=self.db.path.parent/'dandan-history.json';temp=target.with_suffix('.tmp')
        temp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');temp.replace(target)
        return data

    def merge(self,data,media=None):
        media=self.player.media() if media is None else media
        recent={};files={};seen={};now=time.time()
        for x in data['recent']:
            t=timestamp(x.get('played'))
            if not t or not x.get('path'):continue
            k=path_key(x['path'])
            if t>recent.get(k,{}).get('time',0):recent[k]={**x,'time':t}
        for x in data['files']:
            if x.get('path'):files[path_key(x['path'])]=x
        for x in ([] if data.get('account_scope')=='ambiguous' else data['seen']):
            t=timestamp(x.get('watched'));eid=str(x.get('episode_id',''))
            if t and eid and eid!='0':seen[eid]=max(t,seen.get(eid,0))
        matched=0;progress_count=0;seen_count=0;watch_records={}
        with self.db.gate,self.db.connect() as c:
            # Recompute exact associations each time; never carry an old show/season mapping forward.
            c.execute('UPDATE dandan_history SET show_id=NULL,episode=NULL,source_watched=NULL')
            for mid,m in media.items():
                k=path_key(m['path']);r=recent.get(k);f=files.get(k)
                # A reused/replaced file is not an identity match.
                if not f or f.get('size')!=m['size']:continue
                watched=seen.get(str(f.get('episode_id','')))
                if not r and not watched:continue
                p=Path(m['path'])
                if not p.is_file() or p.stat().st_size!=m['size']:continue
                matched+=1;seen_count+=bool(watched)
                pos=number(r.get('seconds')) if r else 0;duration=number(f.get('duration'))
                if not duration and r and 0<number(r.get('fraction'))<=1:duration=number(pos/number(r['fraction']))
                if pos>duration+1:pos=0
                played=r['time'] if r else None
                c.execute('''INSERT INTO dandan_history VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(media_id) DO UPDATE SET show_id=excluded.show_id,episode=excluded.episode,
                    name=excluded.name,title=excluded.title,last_played=excluded.last_played,
                    position=excluded.position,duration=excluded.duration,source_watched=excluded.source_watched,updated=excluded.updated''',
                    (mid,m.get('show_id'),m.get('episode'),m['name'],m['folder'],played,pos,duration,watched,now))
                if pos>0 and duration>0 and played:
                    progress_count+=1
                    c.execute('''INSERT INTO web_progress(media_id,position,duration,last_played,updated) VALUES(?,?,?,?,?)
                        ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                        last_played=max(coalesce(web_progress.last_played,0),excluded.last_played),updated=excluded.updated
                        WHERE coalesce(web_progress.updated,0)<excluded.updated''',(mid,pos,duration,played,played))
                # The user wants the player's existing watched flags carried over.
                # LastPlay/exit position alone still cannot create a watched flag.
                if watched and m.get('show_id') and m.get('episode'):
                    key=(m['show_id'],m['episode'])
                    watch_records[key]=max(watched,watch_records.get(key,0))
            for (sid,ep),watched in watch_records.items():
                c.execute('''INSERT INTO watches(show_id,episode,finished,method,updated)
                    VALUES(?,?,1,'dandan',?) ON CONFLICT(show_id,episode) DO UPDATE SET
                    finished=1,updated=max(watches.updated,excluded.updated)
                    WHERE watches.method='dandan' ''',(sid,ep,watched))
            # Count episode identities, not source files/versions. Never change tracking state.
            c.execute('''UPDATE shows SET watched=(SELECT count(*) FROM watches
                WHERE watches.show_id=shows.id AND finished=1)''')
            synced=c.execute("SELECT count(*) FROM watches WHERE method='dandan' AND finished=1").fetchone()[0]
        return {'ok':True,'success':now,'matched_files':matched,'resume_files':progress_count,
                'seen_files':seen_count,'source_recent':len(recent),'source_seen':len(seen),'interval':60,
                'account_scope':data.get('account_scope','unknown'),'synced_watched':synced}

    def sync(self):
        if not self.lock.acquire(blocking=False):return {**(self.db.get('dandan_history_health') or {}),'busy':True}
        try:
            result=self.merge(self.extract());self.db.set('dandan_history_health',result);return result
        except Exception as ex:
            old=self.db.get('dandan_history_health') or {}
            msg=str(ex) if isinstance(ex,ValueError) else '本机历史读取暂时失败，将自动重试'
            if old.get('error')!=msg:self.db.event('dandan-history',msg,'error')
            result={**old,'ok':False,'attempt':time.time(),'error':msg,'interval':60}
            self.db.set('dandan_history_health',result);return result
        finally:self.lock.release()

    def run(self,stop):
        while not stop.is_set():self.sync();stop.wait(60)

    def confirm(self,ids):
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) for i in ids):
            raise ValueError('请选择1–500条已经看完的记录')
        with self.db.gate,self.db.connect() as c:
            rows=[]
            for mid in set(ids):
                r=c.execute('SELECT * FROM dandan_history WHERE media_id=?',(mid,)).fetchone()
                if not r or not r['show_id'] or not (r['last_played'] or r['source_watched']):raise ValueError('记录尚未关联作品，无法确认集数')
                rows.append(r)
            for r in rows:
                # Preserve original activity time for 180-day cleanup; confirmation is not a new play.
                t=max(r['last_played'] or 0,r['source_watched'] or 0)
                c.execute('''INSERT INTO watches(show_id,episode,finished,method,updated) VALUES(?,?,1,'manual-dandan',?)
                    ON CONFLICT(show_id,episode) DO UPDATE SET finished=1,method='manual-dandan',updated=excluded.updated''',
                    (r['show_id'],r['episode'],t))
                c.execute('UPDATE shows SET watched=(SELECT count(*) FROM watches WHERE show_id=? AND finished=1) WHERE id=?',(r['show_id'],r['show_id']))
        return {'ok':True,'confirmed':len(rows)}


def register_history(app,db):
    history=DandanHistory(db);app.state.dandan_history=history
    @app.get('/history-sync')
    def page():return FileResponse(ROOT/'static/history-sync.html')
    @app.get('/api/history/dandan')
    def records(q:str='',show_id:int=0,offset:int=0,limit:int=100):
        clause="(h.name LIKE ? OR h.title LIKE ?) AND (h.last_played IS NOT NULL OR h.source_watched IS NOT NULL)";args=['%'+q+'%']*2
        if show_id:clause+=' AND h.show_id=?';args.append(show_id)
        total=db.one('SELECT count(*) n FROM dandan_history h WHERE '+clause,args)['n']
        rows=db.rows('''SELECT h.*,coalesce(w.finished,0) finished,w.method FROM dandan_history h
            LEFT JOIN watches w ON h.show_id=w.show_id AND h.episode=w.episode WHERE '''+clause+
            ' ORDER BY max(coalesce(h.last_played,0),coalesce(h.source_watched,0)) DESC LIMIT ? OFFSET ?',
            [*args,min(500,max(1,limit)),max(0,offset)])
        return {'status':db.get('dandan_history_health'),'total':total,'items':rows}
    @app.post('/api/history/dandan/sync')
    def sync():return history.sync()
    @app.post('/api/history/dandan/confirm')
    def confirm(p:dict):return history.confirm(p.get('media_ids'))
