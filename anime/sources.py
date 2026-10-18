import datetime,hashlib,json,re,time,calendar
from urllib.parse import urlencode,urljoin,urlparse
import feedparser
from bs4 import BeautifulSoup
from .net import Web,NetworkError,RequestDeferred,validate_url,safe_error
from .native_indexes import KINDS,entry_metadata,eligible,search_mapping,checked_download_url,verify_hash
from .releases import validate_title,Rejected
from .fidelity import fidelity_blocker

def secure_public_url(url):
    p=urlparse(url)
    if p.scheme=='http' and p.hostname in ('share.dmhy.org','dl.dmhy.org','mikanani.me','mikanime.tv'):
        return 'https:'+url[5:]
    return url

# Alternate hosts of one site share health, backoff and request spacing. The stored source
# URL keeps the canonical host; only the request is rewritten to the preferred alias.
SITE_HOSTS={'tokyotosho':('www.tokyotosho.info','www.tokyotosho.se','www.tokyo-tosho.net')}
HOST_SITE={host:site for site,hosts in SITE_HOSTS.items() for host in hosts}
HOST_SITE.update({'tokyotosho.info':'tokyotosho','tokyotosho.se':'tokyotosho','tokyo-tosho.net':'tokyotosho',
                  'bt4g.org':'bt4g','bt4gprx.com':'bt4g','1337x.to':'1337x','www.1337x.to':'1337x'})  # one site, one health record
SITE_MIN_INTERVAL={'tokyotosho':10,'nyaa.si':5}  # seconds between requests to one site; other sites are not spaced
EVENT_FAILURE_MARKS=(1,5,20,100)
# TokyoTosho answers a search without matches with this fragment (served as text/html): a legitimate empty feed.
EMPTY_RESULT=re.compile(rb'^\s*</channel>\s*</rss>\s*$')
CHALLENGE_MARKERS=(b'cf-chl',b'cf_chl',b'challenge-platform',b'just a moment',b'attention required',b'access denied')


def site_of(url):
    host=urlparse(url).hostname or ''
    return HOST_SITE.get(host,host)


def site_backoff_seconds(failures):
    return min(3600,60*2**min(max(failures,1)-1,6))


def source_backoff_seconds(failures):
    """One feed's own failures: a minute doubling to six hours, reset by any success."""
    return min(21600,60*2**min(max(failures,1)-1,9))


def bump(state,name):
    stats=state.setdefault('stats',{});stats[name]=stats.get(name,0)+1


def preferred_host(url,state):
    hosts=SITE_HOSTS.get(site_of(url))
    if not hosts:return None
    return state.get('host') if state.get('host') in hosts else hosts[0]


def request_url(url,state):
    validate_url(url)  # Do not erase credentials/ports while replacing netloc.
    host=preferred_host(url,state)
    return urlparse(url)._replace(netloc=host).geturl() if host else url


def next_host(url,state):
    hosts=SITE_HOSTS.get(site_of(url))
    if not hosts:return state.get('host')
    return hosts[(hosts.index(preferred_host(url,state))+1)%len(hosts)]


def looks_like_page(response):
    """A 200 that carries HTML instead of a feed: a challenge, block or error page."""
    try:content_type=str(response.headers.get('Content-Type',''))
    except Exception:content_type=''
    head=bytes(response.content[:4096]).lower()
    if b'<rss' in head or b'<feed' in head or b'<?xml' in head:return False
    return 'html' in content_type.lower() or b'<html' in head


def challenge_page(response):
    head=bytes(response.content[:8192]).lower()
    return looks_like_page(response) and any(marker in head for marker in CHALLENGE_MARKERS)


def feed_items(site,response,kind):
    """Classify one answer: empty result, challenge (site-wide), other page (this feed), or a feed to parse."""
    body=bytes(response.content)
    if site=='tokyotosho' and kind=='tokyotosho' and EMPTY_RESULT.fullmatch(body):return []
    if not body.strip():raise NetworkError(site+' 返回空正文，尚不能确认无结果',200,site_wide=False)
    if challenge_page(response):raise NetworkError(site+' 返回验证页面',200,site_wide=True)
    if looks_like_page(response):raise NetworkError(site+' 返回HTML页面而非RSS',200,site_wide=False)
    return parse_rss(body,kind)


