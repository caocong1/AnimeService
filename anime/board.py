"""Per-episode states for the home board: read-only, derived from existing tables."""
import datetime,hashlib,stat
from pathlib import Path
from .source_blacklist import media_filter

# watched > resume > ready > downloading > missing > aired > future
def episode_board(db,engine,s,*,snapshot=None,gaps=None):
    sid=s['id'];today=datetime.date.today().isoformat()
    eps={e['episode']:e for e in (getattr(snapshot,'board_episodes',snapshot.episodes)[sid] if snapshot is not None else db.rows('''SELECT e.episode,e.status,e.progress,e.path,e.size,e.hash,t.status task_status,t.error task_error
        FROM episodes e LEFT JOIN tasks t ON t.hash=e.hash WHERE e.show_id=?''',(sid,)))}
    if snapshot is not None:blocked=snapshot.blocked_episodes.get(sid,set())
    else:
        is_blocked=media_filter(db)
        blocked={n for n,e in eps.items() if is_blocked(e)}
    watched=snapshot.watched[sid] if snapshot is not None else {w['episode'] for w in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1',(sid,))}
    airings={a['episode']:a['airdate'] for a in (snapshot.airings[sid] if snapshot is not None else db.local_airings(sid))}
    keys={n:hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32] for n,e in eps.items() if e['status']=='complete' and e['path']}
    progress={}
    if snapshot is not None:progress=snapshot.progress
    elif keys:
        marks=','.join('?'*len(keys))
        progress={p['media_id']:p for p in db.rows(f'SELECT media_id,position,duration FROM web_progress WHERE media_id IN ({marks})',list(keys.values()))}
    # Aired-but-absent only counts as missing while the show is set to download; otherwise it is just aired.
    try:
        if gaps is None:gaps=engine.gaps(sid,snapshot=snapshot) if snapshot is not None else engine.gaps(sid)
        missing=set(gaps['missing']) if gaps.get('active') else set()
    except Exception:missing=set()
    last=max([s['total'] or 0,*eps,*watched,*airings,*missing,0])
    out=[];next_ep=None
    diagnostics=(getattr(snapshot,'download_gaps',{}) if snapshot is not None else db.get('download_gaps',{})).get('shows',{}).get(str(sid),{})
    for n in range(1,last+1):
        e=eps.get(n);item={'n':n}
        if n in blocked:e=None
        if e and e['status']=='complete' and file_ok(e['path'],e['size']):
            key=keys[n]
            item['media']=key
            p=progress.get(key)
            if n in watched:item['s']='watched'
            elif p and 0<p['position']<(p['duration'] or 0)-30:
                item.update(s='resume',position=p['position'],duration=p['duration'])
            else:item['s']='ready'
        elif n in watched:item['s']='watched'
        elif e and e['status']=='complete':item['s']='missing' # recorded complete, file gone or changed
        elif e and e['status'] not in ('held','cleaned','file_missing','error'):
            item.update(s='downloading',progress=round((e['progress'] or 0)*100))
        elif n in missing or (e and e['status'] in ('file_missing','error')):item['s']='missing'
        elif airings.get(n) and airings[n]<=today:item['s']='aired'
        else:
            item['s']='future'
            if airings.get(n):item['date']=airings[n]
        if item['s']=='missing' and str(n) in diagnostics:item['download']=diagnostics[str(n)]
        if item['s'] in ('missing','downloading') and e:
            status=e.get('task_status');error=e.get('task_error')
            reason=error or {'intent':'下载提交回执待确认；后台先按 hash 对账，避免重复提交',
                'review':'下载任务需要核验，保留原任务及回执','held':'下载任务已保留，等待授权条件恢复'}.get(status)
            if reason:item['download']={'code':'task_'+str(status),'reason':reason}
            elif e['status'] in ('file_missing','error'):
                item['download']={'code':e['status'],'reason':'下载文件缺失或下载器报错；后台正在核验原任务'}
        if item['s']=='missing' and e and e['status']=='complete':
            item['download']={'code':'file_missing','reason':'已登记的文件缺失或大小改变；保留原记录，等待核验'}
        out.append(item)
    for item in out:
        if item['s'] in ('resume','ready'):next_ep=item;break
    return {'eps':out,'next':next_ep}


def file_ok(path,size):
    try:
        info=Path(path).stat() if path else None
        return bool(info and stat.S_ISREG(info.st_mode) and info.st_size==size)
    except OSError:return False
