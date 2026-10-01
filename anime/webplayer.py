"""Read-only local media streaming and an isolated danmu-api adapter."""
import hashlib,json,math,subprocess,time,threading,shutil,secrets,re
from pathlib import Path
from urllib.parse import urlparse
import requests
from fastapi import HTTPException
from fastapi.responses import FileResponse
from .db import ROOT
from .subtitles import Subtitles
from .auto_danmu import match as match_danmu
from .audio_alignment import AlignmentService


def maintain_danmu(stop):
    """The already installed adapter is supervised locally, without a public proxy."""
    import psutil
    repo=ROOT/'vendor/danmuapi'
    node=shutil.which('node')
    if not node or not (repo/'node_modules').exists():return
    child=None
    while not stop.is_set() and not (ROOT/'data/stop.request').exists():
        try:
            pidfile=ROOT/'data/danmu.pid'
            p=psutil.Process(int(pidfile.read_text())) if pidfile.exists() else None
            alive=bool(p and Path(p.cwd()).resolve()==repo.resolve() and 'danmu_api/server.js' in p.cmdline())
        except (OSError,ValueError,psutil.Error):alive=False
        if not alive:
            try:
                with (ROOT/'logs/danmu-start.log').open('ab') as log:
                    child=subprocess.Popen([node,'danmu_api/server.js'],cwd=repo,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                pidfile.write_text(str(child.pid))
            except OSError:pass
        stop.wait(30)
    if child and child.poll() is None:child.terminate()


class WebPlayer:
    def __init__(self,db):
        self.db=db
        self.lock=threading.Lock()
        self.sources={}
        self.subtitles=Subtitles(db)
        self.auto_lock=threading.Lock()
        self.auto_cache={}
        self.alignment=None
        self.alignment_jobs={}
        self.alignment_lock=threading.Lock()
        self.inventory=[]
        self.inventory_mtime=0
        inventory=db.path.parent/'inventory.json'
        if inventory.exists():self.inventory=json.loads(inventory.read_text(encoding='utf-8'))
        with db.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS web_danmu_sources(
                source_key TEXT PRIMARY KEY,url TEXT NOT NULL UNIQUE)''')
            c.execute('''CREATE TABLE IF NOT EXISTS web_progress(
                media_id TEXT PRIMARY KEY,position REAL DEFAULT 0,duration REAL DEFAULT 0,
                last_played REAL,updated REAL,sources TEXT DEFAULT '[]')''')
            c.execute('''CREATE TABLE IF NOT EXISTS web_danmu_matches(
                media_id TEXT NOT NULL,source_key TEXT NOT NULL,
                episode_identity TEXT NOT NULL,
                PRIMARY KEY(media_id,source_key))''')

    def media(self):
        inventory=self.db.path.parent/'inventory.json'
        if inventory.exists() and inventory.stat().st_mtime_ns!=self.inventory_mtime:
            self.inventory=json.loads(inventory.read_text(encoding='utf-8'));self.inventory_mtime=inventory.stat().st_mtime_ns
        rows={}
        for x in self.inventory:
            if x.get('legacy_active'):continue
            path=Path(x['path'])
            key=hashlib.sha256(str(path).lower().encode()).hexdigest()[:32]
            rows[key]={'id':key,'name':path.name,'folder':path.parent.name,'path':str(path),'size':x['size'],'legacy':True}
        for x in self.db.rows("SELECT episodes.*,shows.title FROM episodes JOIN shows ON shows.id=episodes.show_id WHERE episodes.status='complete' OR (episodes.status='legacy_unverified' AND episodes.hash IS NULL)"):
            key=hashlib.sha256(x['path'].lower().encode()).hexdigest()[:32]
            if x['status']=='legacy_unverified' and key not in rows:continue
            rows[key]={'id':key,'name':Path(x['path']).name,'folder':x['title'],'path':x['path'],'size':x['size'],'legacy':not bool(x['hash']),'show_id':x['show_id'],'episode':x['episode'],'unverified':x['status']=='legacy_unverified'}
        return rows

    def item(self,key):
        row=self.media().get(key)
        if not row:raise HTTPException(404,'未找到已盘点的媒体')
        p=Path(row['path'])
        if not p.is_file() or p.stat().st_size!=row['size']:raise ValueError('文件不存在或大小已变化，请重新核查')
        return row

    def danmu(self,path,params=None):
        # This local, separately installed service never receives media bytes or credentials.
        r=requests.get('http://127.0.0.1:4872/api/v2/'+path,params=params,timeout=(3,90))
        r.raise_for_status();d=r.json()
        if d.get('success') is False:raise ValueError('弹幕源未返回结果，请核对标题或稍后重试')
        return d

    def bind_episodes(self,d):
        with self.lock:
            self.sources={k:v for k,v in self.sources.items() if time.time()-v['created']<86400}
            for ep in d.get('bangumi',{}).get('episodes',[]):
                ep.pop('source_key',None)
                url=str(ep.get('url',''))
                if re.fullmatch(r'[1-9][0-9]{0,11}',url) and str(ep.get('episodeTitle','')).startswith('【bahamut】'):
                    url='https://ani.gamer.com.tw/animeVideo.php?sn='+url
                try:
                    u=urlparse(url)
                    if u.scheme not in ('http','https') or u.hostname not in ('www.bilibili.com','www.iqiyi.com','v.youku.com','v.qq.com','ani.gamer.com.tw') or u.username or u.password or u.port:continue
                except ValueError:continue
                if u.hostname=='ani.gamer.com.tw' and not re.fullmatch(r'https://ani\.gamer\.com\.tw/animeVideo\.php\?sn=[1-9][0-9]{0,11}',url):continue
                with self.db.connect() as c:
                    c.execute('INSERT OR IGNORE INTO web_danmu_sources(source_key,url) VALUES(?,?)',
                              (secrets.token_urlsafe(18),url))
                    key=c.execute('SELECT source_key FROM web_danmu_sources WHERE url=?',(url,)).fetchone()[0]
                self.sources[key]={'url':url,'created':time.time()}
                ep['source_key']=key
                ep['source_identity']=hashlib.sha256(url.encode()).hexdigest()
        return d

    def comments(self,ids):
        with self.lock:
            if not isinstance(ids,list) or not 1<=len(ids)<=5 or any(not isinstance(i,str) or len(i)>100 for i in ids):
                raise ValueError('请重新搜索并选择1–5个剧集来源')
            urls=[]
            for sid in ids:
                source=self.db.one('SELECT url FROM web_danmu_sources WHERE source_key=?',(sid,))
                if source is None:
                    raise ValueError('保存的来源已失效，请移除后重新添加')
                urls.append(source['url'])
        combined=[];sources=[]
        for sid,url in zip(ids,urls):
            identity=hashlib.sha256(url.encode()).hexdigest()
            # Sender ids are only meaningful together with the site that issued them.
            site=SITES.get(urlparse(url).hostname,'')
            try:
                rows=normalize_comments(self.danmu('comment',{'url':url,'format':'json'}))
                combined+=rows;sources.append({'id':sid,'source_identity':identity,'site':site,'count':len(rows),'comments':rows})
            except Exception:sources.append({'id':sid,'source_identity':identity,'site':site,'count':0,'comments':[],'error':'来源获取失败或超时，可重试'})
        seen=set();out=[]
        for x in combined:
            k=(round(x['time'],1),x['text'],x['mode'])
            if k not in seen:seen.add(k);out.append(x)
        return {'comments':sorted(out,key=lambda x:x['time']),'sources':sources,'fetched_at':time.time()}

    def auto_danmu(self,key):
        item=self.item(key)
        show=self.db.one('SELECT title,original,mapping FROM shows WHERE id=?',(item.get('show_id'),))
        cache_key=(key,item.get('show_id'),item.get('episode'),item.get('unverified'),json.dumps(show,sort_keys=True))
        with self.auto_lock:
            cached=self.auto_cache.get(cache_key)
            if cached and time.time()-cached[0]<600:return cached[1]
            try:result=match_danmu(self,item)
            except Exception:return {'status':'error','message':'弹幕自动查找暂时失败，可重试或手动选择来源。','comments':[],'sources':[]}
            if result['status']=='matched':
                with self.db.connect() as c:
                    c.executemany('INSERT INTO web_danmu_matches(media_id,source_key,episode_identity) VALUES(?,?,?) '
                                  'ON CONFLICT(media_id,source_key) DO UPDATE SET episode_identity=excluded.episode_identity',
                                  [(key,s['id'],self.episode_identity(item)) for s in result.get('selected',[])])
                if len(self.auto_cache)>=100:self.auto_cache.clear()
                self.auto_cache[cache_key]=(time.time(),result)
            return result

    def episode_identity(self,item):
        show=self.db.one('SELECT mapping FROM shows WHERE id=?',(item.get('show_id'),))
        return json.dumps([item.get('show_id'),item.get('episode'),show.get('mapping') if show else None])

    def start_alignment(self,key,p):
        item=self.item(key)
        if not item.get('show_id') or not item.get('episode') or item.get('unverified'):
            return {'status':'unavailable','message':'请先核对本地文件的作品与集数，再自动对齐'}
        sid=p.get('source_id')
        if not isinstance(sid,str) or len(sid)>100:raise ValueError('来源无效')
        source=self.db.one('SELECT url FROM web_danmu_sources WHERE source_key=?',(sid,))
        if not source:raise ValueError('请重新搜索并选择本集来源')
        identity=self.episode_identity(item)
        if p.get('confirmed') is True:
            self.db.execute('INSERT INTO web_danmu_matches(media_id,source_key,episode_identity) VALUES(?,?,?) '
                            'ON CONFLICT(media_id,source_key) DO UPDATE SET episode_identity=excluded.episode_identity',(key,sid,identity))
        if not self.db.one('SELECT 1 FROM web_danmu_matches WHERE media_id=? AND source_key=? AND episode_identity=?',(key,sid,identity)):
            return {'status':'unavailable','message':'请确认此来源是当前这一集，再点击自动对齐'}
        with self.alignment_lock:
            if self.alignment is None:
                try:ffmpeg=self.subtitles.tool('ffmpeg');ffprobe=self.subtitles.tool('ffprobe')
                except ValueError:return {'status':'unavailable','message':'未安装音频读取工具，仍可手动调时'}
                fpcalc=ROOT/'tools/chromaprint/fpcalc.exe'
                self.alignment=AlignmentService(self.db.path.parent/'cache/audio-alignment',ffmpeg,ffprobe,str(fpcalc))
            result=self.alignment.start(item,source['url'],hashlib.sha256(source['url'].encode()).hexdigest(),force=p.get('force') is True)
            if result.get('job_id'):
                if len(self.alignment_jobs)>=200:
                    self.alignment_jobs={j:v for j,v in self.alignment_jobs.items() if self.alignment.status(j).get('status') in ('queued','running')}
                self.alignment_jobs[result['job_id']]=(key,Path(item['path']).stat().st_mtime_ns,identity)
            return result

    def alignment_status(self,key,job,cancel=False):
        item=self.item(key)
        with self.alignment_lock:
            if not self.alignment or self.alignment_jobs.get(job)!=(key,Path(item['path']).stat().st_mtime_ns,self.episode_identity(item)):
                raise HTTPException(404,'对齐任务已失效')
            if cancel:self.alignment.cancel(job)
            return self.alignment.status(job)


SITES={'www.bilibili.com':'bilibili','ani.gamer.com.tw':'bahamut','www.iqiyi.com':'iqiyi','v.youku.com':'youku','v.qq.com':'tencent'}


def normalize_comments(data):
    result=[];seen=set()
    for c in data.get('comments',[])[:100000]:
        try:
            p=c['p'].split(',');t=float(p[0]);mode=int(p[1]);color=int(p[2]);text=str(c['m'])[:300]
            if not math.isfinite(t) or not 0<=t<=86400 or mode not in (1,4,5) or not text:continue
            key=(round(t,1),text,mode)
            if key in seen:continue
            row={'time':t,'text':text,'mode':{1:0,5:1,4:2}[mode],'color':f'#{color & 0xffffff:06x}'}
            sender=c.get('sender')
            if isinstance(sender,(str,int)) and not isinstance(sender,bool) and 0<len(str(sender))<=64:row['user']=str(sender)
            seen.add(key);result.append(row)
        except (ValueError,KeyError,IndexError,TypeError):continue
    return sorted(result,key=lambda x:x['time'])


def register_webplayer(app,db):
    player=WebPlayer(db)
    @app.get('/watch')
    def page():return FileResponse(ROOT/'static/watch.html')
    @app.get('/api/web/media')
    def media(q:str='',show_id:int=0,offset:int=0,limit:int=150):
        rows=sorted(player.media().values(),key=lambda x:(x['legacy'],x['folder'],x['name']))
        return [{k:v for k,v in r.items() if k!='path'} for r in rows
            if (not show_id or r.get('show_id')==show_id) and q.lower() in (r['name']+' '+r['folder']).lower()][max(0,offset):max(0,offset)+min(500,max(1,limit))]
    @app.get('/api/web/media/{key}')
    def item(key:str):
        r=player.item(key)
        return {**{k:v for k,v in r.items() if k!='path'},'progress':db.one('SELECT * FROM web_progress WHERE media_id=?',(key,))}
    @app.get('/api/web/media/{key}/stream')
    def stream(key:str):
        r=player.item(key)
        return FileResponse(r['path'],media_type='video/mp4' if Path(r['path']).suffix.lower()=='.mp4' else 'video/x-matroska',headers={'Cross-Origin-Resource-Policy':'same-origin'})
    @app.post('/api/web/media/{key}/progress')
    def progress(key:str,p:dict):
        player.item(key);pos=float(p.get('position',0));duration=float(p.get('duration',0))
        if not all(math.isfinite(x) and 0<=x<=86400 for x in (pos,duration)) or pos>duration+1:raise ValueError('播放进度无效')
        now=time.time();played=now if p.get('playing') is True and pos>0 else None
        db.execute('''INSERT INTO web_progress(media_id,position,duration,last_played,updated) VALUES(?,?,?,?,?)
          ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
          last_played=coalesce(excluded.last_played,web_progress.last_played),updated=excluded.updated''',(key,pos,duration,played,now))
        return {'ok':True} # Never turn a seek, open, or end event into "finished".
    @app.post('/api/web/media/{key}/desktop')
    def desktop(key:str):
        r=player.item(key);exe=Path(db.get('dandan_exe'))
        if exe.name.lower()!='dandanplay.exe' or not exe.is_file():raise ValueError('弹弹play路径无效')
        subprocess.Popen([str(exe),r['path']],cwd=exe.parent,shell=False)
        return {'ok':True}
    @app.get('/api/web/danmu/search')
    def search(q:str):
        if not 1<=len(q)<=100:raise ValueError('请输入作品名')
        return player.danmu('search/anime',{'keyword':q})
    @app.get('/api/web/danmu/show/{sid}')
    def episodes(sid:int):
        return player.bind_episodes(player.danmu('bangumi/'+str(sid)))
    @app.post('/api/web/danmu/comments')
    def comments(p:dict):
        return player.comments(p.get('episodes',[]))
    @app.get('/api/web/media/{key}/danmu')
    def automatic_danmu(key:str):
        return player.auto_danmu(key)
    @app.post('/api/web/media/{key}/alignment')
    def alignment(key:str,p:dict):return player.start_alignment(key,p)
    @app.get('/api/web/media/{key}/alignment/{job}')
    def alignment_status(key:str,job:str):return player.alignment_status(key,job)
    @app.post('/api/web/media/{key}/alignment/{job}/cancel')
    def alignment_cancel(key:str,job:str):return player.alignment_status(key,job,cancel=True)
    @app.get('/api/web/media/{key}/subtitles')
    def subtitles(key:str):
        tracks=player.subtitles.tracks(player.item(key))
        supported=[t for t in tracks if t['supported']]
        return {'tracks':[{**t,'url':f'/api/web/media/{key}/subtitles/{t["index"]}.vtt',
                           'ass_url':f'/api/web/media/{key}/subtitles/{t["index"]}.ass' if t['codec'] in ('ass','ssa') else None} for t in tracks],
                'default':supported[0]['index'] if supported else None}
    @app.get('/api/web/media/{key}/subtitles/{index}.ass')
    def subtitle_ass(key:str,index:int):
        path=player.subtitles.extract(player.item(key),index,'ass')
        return FileResponse(path,media_type='text/plain; charset=utf-8')
    @app.get('/api/web/media/{key}/subtitle-fonts')
    def subtitle_fonts(key:str):
        fonts=player.subtitles.fonts(player.item(key))
        return {'fonts':[f'/api/web/media/{key}/subtitle-fonts/{f["index"]}' for f in fonts]}
    @app.get('/api/web/media/{key}/subtitle-fonts/{index}')
    def subtitle_font(key:str,index:int):
        return FileResponse(player.subtitles.extract_font(player.item(key),index),media_type='application/octet-stream')
    @app.get('/api/web/media/{key}/subtitles/{index}.vtt')
    def subtitle_file(key:str,index:int):
        path=player.subtitles.extract(player.item(key),index)
        return FileResponse(path,media_type='text/vtt; charset=utf-8',headers={'Cross-Origin-Resource-Policy':'same-origin'})
