"""Conservative release validation; ambiguity never becomes an automatic download."""
import hashlib,json,re,unicodedata
from pathlib import PurePosixPath
from functools import lru_cache
from opencc import OpenCC
_chinese=OpenCC('t2s')

VIDEO={'.mkv','.mp4','.avi','.m4v','.ts','.webm'}
SUB={'.ass','.ssa','.srt','.sup'}
class Rejected(ValueError):pass
@lru_cache(maxsize=32768)
def norm(s):return re.sub(r'[^\w\u3040-\u30ff\u3400-\u9fff]+','',_chinese.convert(unicodedata.normalize('NFKC',s)).casefold())
def season_of(title):
    m=re.search(r'(?i)(?<![a-z])(?:S(?:eason)?\s*0*(\d{1,2}))(?=[^a-z0-9]|E\d|$)',title)
    if m:return int(m[1])
    m=re.search(r'第([一二三四五六七八九十\d]+)[季期]',title)
    if m:
        v=m[1];return int(v) if v.isdigit() else {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}.get(v,99)
    m=re.search(r'(?i)\b(\d)(?:st|nd|rd|th)\s+season',title)
    return int(m[1]) if m else None
def episodes_of(title,offset=0):
    if re.search(r'(?i)(?:\s-\s*|E|EP|第|\[)\d{1,3}\.\d',title):raise Rejected('小数集号需人工核对')
    if re.search(r'(?i)(?:\b(?:OVA|OAD|SP|NCOP|NCED|PV|Trailer)\b|特别篇|特別篇|总集篇|總集篇|剧场版|劇場版)',title):raise Rejected('特别篇/总集篇需单独核对')
    patterns=[r'(?i)S\d{1,2}E(\d{1,3})(?:\s*[-~]\s*(?:S\d{1,2})?E?(\d{1,3}))?',
              r'第\s*(\d{1,3})(?:\s*[-~～]\s*(\d{1,3}))?\s*[话話集]',
              r'[\[【]\s*(\d{1,3})(?:\s*[-~～]\s*(\d{1,3}))?(?:v\d)?(?:\s*(?:END|完|全集))?\s*[\]】]',
              r'\s-\s*(\d{1,3})(?:\s*[-~～]\s*(\d{1,3}))?(?:v\d)?(?=[\s\[.(]|$)',
              r'(?i)\bEP?(\d{1,3})(?:\s*[-~]\s*E?(\d{1,3}))?(?=[\s\[.(]|$)']
    for pattern in patterns:
        m=re.search(pattern,title,re.I)
        if m:
            a=int(m[1])-offset;b=int(m[2] or m[1])-offset
            if a<1 or b<a or b-a>100:raise Rejected('集数/偏移超出可核验范围')
            return list(range(a,b+1))
    raise Rejected('无法确定正片集号')
def validate_title(title,mapping,settings,quality=True):
    if mapping.get('required_title') and norm(mapping['required_title']) not in norm(title):raise Rejected('发布标题不属于已核验的赛段')
    aliases=mapping.get('aliases',[])
    if not aliases or not any(norm(a) in norm(title) for a in aliases if len(norm(a))>=2):raise Rejected('作品别名不匹配')
    season=season_of(title)
    if mapping.get('require_season') and season!=mapping.get('season',1):raise Rejected('搜索来源缺少明确的正确季号')
    if season is not None and season!=mapping.get('season',1):raise Rejected('季数不匹配')
    if re.search(r'(?i)\bS\d{1,2}\s*[-~]\s*S?\d{1,2}\b',title):raise Rejected('跨季合集需人工核对')
    if quality:
        resolution=mapping.get('resolution') or settings.get('resolution','1080')
        if resolution and not re.search(r'(?i)'+re.escape(resolution)+r'(?:p|i|[\] _.]|$)',title):raise Rejected('分辨率不符合设置或未标明')
        zh=bool(re.search(r'(?i)简|簡|繁|中文|CHS|CHT|SC\b|TC\b|GB\b|BIG5',title))
        if not zh:raise Rejected('未明确标注中文字幕')
        sub=mapping.get('subtitle') or settings.get('subtitle','any_zh')
        if sub=='chs' and not re.search(r'(?i)简|簡|CHS|SC\b|GB\b',title):raise Rejected('不符合简中偏好')
        if sub=='cht' and not re.search(r'(?i)繁|CHT|TC\b|BIG5',title):raise Rejected('不符合繁中偏好')
        groups=mapping.get('groups') or settings.get('groups',[])
        if groups and not any(norm(g) in norm(title[:150]) for g in groups):raise Rejected('不符合字幕组设置')
    return episodes_of(title,mapping.get('offset',0))