def update_health(db,site,change):
    """One short SQLite transaction; no network or sleep while holding it."""
    key='site_health_'+site
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        state=json.loads(row[0]) if row else {}
        if not state.get('alias_health_merged'):
            # A family key must inherit restrictions held by its old host keys.
            for host,family in HOST_SITE.items():
                if family!=site or host==site:continue
                prior=con.execute('SELECT value FROM kv WHERE key=?',('site_health_'+host,)).fetchone()
                if not prior:continue
                old=json.loads(prior[0])
                if old.get('last_error_at',0)>state.get('last_error_at',0):state['error']=old.get('error','')
                for field in ('next_attempt','last_request','last_error_at','failures'):
                    state[field]=max(state.get(field,0),old.get(field,0))
                for field,count in old.get('stats',{}).items():state.setdefault('stats',{})[field]=state.setdefault('stats',{}).get(field,0)+count
            state['alias_health_merged']=True
        result=change(state)
        con.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,json.dumps(state,ensure_ascii=False)))
    return result


def reserve_request(db,url):
    validate_url(url);site=site_of(url);now=time.time()
    spacing=db.get('site_min_interval_seconds',{}).get(site,SITE_MIN_INTERVAL.get(site,0))
    def reserve(state):
        until=state.get('next_attempt',0)
        if until>now:
            bump(state,'deferred');return until
        if now-state.get('last_request',0)<spacing:
            state['deferred']=state.get('deferred',0)+1;bump(state,'deferred')
            return state['last_request']+spacing*state['deferred']
        state.update(last_request=now,deferred=0);bump(state,'http_attempts')
        return None
    until=update_health(db,site,reserve)
    if until is not None:raise RequestDeferred(until,url)


def fail_site(db,url,error):
    if not isinstance(error,NetworkError) or not error.site_wide or getattr(error,'health_recorded',False):return
    now=time.time()
    def failed(state):
        failures=state.get('failures',0)+1
        # Access restrictions cool the whole family without rotating to evade them.
        rotate=error.status is None or 500<=error.status<600
        state.update(failures=failures,next_attempt=max(state.get('next_attempt',0),now+max(site_backoff_seconds(failures),error.retry_after)),
                     error=safe_error(error),host=next_host(url,state) if rotate else preferred_host(url,state),last_error_at=now)
    update_health(db,site_of(url),failed);error.health_recorded=True


def record_poll(db,site,outcome,started):
    def finished(state):
        bump(state,'polls');bump(state,outcome)
        if outcome in ('success','empty') and state.get('last_error_at',0)<=started:
            state.update(failures=0,next_attempt=0,error='')
    update_health(db,site,finished)

