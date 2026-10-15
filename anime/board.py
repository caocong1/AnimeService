"""Per-episode states for the home board: read-only, derived from existing tables."""
import datetime,hashlib,stat
from pathlib import Path
from .source_blacklist import media_filter

# watched > resume > ready > downloading > missing > aired > future
def episode_board(db,engine,s,*,snapshot=None,gaps=None):
    sid=s['id'];today=datetime.date.today().isoformat()
    eps={e['episode']:e for e in (getattr(snapshot,'board_episodes',snapshot.episodes)[sid] if snapshot is not None else db.rows('SELECT episode,status,progress,path,size,hash FROM episodes WHERE show_id=?',(sid,)))}
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
        out.append(item)
    for item in out:
        if item['s'] in ('resume','ready'):next_ep=item;break
    return {'eps':out,'next':next_ep}


def file_ok(path,size):
    try:
        info=Path(path).stat() if path else None
        return bool(info and stat.S_ISREG(info.st_mode) and info.st_size==size)
    except OSError:return False
