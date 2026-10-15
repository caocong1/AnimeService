"""Clean-picture discovery for the existing personally authorized subtitle cohort.

RSS category/softsub declarations are acquisition hints, not rendered-frame
acceptance. Exact identity, torrent files and host submission gates still apply.
"""
import base64,json,re
from urllib.parse import urlencode,urlparse
from .releases import validate_title,Rejected,bdecode

KINDS={'nyaa','tokyotosho','bt4g','1337x'}
INDEX_HOSTS={'nyaa.si','www.tokyotosho.info','tokyotosho.info','bt4g.org','bt4gprx.com','1337x.to','www.1337x.to'}


def metadata(candidate):
    value=candidate.get('source_metadata') or {}
    try:return json.loads(value) if isinstance(value,str) else value
    except (ValueError,TypeError):return {}


def media_mode(candidate):
    title=candidate.get('title','')
    if re.search(r'(?i)hardsubs?|hard[ ._-]*subtitles?|硬字幕|硬嵌|内嵌|內嵌',title):return 'hard'
    if re.search(r'(?i)softsubs?|soft[ ._-]*subtitles?|multi[ ._-]*subs?|内封|內封|外挂|外掛|多国字幕|多國字幕',title):return 'soft'
    if re.search(r'(?i)无字幕|無字幕|\b(?:RAW|unsubbed|unsubtitled)\b',title):return 'raw'
    # A publisher's "-raws" name does not establish subtitle mode.
    return metadata(candidate).get('media_mode','unknown')


def clean_required(db,sid):
    from .viewing_workflow import enabled,production_wanted
    if enabled(db) and not production_wanted(db,sid):return False
    if not (db.get('subtitle_clean_media_preferred',False) and
            db.get('subtitle_studio_enabled',False) and db.review_wanted(sid)):return False
    row=db.one('SELECT config FROM source_policies WHERE show_id=?',(sid,))
    return bool(row and json.loads(row['config']).get('mode') in ('sampling','selected'))


def clean_blocker(db,candidate):
    if clean_required(db,candidate['show_id']) and media_mode(candidate) not in ('raw','soft'):
        return '字幕制作新取样需要生肉或明确可关闭的软字幕；当前版本未满足'
    return ''


def task_clean_blocker(db,task):
    # Existing receipts/in-flight/uncertain outcomes continue reconciliation.
    if task.get('acknowledged') or task.get('status') not in ('intent','held'):return ''
    from .viewing_workflow import reference_task,enabled,lane
    # A normal, policy-selected Chinese release keeps watching available while
    # the separate clean-base/custom-subtitle production is pending.
    if enabled(db) and task.get('purpose','episode')=='episode' and lane(db,task['show_id'])=='watching':return ''
    if reference_task(db,task):return ''
    if not clean_required(db,task['show_id']):return ''
    candidates=db.rows('''SELECT c.* FROM candidates c WHERE c.show_id=? AND
        (c.hash=? OR c.id IN (SELECT candidate_id FROM source_samples WHERE hash=?)
         OR c.id IN (SELECT candidate_id FROM subtitle_samples WHERE hash=?))''',
        (task['show_id'],task['hash'],task['hash'],task['hash']))
    candidate=next((c for c in candidates if c['title']==task['title']),task)
    return clean_blocker(db,candidate)


def acquisition_priority(candidate):
    mode=media_mode(candidate);title=candidate.get('title','')
    return (0 if mode=='raw' else 1 if mode=='soft' else 2,
            0 if re.search(r'(?i)WEB[ ._-]*DL',title) else 1,
            0 if re.search(r'(?i)1080p',title) else 1,
            -int(metadata(candidate).get('seeders') or 0),candidate.get('id',0))


def search_mapping(mapping,kind):
    # Feed association is never sequel proof. Search results AND file manifests
    # must explicitly identify a sequel, without changing the saved mapping.
    return {**mapping,'require_season':True,'index_require_file_season':True} if kind in KINDS and mapping.get('season',1)>1 else mapping


def candidate_mapping(mapping,candidate):
    return search_mapping(mapping,metadata(candidate).get('index'))


