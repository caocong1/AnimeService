"""Repair a declared bilingual release alias only under an independently proved source."""
import json,re,time
from pathlib import Path
from .releases import Rejected,validate_title,select_files
from .native_indexes import candidate_mapping


def preparation_inventory(engine,sid,mapping):
    """Exclude only individually audited files proved to belong to another subject.

    A shared title, season suffix or file date alone never establishes this.
    The original inventory and files remain untouched and are not linked here.
    """
    found=engine.inventory_matches(sid,mapping)
    audit=engine.db.get('inventory_other_subject_'+str(sid),{})
    if audit.get('show_id')!=sid or audit.get('mikan_id')!=mapping.get('mikan_id'):return found
    proofs={p['path']:p for p in audit.get('files',[]) if p.get('all_pieces_match') and
            p.get('other_subject_id') and p['other_subject_id']!=sid and p.get('source_identity_verified')}
    remaining=[]
    for item in found:
        proof=proofs.get(item['path'])
        try:
            stat=Path(item['path']).stat()
            verified=proof and item['size']==proof['size']==stat.st_size and stat.st_mtime_ns==proof['mtime_ns']
        except OSError:verified=False
        if not verified:remaining.append(item)
    return remaining


def verified_part_numbering(db,sid,mapping):
    """Use independent in-season ep / franchise sort values, never infer from filenames."""
    show=db.show(sid)
    part=re.search(r'第(\d+)クール|Part\s*(\d+)',show['original'],re.I)
    if not mapping.get('mikan_id') or not part:return mapping
    evidence=db.get('bangumi_episodes_'+str(sid),{})
    if time.time()-evidence.get('time',0)>21600:return mapping
    rows=evidence.get('items',[])
    if len(rows)<2 or any(r.get('subject_id')!=sid or type(r.get('ep')) not in (int,float) or type(r.get('sort')) not in (int,float)
                          or r['ep']<1 or not float(r['ep']).is_integer() or not float(r['sort']).is_integer() for r in rows):return mapping
    shifts={int(r['sort']-r['ep']) for r in rows}
    if len(shifts)!=1:return mapping
    offset=shifts.pop();end=mapping['end']
    if offset<end or offset+end>999 or not any(r['ep']==1 and r['sort']==offset+1 for r in rows):return mapping
    label=int(next(x for x in part.groups() if x))
    return {**mapping,'airing_offset':offset,'source_seasons':sorted({mapping['season'],label}),
            'numbering':[{'min':1,'max':end,'offset':0},{'min':offset+1,'max':offset+end,'offset':offset}]}


def select_verified_release(engine,candidate,files,wanted,title_eps):
    db=engine.db;sid=candidate['show_id'];mapping=candidate_mapping(db.mapping(sid),candidate)
    try:return select_files(files,mapping,wanted,title_eps)
    except Rejected as error:
        if '实际文件作品名不匹配' not in str(error):raise
        original=error
    if not db.review_wanted(sid) or not db.allowed(sid,1) or wanted!=[1] or title_eps!=[1]:raise original
    source=db.one("SELECT * FROM sources WHERE id=? AND show_id=? AND kind='mikan' AND enabled=1",(candidate['source_id'],sid))
    mid=mapping.get('mikan_id')
    if not source or not mid or not source['url'].endswith('bangumiId='+str(mid)):raise original
    engine.catalog.mikan_mapping(mid,sid) # A release title alone cannot prove identity.
    validate_title(candidate['title'],mapping,{},False)
    title=re.sub(r'^(?:\[[^\]]+\]\s*)+','',candidate['title'])
    base=re.split(r'\s-\s*\d|\[\d|第\s*\d+\s*[话話集]|\s+S\d{1,2}E\d{1,3}\b',title,flags=re.I)[0]
    aliases=[s.strip(' []【】') for s in re.split(r'(?<!\d)/(?!\d)',base)]
    # Publishers also declare bilingual names with hyphens, and may spell the
    # season as "Season 2" in the release but "S02" in the actual filename.
    for alias in aliases[:]:
        aliases.extend(s.strip(' -－') for s in re.split(
            r'(?<=[\u3040-\u30ff\u3400-\u9fff])[-－](?=[A-Za-z])|(?<=[A-Za-z])[-－](?=[\u3040-\u30ff\u3400-\u9fff])',alias))
        stem=re.sub(r'\s+Season\s*\d+\s*$','',alias,flags=re.I).strip()
        if stem!=alias:aliases.append(stem)
    aliases=list(dict.fromkeys(aliases))
    additions=[a for a in aliases if 3<=len(a)<=150 and a not in mapping['aliases']]
    if not additions or len(mapping['aliases'])+len(additions)>20:raise original
    fixed={**mapping,'aliases':mapping['aliases']+additions}
    selected,ids=select_files(files,fixed,wanted,title_eps)
    with db.gate:
        if db.mapping(sid)!=mapping or not db.review_wanted(sid) or not db.allowed(sid,1):raise original
        db.execute('UPDATE shows SET mapping=? WHERE id=?',(json.dumps(fixed,ensure_ascii=False),sid))
        db.set('source_alias_repair_'+str(candidate['id']),{'show_id':sid,'mikan_id':mid,'aliases':additions,'time':time.time()})
        db.event('source:'+str(sid),'已核对同一Bangumi来源与首集文件，自动补充发布标题中明确声明的别名；季集规则未放宽')
    return selected,ids