class Catalog:
    def __init__(self,store,web=None):self.db=store;self.web=web or Web()
    def sync_quarter(self,year,month):
        if month not in (1,4,7,10) or not 2000<=year<=2100:raise ValueError('无效季度')
        quarter=f'{year}-{month:02}';end=f'{year+1}-01-01' if month==10 else f'{year}-{month+3:02}-01'
        offset=0;count=0;seen=set();expected=0;pages=0
        self.db.set('catalog_job',{'status':'running','quarter':quarter,'count':0})
        try:
            while True:
                data=self.web.fetch('https://api.bgm.tv/v0/search/subjects?'+urlencode({'limit':20,'offset':offset}),method='POST',json={
                    'keyword':'','sort':'heat','filter':{'type':[2],'air_date':['>='+quarter+'-01','<'+end],'nsfw':False}}).json()
                items=data.get('data');total=data.get('total');returned_offset=data.get('offset',offset)
                if not isinstance(items,list) or type(total) is not int or total<0 or returned_offset!=offset:
                    raise NetworkError('季度分页响应无效，目录尚不完整')
                expected=max(expected,total);pages+=1
                ids=[s['id'] for s in items]
                if items and not set(ids)-seen:
                    raise NetworkError('季度分页未前进，目录尚不完整')
                for s in items:self.db.upsert_subject(s,quarter)
                seen.update(ids);count=len(seen);self.db.set('catalog_job',{'status':'running','quarter':quarter,'count':count})
                # Search may cap the requested page size. Advance by actual rows,
                # never by our requested limit, or whole ranges are silently skipped.
                offset+=len(items)
                if offset>=expected:
                    if count<expected:raise NetworkError('季度分页存在重复或遗漏，目录尚不完整')
                    break
                if not items:raise NetworkError('季度分页提前结束，目录尚不完整')
                if offset>=2000:raise NetworkError('季度分页达到保护上限，目录可能不完整')
                time.sleep(0.3)
            self.db.set('catalog_'+quarter,{'success':time.time(),'count':count,'total':expected,'pages':pages,'complete':True,'error':''})
            # Search is a compact projection: episode totals/aliases require subject details.
            for row in self.db.rows('SELECT id,metadata FROM shows WHERE quarter=? OR id IN (SELECT show_id FROM quarter_members WHERE quarter=?)',(quarter,quarter)):
                if json.loads(row['metadata']).get('platform') not in ('TV','WEB'):continue
                if time.time()-self.db.get('subject_details_'+str(row['id']),0)<21600:continue
                try:self.subject(row['id'])
                except Exception as ex:self.db.event('catalog:'+str(row['id']),safe_error(ex),'warning')
            self.db.set('catalog_job',{'status':'done','quarter':quarter,'count':count})
            self.db.event('catalog',f'{quarter} 季度缓存更新：{count}部，不改变追番状态')
            return count
        except Exception as e:
            previous=self.db.get('catalog_'+quarter,{})
            self.db.set('catalog_'+quarter,{**previous,'complete':False,'error':safe_error(e),'attempt':time.time()})
            self.db.set('catalog_job',{'status':'error','quarter':quarter,'error':safe_error(e)})
            raise
    def subject(self,sid):
        s=self.web.fetch(f'https://api.bgm.tv/v0/subjects/{sid}').json();self.db.upsert_subject(s)
        self.db.set('subject_details_'+str(sid),time.time())
        return s
    def details(self,sid):
        s=self.subject(sid)
        offset=0;items=[];seen=set();expected=0
        while True:
            r=self.web.fetch('https://api.bgm.tv/v0/episodes?'+urlencode({'subject_id':sid,'type':0,'limit':100,'offset':offset})).json()
            rows=r.get('data');total=r.get('total')
            if not isinstance(rows,list) or type(total) is not int or total<0 or r.get('offset',offset)!=offset:
                raise NetworkError('Bangumi章节分页响应无效，保留原分集资料')
            expected=max(expected,total)
            ids=[e.get('id') for e in rows]
            if any(type(i) is not int or i<=0 for i in ids) or len(set(ids))!=len(ids) or set(ids)&seen:
                raise NetworkError('Bangumi章节分页重复或缺少ID，保留原分集资料')
            seen.update(ids);items+=rows;offset+=len(rows)
            if offset>=expected:break
            if not rows or offset>10000:raise NetworkError('Bangumi章节分页不完整，保留原分集资料')
        self.db.set('bangumi_episodes_'+str(sid),{'time':time.time(),'items':items})
        with self.db.connect() as c:
            for e in items:
                n=e.get('sort',0)
                if float(n).is_integer() and n>0:c.execute('INSERT OR REPLACE INTO airings VALUES(?,?,?,?)',(sid,int(n),e.get('airdate',''),e.get('name_cn') or e.get('name','')))
        # Sequels keep the franchise-wide `sort` (S3E1 = 49); Bangumi's `ep` is the in-season number.
        shifts={int(e['sort'])-int(e['ep']) for e in items if (e.get('sort') or 0)>0 and (e.get('ep') or 0)>0 and float(e['sort']).is_integer() and float(e['ep']).is_integer()}
        self.db.set('airing_sort_offset_'+str(sid),shifts.pop() if len(shifts)==1 else 0)
        self.db.set('details_'+str(sid),time.time())
        self.db.execute("UPDATE bangumi_sync_queue SET revision=revision+1,state='pending',error='',next_attempt=0 WHERE show_id=? AND state='blocked'",(sid,))
        return s
    def mikan_matches(self,sid):
        show=self.db.show(sid);seen=set();out=[]
        # Search index punctuation/sequel suffixes differ from subject metadata.
        # Broader discovery never supplies identity: mikan_mapping still proves the exact subject ID.
        terms=list(dict.fromkeys([show['title'],show['original']]))
        for title in terms[:]:
            stem=re.sub(r'\s*(?:第[\d一二三四五六七八九十]+(?:季|期|部分|クール)|Season\s*\d+)\s*$','',title,flags=re.I).strip()
            if len(stem)>=3 and stem!=title:terms.append(stem)
        for term in dict.fromkeys(terms):
            if not term:continue
            html=self.web.fetch('https://mikanani.me/Home/Search?'+urlencode({'searchstr':term})).text
            soup=BeautifulSoup(html,'html.parser')
            for a in soup.select('a[href*="/Home/Bangumi/"]'):
                m=re.search(r'/Home/Bangumi/(\d+)',a.get('href',''))
                if not m or m[1] in seen:continue
                seen.add(m[1]);out.append({'id':m[1],'title':a.get_text(' ',strip=True) or a.get('title',''),'url':'https://mikanani.me/Home/Bangumi/'+m[1]})
        return out[:30]
    def mikan_mapping(self,mid,sid):
        html=self.web.fetch(f'https://mikanani.me/Home/Bangumi/{mid}').text;soup=BeautifulSoup(html,'html.parser')
        ids=[]
        for a in soup.select('a[href]'):
            m=re.search(r'(?:bgm.tv|bangumi.tv|chii.in)/subject/(\d+)',a['href'])
            if m:ids.append(int(m[1]))
        if int(sid) not in ids:raise ValueError('蜜柑页面未能核实相同 Bangumi ID，请核对作品/季')
        titles=[soup.title.get_text(strip=True)] if soup.title else []
        return {'verified':True,'bangumi_id':sid,'mikan_id':mid,'titles':titles}

