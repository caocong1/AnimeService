"""Supplemental public indexes for the existing streaming-anime cohort.

Platform evidence triggers discovery; it never establishes exclusive rights.
Magnet metadata must become a verified torrent BEFORE any downloader submission.
"""
import base64,hashlib,json,re,time
from urllib.parse import urlencode,quote,parse_qs,urlparse,urljoin
from bs4 import BeautifulSoup
from .native_indexes import eligible,search_terms,search_mapping,metadata,media_mode,verify_hash,checked_download_url
from .net import NetworkError,safe_error,validate_url
from .releases import validate_title,Rejected
from .fidelity import fidelity_blocker

GENERAL_KINDS={'bt4g','1337x'}
PLATFORM=re.compile(r'(?i)(?<![a-z])(?:Netflix|NF|AMZN|Amazon|Prime[ ._-]*Video|网飞|亞馬遜|亚马逊)(?![a-z])')


def publisher_blocked(db,title):
    # General sites frequently move the release group to an unbracketed suffix.
    return any(re.search(r'(?i)(?<![\w])'+re.escape(alias)+r'(?![\w])',title)
               for entry in db.get('source_publisher_blacklist',[]) for alias in entry.get('aliases',[]) if alias)


def platform_basis(db,sid):
    show=db.show(sid)
    if not show:return []
    box=json.loads(show['metadata']).get('infobox',[])
    basis=[{'kind':'catalog-platform','value':str(f.get('value'))[:500]} for f in box
           if f.get('key') in ('在线播放平台','網路播放平台','网络播放平台','播放平台','链接') and PLATFORM.search(str(f.get('value','')))]
    # An NF/AMZN release is a streaming hint, never proof of exclusivity.
    for c in db.rows('SELECT title,id FROM candidates WHERE show_id=? ORDER BY id DESC',(sid,)):
        if not PLATFORM.search(c['title']) or fidelity_blocker(db,c['title']):continue
        try:
            eps=validate_title(c['title'],db.mapping(sid),{},False)
            if len(eps)!=1 or not db.allowed(sid,eps[0]):continue
        except Rejected:continue
        basis.append({'kind':'release-platform-hint','candidate_id':c['id']});break
    return basis


def register_general_sources(db,sid):
    if not eligible(db,sid,'bt4g'):return 0
    count=0
    with db.gate,db.connect() as con:
        if not eligible(db,sid,'bt4g'):return 0
        for term in search_terms(db.show(sid),db.mapping(sid)):
            feeds=[('bt4g','https://bt4gprx.com/search?'+urlencode({'q':term,'orderby':'time','category':'movie','p':1,'page':'rss'})),
                   ('1337x','https://1337x.to/search/'+quote(term,safe='')+'/1/')]
            for kind,url in feeds:
                if con.execute('SELECT 1 FROM sources WHERE url=?',(url,)).fetchone():continue
                con.execute('INSERT INTO sources(show_id,kind,url,archive_url) VALUES(?,?,?,?)',(sid,kind,url,''));count+=1
    return count


def magnet_hash(value):
    p=urlparse(value)
    if p.scheme!='magnet':raise Rejected('通用索引缺少有效磁力标识')
    hashes=[]
    for xt in parse_qs(p.query).get('xt',[]):
        m=re.fullmatch(r'(?i)urn:btih:([a-f0-9]{40}|[a-z2-7]{32})',xt)
        if m:hashes.append(base64.b32decode(m[1].upper()).hex() if len(m[1])==32 else m[1].lower())
    if not hashes or len(set(hashes))!=1:raise Rejected('通用索引磁力hash缺失或冲突')
    return hashes[0]


def public_magnet(value):
    # Never retain third-party trackers, credentials or arbitrary redirect targets.
    return 'magnet:?xt=urn:btih:'+magnet_hash(value)


def corroborate_mode(db,sid,entry):
    m=entry['source_metadata']
    if m['media_mode']!='unknown' or not m.get('expected_hash'):return
    for c in db.rows('SELECT * FROM candidates WHERE show_id=?',(sid,)):
        if metadata(c).get('expected_hash')!=m['expected_hash'] or fidelity_blocker(db,c['title']):continue
        if media_mode(c) not in ('raw','soft'):continue
        m.update(media_mode=media_mode(c),mode_basis={'candidate_id':c['id'],'infohash':m['expected_hash']});return


def site_backoff(db,source):
    next_attempt=db.get('general_site_backoff_'+source['kind'],{}).get('next_attempt',0)
    if next_attempt<=time.time():return False
    db.execute('UPDATE sources SET next_attempt=? WHERE id=?',(next_attempt,source['id']));return True


