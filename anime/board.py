"""Per-episode states for the home board: read-only, derived from existing tables."""
import datetime,hashlib
from pathlib import Path

# watched > resume > ready > downloading > missing > aired > future
def episode_board(db,engine,s):
    sid=s['id'];today=datetime.date.today().isoformat()
    eps={e['episode']:e for e in db.rows('SELECT episode,status,progress,path,size FROM episodes WHERE show_id=?',(sid,))}
    watched={w['episode'] for w in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1',(sid,))}
    airings={a['episode']:a['airdate'] for a in db.rows('SELECT episode,airdate FROM airings WHERE show_id=?',(sid,))}
    try:missing=set(engine.gaps(sid)['missing'])
    except Exception:missing=set()
    last=max([s['total'] or 0,*eps,*watched,*airings,*missing,0])
    out=[];next_ep=None
    for n in range(1,last+1):
        e=eps.get(n);item={'n':n}
        if e and e['status']=='complete' and file_ok(e['path'],e['size']):
            key=hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32]
            item['media']=key
            p=db.one('SELECT position,duration FROM web_progress WHERE media_id=?',(key,))
            if n in watched:item['s']='watched'
            elif p and 0<p['position']<(p['duration'] or 0)-30:
                item.update(s='resume',position=p['position'],duration=p['duration'])
            else:item['s']='ready'
        elif n in watched:item['s']='watched'
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
    try:return bool(path) and Path(path).is_file() and Path(path).stat().st_size==size
    except OSError:return False
