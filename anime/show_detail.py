"""One request-local database snapshot for show details, never a response cache."""
import hashlib,json
from collections import defaultdict
from .db import Store
from .show_list import chunks
from .source_blacklist import KEY,blacklist_blocker
from pathlib import Path


class ShowDetailSnapshot:
    board_only=False

    def __init__(self,db,sid):
        self.shows={};self.episodes=defaultdict(list);self.watched=defaultdict(set)
        self.airings=defaultdict(list);self.candidates=defaultdict(list);self.progress={}
        self.blocked_episodes=defaultdict(set)
        with db.connect() as c:
            c.execute('BEGIN')
            def rows(sql,args=(sid,)):
                return [dict(r) for r in c.execute(sql,args)]
            data=rows('SELECT * FROM shows WHERE id=?')
            if not data:return
            self.shows[sid]=data[0]
            self.episodes[sid]=rows('''SELECT e.*,coalesce(w.finished,0) finished,w.method watch_method,
                t.title source_task_title,t.selection source_task_selection
                FROM episodes e LEFT JOIN watches w ON e.show_id=w.show_id AND e.episode=w.episode
                LEFT JOIN tasks t ON t.hash=e.hash
                WHERE e.show_id=? ORDER BY e.episode''')
            self.watches=rows('SELECT * FROM watches WHERE show_id=? ORDER BY episode')
            self.watched[sid]={w['episode'] for w in self.watches if w['finished']==1}
            self.sources=rows('SELECT * FROM sources WHERE show_id=?')
            self.candidates[sid]=rows('SELECT * FROM candidates WHERE show_id=? ORDER BY id DESC')
            keys=['resolution','subtitle','groups',f'airing_sort_offset_{sid}',KEY]
            kv={r['key']:json.loads(r['value']) for r in rows(
                'SELECT key,value FROM kv WHERE key IN (?,?,?,?,?)',keys)}
            self.settings={key:kv.get(key) for key in keys[:3]}
            self.airings[sid]=Store.adjust_airings(json.loads(data[0]['mapping']),
                rows('SELECT * FROM airings WHERE show_id=? ORDER BY episode'),kv.get(keys[3],0))
            self.candidates[sid]=[r for r in self.candidates[sid] if not blacklist_blocker(kv.get(KEY,[]),r['title'])]
            visible=[]
            for e in self.episodes[sid]:
                blocked=blacklist_blocker(kv.get(KEY,[]),e.pop('source_task_title'),e.pop('source_task_selection')) or blacklist_blocker(kv.get(KEY,[]),Path(e['path']).name)
                if blocked:
                    self.blocked_episodes[sid].add(e['episode']);continue
                e['media_id']=hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32] if e['path'] else None
                visible.append(e)
            self.episodes[sid]=visible
            media_ids=list({e['media_id'] for e in self.episodes[sid] if e['media_id']})
            for batch in chunks(media_ids):
                marks=','.join('?' for _ in batch)
                for p in rows(f'SELECT media_id,position,duration FROM web_progress WHERE media_id IN ({marks})',batch):
                    self.progress[p['media_id']]=p
            self.board_episodes={sid:[dict(e) for e in self.episodes[sid]]}
            for e in self.episodes[sid]:
                p=self.progress.get(e['media_id'])
                e['progress']={'position':p['position'],'duration':p['duration']} if p else None
