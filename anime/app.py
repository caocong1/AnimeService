import contextlib,datetime,ipaddress,json,os,secrets,subprocess,threading,time
from pathlib import Path
from urllib.parse import urlencode,urlparse
import requests
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from .db import Store,ROOT,DEFAULTS
from .engine import Engine
from .net import safe_error
from .download_policy import status as downloader_status
from .sources import Catalog,parse_rss
from .releases import episodes_of,Rejected,season_of,validate_title
from .deployment import load as load_deployment
from .board import episode_board

def catalog_quarters(today=None):
    """The quarter that opens within three weeks and the one before it, oldest first."""
    horizon=(today or datetime.date.today())+datetime.timedelta(days=21)
    y,m=horizon.year,((horizon.month-1)//3)*3+1
    py,pm=(y,m-3) if m>1 else (y-1,10)
    return [f'{py}-{pm:02}',f'{y}-{m:02}']

def create_app(db=None,engine=None,start_worker=True):
    db=db or Store();engine=engine or Engine(db);token=secrets.token_urlsafe(32);jobs=threading.Lock()
    deployment=load_deployment()
    @contextlib.asynccontextmanager
    async def lifespan(app):
        if start_worker:
            thread=threading.Thread(target=engine.run,daemon=True,name='anime-worker');thread.start()
            from .webplayer import maintain_danmu
            threading.Thread(target=maintain_danmu,args=(engine.stop_event,),daemon=True,name='danmu-supervisor').start()
            from .desktop_guardian import maintain_desktop
            threading.Thread(target=maintain_desktop,args=(engine.stop_event,db),daemon=True,name='desktop-guardian').start()
            threading.Thread(target=app.state.dandan_history.run,args=(engine.stop_event,),daemon=True,name='dandan-history').start()
            from .edge import maintain_https
            threading.Thread(target=maintain_https,args=(engine.stop_event,),daemon=True,name='https-edge').start()
        yield
        engine.stop_event.set();engine.wake.set()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None);app.state.db=db;app.state.engine=engine
    from .remote_access import RemoteAccess,register_access
    access=RemoteAccess(db);register_access(app,access)
    @app.middleware('http')
    async def local_only(request:Request,call_next):
        authority=request.headers.get('host','').lower()
        host=authority.split(':')[0]
        local=host in ('127.0.0.1','localhost') and request.client.host in ('127.0.0.1','::1','testclient')
        request.state.local=local
        try:
            lan_peer=ipaddress.ip_address(request.client.host) in ipaddress.ip_network(deployment.get('lan_network','127.0.0.1/32'))
        except ValueError:
            lan_peer=False
        lan=authority==deployment.get('lan_authority','') and lan_peer and request.url.scheme=='http'
        request.state.lan=lan
        public=authority in deployment.get('public_authorities',[]) and request.url.scheme=='https'
        if not local and not public and not lan:return JSONResponse({'error':'访问地址不受信任'},status_code=403)
        path=request.url.path
        public_asset=path in ('/login','/static/login.js','/static/tokens.css','/static/app.css','/static/theme.js','/static/icon.svg','/static/manifest.webmanifest')
        login=path=='/api/access/login'
        if lan and path=='/login':return RedirectResponse('/',status_code=303)
        if not (local or lan) and not (public_asset or login) and not access.valid(request):
            if path.startswith('/api/'):return JSONResponse({'error':'请登录这台设备'},status_code=401)
            return RedirectResponse('/login',status_code=303)
        if not local and (path.startswith('/api/thunder/') or path.startswith('/api/player/') or path.endswith('/desktop')):
            return JSONResponse({'error':'此操作需要在家中电脑执行'},status_code=403)
        if request.method not in ('GET','HEAD'):
            origin=request.headers.get('origin')
            expected=('http://127.0.0.1:4871','http://localhost:4871') if local else (('http://' if lan else 'https://')+authority,)
            # Some local browser/network configurations strip the Origin port.
            # Accept only the matching loopback host when the browser's protected
            # Fetch Metadata explicitly attests same-origin. The token check below
            # still applies; remote sessions and other origins get no exception.
            loopback_port_omitted=(local and authority in ('127.0.0.1:4871','localhost:4871')
                and origin=='http://'+host and request.headers.get('sec-fetch-site')=='same-origin')
            if (origin and origin not in expected and not loopback_port_omitted) or (not local and not origin):
                return JSONResponse({'error':'跨站请求被拒绝'},status_code=403)
            if not login and not secrets.compare_digest(request.headers.get('x-anime-token',''),token):return JSONResponse({'error':'会话已更新，请刷新页面'},status_code=403)
        response=await call_next(request)
        if path.startswith('/api/') or path=='/login':response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' https: data:; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'"
        return response
    @app.exception_handler(ValueError)
    async def value_error(request,e):return JSONResponse({'error':str(e)[:400]},status_code=400)
    @app.exception_handler(Exception)
    async def unexpected(request,e):
        db.event('api',safe_error(e),'error');return JSONResponse({'error':safe_error(e)},status_code=503)
    def show(sid):
        s=db.show(sid)
        if not s:raise HTTPException(404,'没有此条目')
        return s
    def background(fn):
        if not jobs.acquire(blocking=False):raise ValueError('资料任务正在运行，请稍后')
        def run():
            try:fn()
            except Exception as e:db.event('catalog',safe_error(e),'error')
            finally:jobs.release()
        threading.Thread(target=run,daemon=True).start()
        return {'ok':True}
    @app.get('/')
    def index():return FileResponse(ROOT/'static/index.html')
    @app.get('/manage')
    def manage_page():return FileResponse(ROOT/'static/manage.html')
    @app.get('/show/{sid}')
    def show_page(sid:int):return FileResponse(ROOT/'static/show.html')
    @app.get('/season')
    def season_page():return FileResponse(ROOT/'static/season.html')
    @app.get('/library')
    def library_page():return FileResponse(ROOT/'static/library.html')
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    @app.get('/api/bootstrap')
    def bootstrap(request:Request):return {'token':token,'settings':{k:db.get(k,v) for k,v in DEFAULTS.items()},'version':'2.0-ui','local':request.state.local,'quarters':catalog_quarters()}
    @app.get('/api/home')
    def home_summary():
        import hashlib
        data=db.rows("""SELECT e.show_id,e.episode,e.path,e.size,s.title,s.image,s.total,s.watched,s.state,
            e.first_completed FROM episodes e JOIN shows s ON s.id=e.show_id
            WHERE e.status='complete' AND s.state NOT IN ('dropped','paused','completed')
            AND NOT EXISTS(SELECT 1 FROM watches w WHERE w.show_id=e.show_id AND w.episode=e.episode AND w.finished=1)
            ORDER BY e.first_completed DESC,e.show_id,e.episode""")
        entries=[]
        for row in data:
            p=Path(row.pop('path') or '')
            try:
                if not p.is_file() or p.stat().st_size!=row.pop('size'):continue
            except OSError:continue
            row['media_id']=hashlib.sha256(str(p).lower().encode()).hexdigest()[:32]
            row['progress']=db.one('SELECT position,duration,last_played FROM web_progress WHERE media_id=?',(row['media_id'],))
            entries.append(row)
        # An actual unfinished playback comes before new unwatched episodes.
        resume=[x for x in entries if x['progress'] and x['progress']['last_played'] and 0<x['progress']['position']<x['progress']['duration']-30]
        resume.sort(key=lambda x:x['progress']['last_played'],reverse=True)
        first={}
        for x in entries:
            if x['show_id'] not in first or x['episode']<first[x['show_id']]['episode']:first[x['show_id']]=x
        return {'resume':resume[:8],'next':list(first.values())[:18],'unwatched':len(entries),
                'watching':db.one("SELECT count(*) n FROM shows WHERE selected=1 AND state IN ('trial','watching')")['n']}
    @app.get('/api/status')
    def status():
        return {'service':'AnimeService','heartbeat':db.get('heartbeat'),'qbit':db.get('qbit_health'),'downloader':downloader_status(db),'catalog_job':db.get('catalog_job'),
            'catalogs':{q:db.get('catalog_'+q) for q in catalog_quarters()},
            'sources':db.rows('SELECT sources.*,shows.title FROM sources JOIN shows ON shows.id=sources.show_id'),
            'tasks':db.rows('SELECT * FROM tasks ORDER BY created DESC LIMIT 100'),
            'events':db.rows('SELECT * FROM events ORDER BY id DESC LIMIT 60'),'player':db.get('player_health'),'dandan_history':db.get('dandan_history_health'),
            'authorized_count':db.one('SELECT count(*) n FROM shows WHERE authorized=1')['n'],
            'watch_counts':db.rows('''SELECT id,watched,(SELECT count(*) FROM episodes e WHERE e.show_id=shows.id
                AND e.status='complete' AND NOT EXISTS (SELECT 1 FROM watches w WHERE w.show_id=e.show_id
                AND w.episode=e.episode AND w.finished=1)) unwatched FROM shows WHERE selected=1'''),
            'schedule':{'poll_seconds':db.get('poll_seconds',180),'requested':db.get('check_requested',0),
                        'completed':db.get('check_completed',0),'started':db.get('check_started_at'),
                        'finished':db.get('check_finished_at')}}
    @app.get('/api/shows')
    def shows(quarter:str='',scope:str='catalog',q:str='',kind:str='series'):
        clause='1=1';args=[]
        if scope=='home':clause="selected=1 AND state IN ('trial','watching','wish','paused')"
        elif scope=='history':clause='selected=1'
        elif quarter:
            clause='(quarter=? OR id IN (SELECT show_id FROM quarter_members WHERE quarter=?))';args.extend([quarter,quarter])
        if q:clause+=' AND (title LIKE ? OR original LIKE ?)';args+=['%'+q+'%','%'+q+'%']
        data=db.rows('SELECT * FROM shows WHERE '+clause+' ORDER BY score DESC,title',args)
        if scope=='catalog':
            # Positive non-Japanese tags are excluded; uncertain entries remain visible and labelled.
            import re
            filtered=[]
            for s in data:
                meta=json.loads(s['metadata']);tags={t.get('name','') for t in json.loads(s['tags'])}|set(meta.get('meta_tags',[]))
                if meta.get('nsfw') or tags & {'里番','R18','18禁'}:continue
                if tags & {'国产','中国','欧美','美国','法国','韩国','国漫','英国','俄罗斯','苏联','加拿大','西班牙'} and '日本' not in tags:continue
                japanese='日本' in tags or re.search('[ぁ-ゟ゠-ヿ]',s['original'])
                if kind!='uncertain' and not japanese:continue
                if kind=='series' and meta.get('platform') not in ('TV','WEB'):continue
                s['platform']=meta.get('platform','');s['region_confidence']='已标注日本' if japanese else '地区待核实'
                filtered.append(s)
            data=filtered
        for s in data:
            membership=db.one('SELECT kind FROM quarter_members WHERE show_id=? AND quarter=?',(s['id'],quarter))
            s['quarter_kind']=membership['kind'] if membership else 'new'
            s['tags']=json.loads(s['tags']);s['mapping']=json.loads(s['mapping']);s.pop('metadata')
            eps=db.rows('SELECT episode,status,progress,first_completed,path FROM episodes WHERE show_id=?',(s['id'],))
            s['downloaded']=sum(e['status']=='complete' for e in eps);s['downloading']=sum(e['status'] not in ('complete','held') for e in eps)
            times=[]
            for e in eps:
                if e['status']!='complete':continue
                if e['first_completed']:times.append(e['first_completed'])
                elif e['path']:
                    try:times.append(Path(e['path']).stat().st_mtime)
                    except OSError:pass
            s['download_updated']=max(times,default=0)
            seen={w['episode'] for w in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1',(s['id'],))}
            s['unwatched']=sum(e['status']=='complete' and e['episode'] not in seen for e in eps)
            if scope in ('home','history'):s.update(episode_board(db,engine,s))
        data.sort(key=lambda s:(0 if s['downloaded'] else 1,-s['download_updated'] if s['downloaded'] else 0,s['air_date'] or '9999',s['id']))
        return data
    @app.get('/api/shows/{sid}')
    def details(sid:int):
        import hashlib
        s=show(sid);s['mapping']=json.loads(s['mapping']);s['tags']=json.loads(s['tags']);s['metadata']=json.loads(s['metadata'])
        episodes=db.rows('SELECT episodes.*,coalesce(watches.finished,0) finished,watches.method watch_method FROM episodes LEFT JOIN watches ON episodes.show_id=watches.show_id AND episodes.episode=watches.episode WHERE episodes.show_id=? ORDER BY episode',(sid,))
        for e in episodes:
            # The web player's media key, so an episode row can link straight to /watch.
            e['media_id']=hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32] if e['path'] else None
            e['progress']=db.one('SELECT position,duration FROM web_progress WHERE media_id=?',(e['media_id'],)) if e['media_id'] else None
        return {'show':s,'episodes':episodes,
            'watches':db.rows('SELECT * FROM watches WHERE show_id=? ORDER BY episode',(sid,)),
            'sources':db.rows('SELECT * FROM sources WHERE show_id=?',(sid,)),
            'candidates':db.rows('SELECT * FROM candidates WHERE show_id=? ORDER BY id DESC LIMIT 100',(sid,)),
            'gaps':engine.gaps(sid),'board':episode_board(db,engine,s),'airings':db.rows('SELECT * FROM airings WHERE show_id=? ORDER BY episode',(sid,))}
    @app.post('/api/catalog/sync')
    def sync(payload:dict):
        quarters=payload.get('quarters',catalog_quarters())
        if not isinstance(quarters,list) or len(quarters)>4:raise ValueError('最多同步四个季度')
        parsed=[]
        for q in quarters:
            y,m=map(int,q.split('-'))
            if m not in (1,4,7,10) or not 2000<=y<=2100:raise ValueError('无效季度')
            parsed.append((y,m))
        return background(lambda:[engine.catalog.sync_quarter(y,m) for y,m in parsed])
    @app.post('/api/shows/{sid}/refresh')
    def refresh(sid:int):show(sid);return background(lambda:engine.catalog.details(sid))
    @app.post('/api/shows/{sid}/state')
    def set_state(sid:int,payload:dict):engine.state(sid,payload['state']);return {'ok':True}
    @app.post('/api/shows/{sid}/unselect')
    def unselect(sid:int):
        # Undo for a fresh "想看": only a wish with no downloads, sources or authorization leaves the list.
        with db.gate:
            s=show(sid)
            if s['state']!='wish' or s['authorized'] or db.one('SELECT 1 FROM tasks WHERE show_id=?',(sid,)) or db.one('SELECT 1 FROM watches WHERE show_id=?',(sid,)):
                raise ValueError('这部作品已有记录，只能改状态')
            db.execute('UPDATE shows SET selected=0 WHERE id=?',(sid,))
        return {'ok':True}
    @app.post('/api/shows/{sid}/feedback')
    def feedback(sid:int,payload:dict):
        show(sid);rating=payload.get('rating')
        if rating is not None and (not isinstance(rating,int) or not 1<=rating<=10):raise ValueError('评分为1–10或留空')
        db.execute('UPDATE shows SET rating=?,feedback=?,drop_reason=? WHERE id=?',(rating,str(payload.get('feedback',''))[:4000],str(payload.get('drop_reason',''))[:1000],sid))
        return {'ok':True}
    @app.get('/api/shows/{sid}/mikan')
    def mikan(sid:int):show(sid);return engine.catalog.mikan_matches(sid)
    @app.post('/api/shows/{sid}/prepare')
    def prepare(sid:int):
        s=show(sid)
        if time.time()-db.get('subject_details_'+str(sid),0)>21600:
            engine.catalog.subject(sid);s=show(sid)
        valid=[]
        try:matches=engine.catalog.mikan_matches(sid)
        except Exception as ex:
            db.event('discovery:'+str(sid),safe_error(ex),'warning');matches=[]
        for match in matches[:8]:
            try:engine.catalog.mikan_mapping(match['id'],sid);valid.append(match)
            except ValueError:continue
        if len(valid)>1:raise ValueError('蜜柑出现多个相同作品条目，需要核对来源')
        mid=valid[0]['id'] if valid else ''
        aliases=[s['title'],s['original']]
        for field in json.loads(s['metadata']).get('infobox',[]):
            if field.get('key')=='别名' and isinstance(field.get('value'),list):
                aliases.extend(x['v'] for x in field['value'] if isinstance(x,dict) and isinstance(x.get('v'),str))
        season=season_of(s['title']) or season_of(s['original']) or 1
        m={'aliases':list(dict.fromkeys(x for x in aliases if x))[:20], 'season':season,'offset':0,
           'start':1,'end':s['total'] or 999,'mikan_id':mid,'dmhy_keyword':'','groups':[],
           'resolution':db.get('resolution','1080'),'subtitle':db.get('subtitle','any_zh')}
        if mid:
            items=parse_rss(engine.sources.web.fetch(f'https://mikanani.me/RSS/Bangumi?bangumiId={mid}').content)
            source=valid[0]
        else:
            # A title search cannot inherit Mikan's subject-ID proof. Require explicit sequel season.
            m['require_season']=season>1
            import re
            if re.search(r'第.*(?:部分|赛段|賽段)',s['title']):m['required_title']=s['title']
            m['dmhy_keyword']=s['title']
            url='https://share.dmhy.org/topics/rss/rss.xml?'+urlencode({'keyword':s['title']})
            items=parse_rss(engine.sources.web.fetch(url).content)
            source={'title':'动漫花园：'+s['title'],'url':url}
        suitable=[];all_eps=[]
        excluded_numbering=0
        for i in items:
            try:
                eps=validate_title(i['title'],m,engine.settings())
                if s['total'] and max(eps)>s['total']:
                    excluded_numbering+=1;continue
                all_eps+=eps;suitable.append(i)
            except Rejected:continue
        # Add release-title aliases as visible suggestions only; user still confirms them before saving.
        import re
        for i in suitable[:8]:
            base=re.sub(r'^(?:\[[^\]]+\]\s*)+','',i['title'])
            base=re.split(r'\s-\s*\d|\[\d|第\s*\d+\s*[话話集]',base)[0]
            for alias in base.split('/'):
                alias=alias.strip()
                if 3<=len(alias)<=150 and alias not in m['aliases']:m['aliases'].append(alias)
        warning=''
        if not suitable and excluded_numbering:
            warning='发现超出本季集数的连续编号，需要核对偏移；未自动启用下载。'
        elif all_eps and min(all_eps)>1:warning='来源没有发现第1集；请核对是否使用连续编号，或历史资源是否缺失。'
        if not items:warning='尚未发布资源。可先保存想看，发布后再核对来源与版本。'
        return {'mapping':m,'preview':[x['title'] for x in suitable[:6]],'warning':warning,
                'source':source,'inventory_count':len(engine.inventory_matches(sid,m)),
                'excluded_numbering':excluded_numbering}
    @app.post('/api/shows/{sid}/inventory')
    def inventory(sid:int,payload:dict):
        show(sid);return engine.inventory_matches(sid,{'aliases':payload.get('aliases',[])})
    @app.post('/api/shows/{sid}/mapping')
    def mapping(sid:int,p:dict):
        s=show(sid);m={k:p[k] for k in ('aliases','season','offset','airing_offset','start','end','mikan_id','dmhy_keyword','groups','resolution','subtitle','confirmed','inventory_checked','require_season','required_title') if k in p}
        old_mapping=json.loads(s['mapping'])
        if old_mapping.get('require_season'):m['require_season']=True
        if old_mapping.get('required_title'):m['required_title']=old_mapping['required_title']
        if 'airing_offset' not in m and 'airing_offset' in old_mapping:m['airing_offset']=old_mapping['airing_offset']
        if not isinstance(m.get('aliases'),list) or not m['aliases'] or len(m['aliases'])>20 or any(not isinstance(a,str) or len(a)<2 or len(a)>200 for a in m['aliases']):raise ValueError('至少填写一个明确作品别名')
        for k,default,lo,hi in [('season',1,1,99),('offset',0,-999,999),('airing_offset',0,-999,999),('start',1,1,999),('end',s['total'] or 999,1,999)]:
            v=m.get(k,default)
            if not isinstance(v,int) or not lo<=v<=hi:raise ValueError(k+'数值超界')
            m[k]=v
        if m['end']<m['start']:raise ValueError('结束集不能小于开始集')
        if m.get('resolution','1080') not in ('1080','720','2160',''):raise ValueError('分辨率无效')
        if m.get('subtitle','any_zh') not in ('any_zh','chs','cht'):raise ValueError('字幕偏好无效')
        if not isinstance(m.get('groups',[]),list) or any(not isinstance(g,str) or len(g)>80 for g in m.get('groups',[])):raise ValueError('字幕组格式无效')
        if m.get('confirmed') is not True or m.get('inventory_checked') is not True:raise ValueError('请先核对作品映射和已有文件')
        mid=str(m.get('mikan_id','')).strip()
        if mid and not mid.isdigit():raise ValueError('蜜柑ID必须为数字')
        if not mid and not str(m.get('dmhy_keyword','')).strip():raise ValueError('至少配置一个来源')
        if mid:engine.catalog.mikan_mapping(mid,sid)
        with db.gate:
            if db.one('SELECT 1 FROM tasks WHERE show_id=?',(sid,)) and json.loads(s['mapping'])!=m:raise ValueError('已有下载任务，请保留映射；改变季集需核查台账')
            configs=[]
            if mid:configs.append(('mikan',f'https://mikanani.me/RSS/Bangumi?bangumiId={mid}',f'https://mikanani.me/Home/Bangumi/{mid}'))
            kw=str(m.get('dmhy_keyword','')).strip()
            if kw:configs.append(('dmhy','https://share.dmhy.org/topics/rss/rss.xml?'+urlencode({'keyword':kw}),'https://share.dmhy.org/topics/list?'+urlencode({'keyword':kw})))
            with db.connect() as con:
                con.execute('UPDATE shows SET mapping=? WHERE id=?',(json.dumps(m,ensure_ascii=False),sid))
                con.execute('UPDATE sources SET enabled=0 WHERE show_id=?',(sid,))
                for kind,url,archive in configs:
                    owned=con.execute('SELECT show_id FROM sources WHERE url=?',(url,)).fetchone()
                    if owned and owned['show_id']!=sid:raise ValueError('此订阅源已绑定另一作品，不允许双重接管')
                    con.execute('INSERT INTO sources(show_id,kind,url,archive_url) VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET enabled=1',(sid,kind,url,archive))
                con.execute("UPDATE candidates SET status='discovered',reason='' WHERE show_id=? AND status IN ('rejected','review','covered')",(sid,))
        db.event('show:'+str(sid),'保存了经用户核对的作品/季集映射');return {'ok':True}
    @app.post('/api/shows/{sid}/authorize')
    def authorize(sid:int,p:dict):
        s=show(sid);enable=p.get('enabled') is True
        with db.gate:
            if enable:
                m=json.loads(s['mapping'])
                if not m.get('confirmed') or not m.get('inventory_checked'):raise ValueError('请先保存已核验的下载设置')
                if not s['selected'] or s['state'] not in ('trial','watching'):raise ValueError('请先选择试看或在追')
                if not db.get('auto_quarter_enabled',False) and not s['authorized'] and db.one('SELECT count(*) n FROM shows WHERE authorized=1')['n']>=3:raise ValueError('第一版真实试运行最多3部，请先关闭其他作品自动下载')
                # Legacy default-profile RSS has a catch-all personal subscription. Never run concurrently.
                engine.check_legacy_owner(sid)
            db.execute('UPDATE shows SET authorized=? WHERE id=?',(int(enable),sid))
            db.event('show:'+str(sid),'用户'+('开启' if enable else '关闭')+'自动下载')
            engine.wake.set()
        return {'ok':True}
    @app.post('/api/shows/{sid}/link-local')
    def link_local(sid:int,p:dict):
        show(sid);ep=int(p['episode']);path=str(p['path'])
        inventory=json.loads((db.path.parent/'inventory.json').read_text(encoding='utf-8'))
        f=next((x for x in inventory if x['path']==path),None)
        if not 1<=ep<=999 or not f or not Path(path).is_file() or Path(path).stat().st_size!=f['size']:raise ValueError('文件不在已核实的只读盘点内')
        with db.gate:
            if db.one('SELECT 1 FROM episodes WHERE show_id=? AND episode=?',(sid,ep)):raise ValueError('本集已有文件或下载记录')
            db.execute('INSERT INTO episodes(show_id,episode,path,status,progress,size) VALUES(?,?,?,?,?,?)',(sid,ep,path,'complete',1,f['size']))
        db.event('show:'+str(sid),f'用户只读关联已有第{ep}集');return {'ok':True}
    @app.post('/api/shows/{sid}/watch')
    def watch(sid:int,p:dict):
        show(sid);ep=int(p['episode'])
        if not 1<=ep<=999:raise ValueError('集数超界')
        db.execute('INSERT INTO watches(show_id,episode,finished,method,updated) VALUES(?,?,?,?,?) ON CONFLICT(show_id,episode) DO UPDATE SET finished=excluded.finished,method=excluded.method,updated=excluded.updated',(sid,ep,int(p.get('finished') is True),'manual',time.time()))
        db.execute('UPDATE shows SET watched=(SELECT count(*) FROM watches WHERE show_id=? AND finished=1) WHERE id=?',(sid,sid))
        return {'ok':True}
    @app.post('/api/player/probe')
    def probe():
        port=db.get('dandan_port',26666)
        try:
            r=requests.get(f'http://127.0.0.1:{port}/api/v1/welcome',timeout=3);r.raise_for_status();info=r.json()
            result={'ok':True,'version':info.get('version'),'token_required':info.get('tokenRequired'),'time':time.time(),'auto_watch':False}
            if not info.get('tokenRequired'):
                current=requests.get(f'http://127.0.0.1:{port}/api/v1/current/video',timeout=3)
                result['current_endpoint']=current.status_code
                if current.ok:result['progress_fields']=sorted(set(current.json())&{'Duration','Position','Playing','EpisodeId'})
        except Exception:result={'ok':False,'time':time.time(),'error':f'弹弹play本机接口 {port} 未启用或不可达；可直接播放、手动标记观看','auto_watch':False}
        db.set('player_health',result);return result
    @app.post('/api/player/open')
    def play(p:dict):
        exe=Path(db.get('dandan_exe'))
        if exe.name.lower()!='dandanplay.exe' or not exe.is_file():raise ValueError('弹弹play安装路径无效')
        args=[str(exe)]
        if p.get('show_id') is not None:
            e=db.one('SELECT * FROM episodes WHERE show_id=? AND episode=?',(int(p['show_id']),int(p['episode'])))
            if not e or e['status']!='complete' or not Path(e['path']).is_file() or Path(e['path']).stat().st_size!=e['size']:raise ValueError('本集尚未确认下载完成')
            args.append(e['path'])
        subprocess.Popen(args,cwd=str(exe.parent),shell=False)
        db.event('player','已请求弹弹play打开；观看状态未改变');return {'ok':True,'message':'已打开弹弹play；观看进度请手动确认'}
    @app.post('/api/check')
    def check():
        sequence=engine.request_check()
        return {'ok':True,'sequence':sequence,'message':'已请求立即检查及历史补查；运行中的检查结束后接续，不重复提交。',
                'blocker':downloader_status(db)['blocker']}
    @app.post('/api/settings')
    def settings(p:dict):
        for k,v in p.items():
            if k=='poll_seconds' and isinstance(v,int) and 60<=v<=86400:db.set(k,v)
            elif k=='resolution' and v in ('1080','720','2160',''):db.set(k,v)
            elif k=='subtitle' and v in ('any_zh','chs','cht'):db.set(k,v)
            elif k=='groups' and isinstance(v,list) and len(v)<=30 and all(isinstance(x,str) and len(x)<=80 for x in v):db.set(k,v)
            elif k=='dandan_port' and isinstance(v,int) and 1<=v<=65535:db.set(k,v)
            else:raise ValueError('设置值无效：'+k)
        db.execute("UPDATE candidates SET status='discovered' WHERE status='rejected'")
        engine.wake.set();return {'ok':True}
    from .season import install_season
    install_season(app,db,engine,shows,prepare,mapping)
    from .webplayer import register_webplayer
    register_webplayer(app,db)
    from .cleanup import register_cleanup
    register_cleanup(app,db,engine)
    from .thunder_bridge import register_thunder
    register_thunder(app,engine)
    from .transition import register_transition
    register_transition(app,db,engine)
    from .dandan_history import register_history
    register_history(app,db)
    return app

app=create_app(start_worker=os.environ.get('ANIMESERVICE_START_WORKER','1')!='0')
