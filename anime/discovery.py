"""Read-only animation discovery; importing metadata is an explicit separate action."""
import re
from urllib.parse import urlencode
from .net import NetworkError
from .db import subject_episode_total

KINDS={'all','series','tv','web','movie','ova','continuing','uncertain'}

def platform_of(item):
    value=str(item.get('platform') or '').strip()
    return {'Movie':'剧场版','movie':'剧场版','映画':'剧场版','OAD':'OVA'}.get(value,value)

def visible(item,kind='all',include_unknown=False):
    tags={t.get('name','') for t in item.get('tags',[])}|set(item.get('meta_tags',[]))
    if item.get('nsfw') or tags & {'里番','R18','18禁'}:return False
    if tags & {'国产','中国','欧美','美国','法国','韩国','国漫','英国','俄罗斯','苏联','加拿大','西班牙'} and '日本' not in tags:return False
    japanese=bool('日本' in tags or re.search('[ぁ-ゟ゠-ヿ]',item.get('name','')))
    if not japanese and not (include_unknown or kind=='uncertain'):return False
    platform=platform_of(item)
    return kind not in ('series','tv','web','movie','ova') or platform in {
        'series':{'TV','WEB'},'tv':{'TV'},'web':{'WEB'},'movie':{'剧场版'},'ova':{'OVA'}
    }[kind]

def discover(catalog,db,q='',kind='all',year=0,offset=0,include_unknown=False):
    q=q.strip()
    if len(q)>100 or kind not in KINDS or kind=='continuing':raise ValueError('无效搜索条件')
    if year and not 1900<=year<=2100:raise ValueError('无效年份')
    if offset<0 or offset>2000:raise ValueError('搜索页码超出范围')
    if q:
        filters={'type':[2],'nsfw':False}
        if year:filters['air_date']=[f'>={year}-01-01',f'<{year+1}-01-01']
        url='https://api.bgm.tv/v0/search/subjects?'+urlencode({'limit':24,'offset':offset})
        result=catalog.web.fetch(url,method='POST',json={'keyword':q,'sort':'match','filter':filters}).json()
    else:
        params={'type':2,'sort':'date','limit':24,'offset':offset}
        if kind in ('tv','web','movie','ova'):params['cat']={'tv':1,'web':5,'movie':3,'ova':2}[kind]
        if year:params['year']=year
        result=catalog.web.fetch('https://api.bgm.tv/v0/subjects?'+urlencode(params)).json()
    items=result.get('data');total=result.get('total')
    if not isinstance(items,list) or type(total) is not int or total<0 or result.get('offset',offset)!=offset:
        raise NetworkError('搜索分页响应无效，请重试')
    rows=[];seen=set()
    for item in items:
        sid=item.get('id')
        if type(sid) is not int or sid<=0 or sid in seen or item.get('type',2)!=2:continue
        seen.add(sid)
        if not visible(item,kind,include_unknown):continue
        local=db.show(sid)
        rows.append({'id':sid,'title':item.get('name_cn') or item.get('name') or str(sid),
            'original':item.get('name',''),'image':item.get('images',{}).get('medium',''),
            'air_date':item.get('date',''),'total':subject_episode_total(item),
            'score':item.get('rating',{}).get('score',0),'platform':platform_of(item),
            'selected':local['selected'] if local else 0,'state':local['state'] if local else 'wish',
            'in_catalog':bool(local),'quarter_kind':'new',
            'region_confidence':'已标注日本' if '日本' in {t.get('name','') for t in item.get('tags',[])} or re.search('[ぁ-ゟ゠-ヿ]',item.get('name','')) else '地区待核实'})
    next_offset=offset+len(items)
    return {'items':rows,'total':total,'offset':offset,'next_offset':next_offset if items and next_offset<total and next_offset<=2000 else None,
            'page_filtered':True}

def import_subject(catalog,db,sid):
    if sid<=0:raise ValueError('无效作品编号')
    # Never call prepare/mapping or download authorization here.
    item=catalog.web.fetch(f'https://api.bgm.tv/v0/subjects/{sid}').json()
    if item.get('id')!=sid or item.get('type')!=2 or not visible(item,'uncertain'):
        raise ValueError('此条目不是可收录的动画')
    db.upsert_subject(item)
    return {'ok':True,'id':sid}