def parse_rss(content,kind=None):
    feed=feedparser.parse(content)
    if not feed.get('version') or (feed.bozo and not feed.entries):raise NetworkError('RSS格式无效或返回了验证页面',200,site_wide=False)
    out=[]
    for e in feed.entries:
        enclosures=e.get('enclosures',[]);link=secure_public_url(e.get('link',''))
        url=secure_public_url(next((x.get('href') for x in enclosures if x.get('href')),None) or link)
        detail=secure_public_url(e.get('id','')) if kind in KINDS else link
        if kind in KINDS:
            try:
                if kind=='bt4g':
                    from .general_indexes import public_magnet
                    url=public_magnet(url)
                else:url=checked_download_url(url)
                detail=checked_download_url(detail)
            except Rejected:continue
        published=e.get('published_parsed') or e.get('updated_parsed')
        if url:out.append({'title':e.get('title',''),'url':url,'detail':detail,
                          'published':calendar.timegm(published) if published else None,
                          'source_metadata':entry_metadata(e,kind) if kind in KINDS else {}})
    return out

class Sources:
    def __init__(self,store,web=None):self.db=store;self.web=web or Web()
    def fetch(self,url,**kwargs):
        """Apply shared budgets to RSS, details, torrents and every redirect/retry."""
        validate_url(url);site=site_of(url);now=time.time();key=hashlib.sha256(url.encode()).hexdigest()
        state=self.db.get('site_health_'+site,{})
        pending=state.get('redirects',{}).get(key,{})
        if pending.get('expires',0)<=now:pending={}
        target=validate_url(pending.get('target',url));visited=list(pending.get('visited',[]))
        if pending.get('not_before',0)>now:raise RequestDeferred(pending['not_before'],target)
        reserve_request(self.db,target)
        first=True
        def before_request(next_url):
            nonlocal first
            if first:first=False
            else:reserve_request(self.db,next_url)
        spacing=self.db.get('site_min_interval_seconds',{}).get(site,SITE_MIN_INTERVAL.get(site,0))
        kwargs['retries']=1 if spacing else kwargs.get('retries',3)
        def clear_pending(state):
            state.get('redirects',{}).pop(key,None)
        try:
            result=self.web.fetch(target,before_request=before_request,**kwargs)
            if pending:update_health(self.db,site,clear_pending)
            return result
        except RequestDeferred as deferred:
            validate_url(deferred.target)
            if deferred.target!=target:
                if deferred.target in visited or len(visited)>=3:
                    update_health(self.db,site,clear_pending)
                    raise NetworkError('重定向次数过多',site_wide=False) from None
                visited.append(target)
                def remember_redirect(state):
                    redirects={k:v for k,v in state.get('redirects',{}).items() if v.get('expires',0)>time.time()}
                    if len(redirects)>=256:redirects.pop(next(iter(redirects)))
                    redirects[key]={'target':deferred.target,'visited':visited,'not_before':deferred.until,
                                    'expires':max(time.time()+3600,deferred.until+60)}
                    state['redirects']=redirects
                update_health(self.db,site,remember_redirect)
            raise
        except NetworkError as error:
            error.health_recorded=False  # One new fetch, even if a test/adapter reuses an exception instance.
            fail_site(self.db,target,error);raise
    def remember(self,source,items):
        if source['kind'] in KINDS:
            if not eligible(self.db,source['show_id'],source['kind']):return []
            mapping=search_mapping(self.db.mapping(source['show_id']),source['kind']);checked=[]
            for e in items:
                try:
                    eps=validate_title(e['title'],mapping,{},False)
                    if len(eps)!=1 or not self.db.allowed(source['show_id'],eps[0]):continue
                    if source['kind']=='bt4g':
                        from .general_indexes import corroborate_mode
                        corroborate_mode(self.db,source['show_id'],e)
                    if e['source_metadata']['media_mode'] not in ('raw','soft'):continue
                    if source['kind'] in ('bt4g','1337x'):
                        from .general_indexes import publisher_blocked
                        if publisher_blocked(self.db,e['title']):continue
                    if fidelity_blocker(self.db,e['title']):continue
                except (Rejected,ValueError):continue
                checked.append(e)
            items=checked
        with self.db.gate,self.db.connect() as c:
            if source['kind'] in KINDS and not eligible(self.db,source['show_id'],source['kind']):return []
            for e in items:
                c.execute('''INSERT INTO candidates(show_id,source_id,title,url,detail,discovered,published,source_metadata) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id,url) DO UPDATE SET published=coalesce(candidates.published,excluded.published),
                    source_metadata=excluded.source_metadata''',
                    (source['show_id'],source['id'],e['title'],e['url'],e.get('detail',''),time.time(),e.get('published'),json.dumps(e.get('source_metadata',{}))))
        return items
    def rss(self,source):
        if source['kind'] in KINDS and not eligible(self.db,source['show_id'],source['kind']):return False
        from .general_indexes import GENERAL_KINDS,site_backoff,search_1337x,record_failure
        if source['kind'] in GENERAL_KINDS and site_backoff(self.db,source):return False
        site=site_of(source['url']);key='site_health_'+site;state=self.db.get(key,{});now=time.time()
        try:
            if source['kind']=='1337x':items=search_1337x(self.db,self,source)
            else:items=feed_items(site,self.fetch(request_url(source['url'],state)),source['kind'])
            outcome='empty' if not items else 'success'
            items=self.remember(source,items)
            interval=self.db.get('poll_seconds',180)
            if self.db.review_wanted(source['show_id']):interval=min(interval,180)
            if source['kind'] in GENERAL_KINDS:interval=3600
            self.db.execute('UPDATE sources SET last_attempt=?,last_success=?,next_attempt=?,failures=0,error=?,latest=? WHERE id=?',
                (now,time.time(),time.time()+interval,'',items[0]['title'] if items else '本次RSS无条目',source['id']))
            record_poll(self.db,site,outcome,now)
            return True
        except RequestDeferred as deferred:
            self.db.execute('UPDATE sources SET next_attempt=? WHERE id=?',(deferred.until,source['id']));return False
        except Exception as e:
            if source['kind'] in GENERAL_KINDS:record_failure(self.db,source,e)
            n=source['failures']+1;err=safe_error(e);retry=time.time()+source_backoff_seconds(n)
            fail_site(self.db,source['url'],e)
            site_failure=isinstance(e,NetworkError) and e.site_wide
            record_poll(self.db,site,'failed_site' if site_failure else 'failed_source',now)
            if site_failure:retry=max(retry,self.db.get(key,{}).get('next_attempt',0))
            self.db.execute('UPDATE sources SET last_attempt=?,failures=?,next_attempt=?,error=? WHERE id=?',(now,n,retry,err,source['id']))
            if err!=source['error'] or n in EVENT_FAILURE_MARKS or n%100==0:self.db.event('source:'+str(source['id']),err,'error')
            return False
    def archive(self,source):
        if not source['archive_url']:return
        try:
            url=source['archive_url'];items=[];visited=set()
            for _ in range(20):
                if url in visited:break
                visited.add(url);soup=BeautifulSoup(self.fetch(url).text,'html.parser')
                for tr in soup.select('tr'):
                    a=tr.select_one('a[href*="/Home/Episode/"],a[href*="/topics/view/"]')
                    if not a:continue
                    torrent=tr.select_one('a[href$=".torrent"],a[href^="magnet:"]')
                    detail=urljoin(url,a['href'])
                    items.append({'title':a.get_text(' ',strip=True),'url':urljoin(url,torrent['href']) if torrent else detail,'detail':detail})
                nxt=next((a for a in soup.select('a[href]') if a.get_text(strip=True) in ('下一页','下一頁','Next','›','»')),None)
                if not nxt:break
                url=validate_url(urljoin(url,nxt['href']));time.sleep(0.3)
            else:raise NetworkError('历史分页达到20页上限，补查范围尚不完整')
            if not items:raise NetworkError('历史页未解析到条目，可能尚未发布或页面结构变化')
            self.remember(source,items)
            self.db.execute('UPDATE sources SET archive_success=?,archive_error=? WHERE id=?',(time.time(),'',source['id']))
        except RequestDeferred:return
        except Exception as e:
            err=safe_error(e);self.db.execute('UPDATE sources SET archive_error=? WHERE id=?',(err,source['id']))
            if err!=source['archive_error']:self.db.event('archive:'+str(source['id']),err,'warning')  # a continuing condition is one warning
    def torrent(self,candidate):
        source=self.db.one('SELECT kind FROM sources WHERE id=?',(candidate.get('source_id'),))
        kind=source['kind'] if source else None
        if kind in KINDS:
            if not eligible(self.db,candidate['show_id'],kind):raise Rejected('当前作品不在已授权的字幕片源范围')
            eps=validate_title(candidate['title'],search_mapping(self.db.mapping(candidate['show_id']),kind),{},False)
            if len(eps)!=1 or not self.db.allowed(candidate['show_id'],eps[0]):raise Rejected('索引候选不在已核验的单集范围')
        if kind in ('bt4g','1337x'):
            from .general_indexes import resolve_torrent
            return resolve_torrent(self.db,self,candidate)
        url=secure_public_url(candidate['url'])
        if url.startswith('magnet:'):
            detail=secure_public_url(candidate['detail'])
            if not detail.startswith('https:'):raise ValueError('仅磁力链接，缺少可核验种子；等待人工来源补充')
            url=detail
        r=self.fetch(url)
        if r.content.startswith(b'd'):return verify_hash(candidate,r.content)
        soup=BeautifulSoup(r.text,'html.parser')
        for a in soup.select('a[href]'):
            href=secure_public_url(urljoin(url,a['href']))
            if '.torrent' in href and urlparse(href).hostname in ('mikanani.me','mikanime.tv','dl.dmhy.org','share.dmhy.org','nyaa.si'):
                return verify_hash(candidate,self.fetch(href).content)
        raise ValueError('来源没有可核验的torrent文件，未提交下载')