def record_failure(db,source,error):
    # A shared site outage must not stall every series in the sequential worker.
    if isinstance(error,NetworkError):
        db.set('general_site_backoff_'+source['kind'],{'next_attempt':time.time()+3600,'error':safe_error(error)})


def parse_1337x(content,url):
    soup=BeautifulSoup(content,'html.parser');rows=soup.select('tr:has(a[href^="/torrent/"])')
    if not rows and not soup.select('table.table-list') and not re.search(r'(?i)no results|no torrents',soup.get_text(' ',strip=True)):
        raise NetworkError('1337x搜索返回验证页面或结构已变化')
    out=[]
    for row in rows[:40]:
        a=row.select_one('td[class^="coll-1"] a[href^="/torrent/"]')
        if not a:continue
        detail=checked_download_url(urljoin(url,a['href']));title=a.get_text(' ',strip=True)
        # Truncated titles cannot establish identity and must wait for full detail.
        if '...' in title or '…' in title:continue
        seed=row.select_one('td[class^="coll-2"]');cat=row.select_one('a[href^="/sub/"]')
        category=cat['href'].strip('/').split('/')[1] if cat else ''
        out.append({'title':title,'url':detail,'detail':detail,'published':None,
                    'source_metadata':{'index':'1337x','category':category,'media_mode':media_mode({'title':title,'source_metadata':{'media_mode':'raw' if category=='81' else 'unknown'}}),
                                       'seeders':int(re.sub(r'\D','',seed.get_text()) or '0') if seed else 0,'declaration_only':True}})
    return out


def search_1337x(db,web,source):
    items=parse_1337x(web.fetch(source['url']).content,source['url']);out=[];mapping=search_mapping(db.mapping(source['show_id']),'1337x')
    details=0
    for item in items:
        if not eligible(db,source['show_id'],'1337x'):break
        try:
            eps=validate_title(item['title'],mapping,{},False)
            if len(eps)!=1 or not db.allowed(source['show_id'],eps[0]) or fidelity_blocker(db,item['title']) or publisher_blocked(db,item['title']):continue
            # At most six matching detail pages per query; no whole-site crawling.
            if details>=6:break
            details+=1
            soup=BeautifulSoup(web.fetch(item['detail']).content,'html.parser')
            a=soup.select_one('a[href^="magnet:?"]')
            if not a:continue
            item['url']=public_magnet(a['href']);m=item['source_metadata'];m['expected_hash']=magnet_hash(item['url'])
            desc=soup.select_one('#description,.torrent-description')
            if desc:
                text=desc.get_text(' ',strip=True)[:20000]
                m['media_mode']=media_mode({'title':item['title']+' '+text,'source_metadata':m})
                m['description_sha256']=hashlib.sha256(text.encode()).hexdigest()
            if m['media_mode'] not in ('raw','soft'):continue
            out.append(item)
        except Rejected:continue
    return out


def resolve_torrent(db,web,candidate):
    h=magnet_hash(candidate['url'])
    if metadata(candidate).get('expected_hash')!=h:raise Rejected('磁力hash与索引记录不符')
    cached=db.path.parent/'torrents'/(h+'.torrent')
    if cached.is_file():return verify_hash(candidate,cached.read_bytes())
    # Indexes often describe the SAME torrent. Reuse verified public torrent links.
    for c in db.rows('SELECT * FROM candidates WHERE show_id=?',(candidate['show_id'],)):
        if c['hash']!=h and metadata(c).get('expected_hash')!=h:continue
        u=c['url'];p=urlparse(u)
        if p.scheme!='https' or p.hostname not in ('nyaa.si','mikanani.me','mikanime.tv','dl.dmhy.org','share.dmhy.org') or '.torrent' not in p.path:continue
        try:return verify_hash(candidate,web.fetch(validate_url(u)).content)
        except NetworkError:continue
    # A public hash query provides a concrete torrent without adding a magnet to qB.
    from .sources import parse_rss
    try:
        for c in parse_rss(web.fetch('https://nyaa.si/?'+urlencode({'page':'rss','c':'1_0','q':h})).content,'nyaa'):
            if metadata(c).get('expected_hash')==h:return verify_hash(candidate,web.fetch(c['url']).content)
    except NetworkError:pass
    try:return verify_hash(candidate,web.fetch('https://itorrents.net/torrent/'+h.upper()+'.torrent').content)
    except NetworkError:raise NetworkError('磁力已发现，种子元数据暂未取得；保留候选并等待重试，未投递下载器') from None