def eligible(db,sid,kind=None):
    if not db.get('native_indexes_enabled',False) or not db.review_wanted(sid) or not db.allowed(sid,1):return False
    row=db.one('SELECT config FROM source_policies WHERE show_id=?',(sid,))
    valid=bool(row and json.loads(row['config']).get('mode') in ('sampling','selected'))
    if valid and kind in ('bt4g','1337x'):
        from .general_indexes import platform_basis
        valid=bool(db.get('general_indexes_enabled',False) and platform_basis(db,sid))
    return valid


def search_terms(show,mapping):
    aliases=[a for a in mapping.get('aliases',[]) if isinstance(a,str) and 3<=len(a)<=200]
    latin=next((a for a in aliases if re.search('[A-Za-z]',a) and not re.search(r'[\[\]【】]|S\d+E\d+|1080|2160|WEB-DL',a,re.I)
                and not re.search('[\u3400-\u9fffぁ-ゟ゠-ヿ]',a)),None)
    terms=[]
    for value in (latin,show.get('original')):
        if not value:continue
        value=re.sub(r'\s*(?:第[\d一二三四五六七八九十]+[季期]|Season\s*\d+)\s*$','',value,flags=re.I)
        value=re.sub(r'\s*\(\d{4}\)|\b\d+\s*/\s*\d+|½',' ',value)
        value=re.sub(r'\s+',' ',value).strip()
        if 3<=len(value)<=200 and value not in terms:terms.append(value)
    return terms[:2]


def register_sources(db,sid):
    if not eligible(db,sid):return 0
    terms=search_terms(db.show(sid),db.mapping(sid));count=0
    with db.gate,db.connect() as con:
        if not eligible(db,sid):return 0
        for term in terms:
            feeds=[('nyaa','https://nyaa.si/?'+urlencode({'page':'rss','c':'1_0','f':0,'q':term}))]
            feeds += [('tokyotosho','https://www.tokyotosho.info/rss.php?'+urlencode({'filter':category,'terms':term})) for category in (7,1)]
            for kind,url in feeds:
                # Never re-enable a disabled source or steal another work's URL.
                if con.execute('SELECT 1 FROM sources WHERE url=?',(url,)).fetchone():continue
                con.execute('INSERT INTO sources(show_id,kind,url,archive_url) VALUES(?,?,?,?)',(sid,kind,url,''));count+=1
    return count


def entry_metadata(entry,kind):
    categories=[t.get('term','') for t in entry.get('tags',[])]
    category=entry.get('nyaa_categoryid') or (categories[0] if categories else '')
    raw=category=='1_4' if kind=='nyaa' else kind=='tokyotosho' and 'Raws' in categories
    result={'index':kind,'category':category,'media_mode':'raw' if raw else 'unknown','declaration_only':True}
    result['media_mode']=media_mode({'title':entry.get('title',''),'source_metadata':result})
    expected=str(entry.get('nyaa_infohash') or '').strip().lower()
    if re.fullmatch('[a-f0-9]{40}',expected):result['expected_hash']=expected
    if not result.get('expected_hash'):
        # Tokyo publishes a magnet in the description, sometimes with base32 btih.
        match=re.search(r'(?i)urn:btih:([a-f0-9]{40}|[a-z2-7]{32})(?:&|["\s<]|$)',entry.get('link','')+' '+entry.get('summary',''))
        if match:
            value=match[1];result['expected_hash']=base64.b32decode(value.upper()).hex() if len(value)==32 else value.lower()
    try:result['seeders']=max(0,int(entry.get('nyaa_seeders',0)))
    except (ValueError,TypeError):result['seeders']=0
    return result


def verify_hash(candidate,data):
    expected=metadata(candidate).get('expected_hash')
    if expected and bdecode(data)[0]!=expected:raise Rejected('种子infohash与索引发布标识不符')
    return data


def checked_download_url(url):
    p=urlparse(url)
    if p.scheme=='http' and p.hostname in INDEX_HOSTS:url='https:'+url[5:];p=urlparse(url)
    if p.scheme!='https' or p.hostname not in INDEX_HOSTS or p.username or p.password or p.port not in (None,443):
        raise Rejected('索引种子链接尚未核验为受支持来源')
    return url
