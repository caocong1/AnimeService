import datetime,json,re,time
from urllib.parse import urlencode,urljoin,urlparse
import feedparser
from bs4 import BeautifulSoup
from .net import Web,NetworkError,validate_url,safe_error

def secure_public_url(url):
    p=urlparse(url)
    if p.scheme=='http' and p.hostname in ('share.dmhy.org','dl.dmhy.org','mikanani.me','mikanime.tv'):
        return 'https:'+url[5:]
    return url

class Catalog:
    def __init__(self,store,web=None):self.db=store;self.web=web or Web()
    def sync_quarter(self,year,month):
        if month not in (1,4,7,10) or not 2000<=year<=2100:raise ValueError('无效季度')
        quarter=f'{year}-{month:02}';end=f'{year+1}-01-01' if month==10 else f'{year}-{month+3:02}-01'
        offset=0;count=0
        self.db.set('catalog_job',{'status':'running','quarter':quarter,'count':0})
        try:
            while True:
                data=self.web.fetch('https://api.bgm.tv/v0/search/subjects?'+urlencode({'limit':50,'offset':offset}),method='POST',json={
                    'keyword':'','sort':'heat','filter':{'type':[2],'air_date':['>='+quarter+'-01','<'+end],'nsfw':False}}).json()
                items=data.get('data',[])
                for s in items:self.db.upsert_subject(s,quarter)
                count+=len(items);self.db.set('catalog_job',{'status':'running','quarter':quarter,'count':count})
                offset+=50
                if not items or offset>=data.get('total',0):break
                if offset>=2000:raise NetworkError('季度分页达到保护上限，目录可能不完整')
                time.sleep(0.3)
            self.db.set('catalog_'+quarter,{'success':time.time(),'count':count,'error':''})
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
            self.db.set('catalog_'+quarter,{**previous,'error':safe_error(e),'attempt':time.time()})
            self.db.set('catalog_job',{'status':'error','quarter':quarter,'error':safe_error(e)})
            raise
    def subject(self,sid):
        s=self.web.fetch(f'https://api.bgm.tv/v0/subjects/{sid}').json();self.db.upsert_subject(s)
        self.db.set('subject_details_'+str(sid),time.time())
        return s
    def details(self,sid):
        s=self.subject(sid)
        offset=0;items=[]
        while True:
            r=self.web.fetch('https://api.bgm.tv/v0/episodes?'+urlencode({'subject_id':sid,'type':0,'limit':100,'offset':offset})).json()
            items+=r.get('data',[]);offset+=100
            if offset>=r.get('total',0) or not r.get('data'):break
        with self.db.connect() as c:
            for e in items:
                n=e.get('sort',0)
                if float(n).is_integer() and n>0:c.execute('INSERT OR REPLACE INTO airings VALUES(?,?,?,?)',(sid,int(n),e.get('airdate',''),e.get('name_cn') or e.get('name','')))
        # Sequels keep the franchise-wide `sort` (S3E1 = 49); Bangumi's `ep` is the in-season number.
        shifts={int(e['sort'])-int(e['ep']) for e in items if (e.get('sort') or 0)>0 and (e.get('ep') or 0)>0 and float(e['sort']).is_integer() and float(e['ep']).is_integer()}
        self.db.set('airing_sort_offset_'+str(sid),shifts.pop() if len(shifts)==1 else 0)
        self.db.set('details_'+str(sid),time.time())
        return s
    def mikan_matches(self,sid):
        show=self.db.show(sid);seen=set();out=[]
        for term in dict.fromkeys([show['title'],show['original']]):
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

def parse_rss(content):
    feed=feedparser.parse(content)
    if not feed.get('version') or (feed.bozo and not feed.entries):raise NetworkError('RSS格式无效或返回了验证页面')
    out=[]
    for e in feed.entries:
        enclosures=e.get('enclosures',[]);link=secure_public_url(e.get('link',''))
        url=secure_public_url(next((x.get('href') for x in enclosures if x.get('href')),None) or link)
        if url:out.append({'title':e.get('title',''),'url':url,'detail':link})
    return out

class Sources:
    def __init__(self,store,web=None):self.db=store;self.web=web or Web()
    def remember(self,source,items):
        with self.db.connect() as c:
            for e in items:c.execute('INSERT OR IGNORE INTO candidates(show_id,source_id,title,url,detail,discovered) VALUES(?,?,?,?,?,?)',
                (source['show_id'],source['id'],e['title'],e['url'],e.get('detail',''),time.time()))
    def rss(self,source):
        now=time.time();self.db.execute('UPDATE sources SET last_attempt=? WHERE id=?',(now,source['id']))
        try:
            items=parse_rss(self.web.fetch(source['url']).content);self.remember(source,items)
            self.db.execute('UPDATE sources SET last_success=?,next_attempt=?,failures=0,error=?,latest=? WHERE id=?',
                (now,now+self.db.get('poll_seconds',180),'',items[0]['title'] if items else '本次RSS无条目',source['id']))
            return True
        except Exception as e:
            n=source['failures']+1;err=safe_error(e)
            self.db.execute('UPDATE sources SET failures=?,next_attempt=?,error=? WHERE id=?',(n,now+min(300,15*2**min(n,5)),err,source['id']))
            self.db.event('source:'+str(source['id']),err,'error');return False
    def archive(self,source):
        if not source['archive_url']:return
        try:
            url=source['archive_url'];items=[];visited=set()
            for _ in range(20):
                if url in visited:break
                visited.add(url);soup=BeautifulSoup(self.web.fetch(url).text,'html.parser')
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
        except Exception as e:
            err=safe_error(e);self.db.execute('UPDATE sources SET archive_error=? WHERE id=?',(err,source['id']))
            self.db.event('archive:'+str(source['id']),err,'warning')
    def torrent(self,candidate):
        url=secure_public_url(candidate['url'])
        if url.startswith('magnet:'):
            detail=secure_public_url(candidate['detail'])
            if not detail.startswith('https:'):raise ValueError('仅磁力链接，缺少可核验种子；等待人工来源补充')
            url=detail
        r=self.web.fetch(url)
        if r.content.startswith(b'd'):return r.content
        soup=BeautifulSoup(r.text,'html.parser')
        for a in soup.select('a[href]'):
            href=secure_public_url(urljoin(url,a['href']))
            if '.torrent' in href and urlparse(href).hostname in ('mikanani.me','mikanime.tv','dl.dmhy.org','share.dmhy.org'):
                return self.web.fetch(href).content
        raise ValueError('来源没有可核验的torrent文件，未提交下载')