def bdecode(data):
    pos=0;info_span=None
    def parse(depth=0):
        nonlocal pos,info_span
        if depth>40 or pos>=len(data):raise Rejected('无效种子结构')
        start=pos;ch=data[pos:pos+1];pos+=1
        if ch==b'i':
            end=data.index(b'e',pos);v=int(data[pos:end]);pos=end+1;return v
        if ch in (b'd',b'l'):
            obj={} if ch==b'd' else []
            while data[pos:pos+1]!=b'e':
                if ch==b'd':
                    key=parse(depth+1);beg=pos;obj[key]=parse(depth+1)
                    if depth==0 and key==b'info':info_span=(beg,pos)
                else:obj.append(parse(depth+1))
            pos+=1;return obj
        if ch.isdigit():
            end=data.index(b':',start);length=int(data[start:end]);pos=end+1+length
            if length<0 or pos>len(data):raise Rejected('种子字符串越界')
            return data[end+1:pos]
        raise Rejected('不是有效的torrent文件')
    try:
        result=parse()
        if pos!=len(data) or not info_span:raise Rejected('种子缺少info')
        info=result[b'info']
        if b'pieces' not in info:raise Rejected('第一版暂不自动处理纯v2种子')
        def dec(v):
            try:return v.decode('utf-8')
            except UnicodeDecodeError:return v.decode('gb18030')
        name=dec(info.get(b'name.utf-8',info[b'name']))
        raw=info.get(b'files')
        files=[{'index':i,'name':name+'/'+ '/'.join(dec(x) for x in f.get(b'path.utf-8',f[b'path'])),'size':f[b'length']} for i,f in enumerate(raw)] if raw else [{'index':0,'name':name,'size':info[b'length']}]
        for f in files:
            p=PurePosixPath(f['name'].replace('\\','/'))
            if p.is_absolute() or '..' in p.parts or ':' in f['name'] or f['size']<0:raise Rejected('种子文件路径不安全')
        return hashlib.sha1(data[info_span[0]:info_span[1]]).hexdigest(),files
    except (KeyError,IndexError,ValueError,UnicodeError) as e:
        if isinstance(e,Rejected):raise
        raise Rejected('种子结构或编码不可核验') from None

def select_files(files,mapping,wanted,title_eps):
    selected={};video_count=sum(PurePosixPath(f['name']).suffix.lower() in VIDEO for f in files)
    for f in files:
        if PurePosixPath(f['name']).suffix.lower() not in VIDEO:continue
        if not any(norm(a) in norm(f['name']) for a in mapping.get('aliases',[]) if len(norm(a))>=2):
            raise Rejected('实际文件作品名不匹配；请核对并补充准确文件别名')
        if season_of(f['name']) not in (None,mapping.get('season',1)):raise Rejected('合集内文件季数不符')
        basename=PurePosixPath(f['name']).name
        if re.search(r'(?i)\b(?:OVA|OAD|SP|NCOP|NCED|PV|Trailer)\b|特别篇|特別篇',basename):raise Rejected('合集含特别篇或宣传视频，需单独核对')
        try:eps=episodes_of(basename,mapping.get('offset',0))
        except Rejected as ex:
            if str(ex)=='无法确定正片集号' and video_count==1 and len(title_eps)==1:eps=title_eps
            else:raise Rejected('合集内有无法确定集号的视频')
        if len(eps)!=1:raise Rejected('一文件多集需人工核对')
        ep=eps[0]
        if ep not in title_eps:raise Rejected('文件集号超出发布标题范围')
        if ep in wanted:
            if ep in selected:raise Rejected('同一集包含多个视频版本')
            selected[ep]=f
    if set(selected)!=set(wanted):raise Rejected('种子没有覆盖所需集数')
    ids=[f['index'] for f in selected.values()]
    for f in files:
        if PurePosixPath(f['name']).suffix.lower() not in SUB:continue
        try:
            if any(e in selected for e in episodes_of(PurePosixPath(f['name']).name,mapping.get('offset',0))):ids.append(f['index'])
        except Rejected:pass
    return selected,ids
