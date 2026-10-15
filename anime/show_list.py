"""Request-local, read-only snapshot for the show list and its episode boards."""
import hashlib,json
from collections import defaultdict
from .db import Store
from .source_blacklist import KEY,blacklist_blocker
from pathlib import Path


def chunks(values):
    # Stay below SQLite's parameter limit, including on older bundled builds.
    for start in range(0,len(values),400):yield values[start:start+400]


class ShowListSnapshot:
    def __init__(self,db,clause,args,quarter,with_boards):
        self.shows={};self.episodes=defaultdict(list);self.watched=defaultdict(set)
        self.airings=defaultdict(list);self.candidates=defaultdict(list)
        self.memberships={};self.progress={};self.settings={}
        self.blocked_episodes=defaultdict(set)
        with db.connect() as c:
            # All database fields in one response come from the same WAL snapshot.
            c.execute('BEGIN')
            self.data=[dict(r) for r in c.execute('SELECT * FROM shows WHERE '+clause+' ORDER BY score DESC,title',args)]
            self.shows={s['id']:dict(s) for s in self.data}
            ids=list(self.shows)
            def related(sql,values):
                for batch in chunks(values):
                    marks=','.join('?' for _ in batch)
                    yield from (dict(r) for r in c.execute(sql.format(marks=marks),batch))
            for e in related('''SELECT e.show_id,e.episode,e.status,e.progress,e.first_completed,e.path,e.size,
                t.title source_task_title,t.selection source_task_selection FROM episodes e
                LEFT JOIN tasks t ON t.hash=e.hash WHERE e.show_id IN ({marks})''',ids):
                self.episodes[e['show_id']].append(e)
            kv_keys=['resolution','subtitle','groups',KEY,*(f'airing_sort_offset_{sid}' for sid in ids)]
            kv={r['key']:json.loads(r['value']) for r in related('SELECT key,value FROM kv WHERE key IN ({marks})',kv_keys)}
            self.settings={key:kv.get(key) for key in ('resolution','subtitle','groups')}
            for sid,eps in list(self.episodes.items()):
                visible=[]
                for e in eps:
                    if blacklist_blocker(kv.get(KEY,[]),e.pop('source_task_title'),e.pop('source_task_selection')) or blacklist_blocker(kv.get(KEY,[]),Path(e['path']).name):
                        self.blocked_episodes[sid].add(e['episode'])
                    else:visible.append(e)
                self.episodes[sid]=visible
            for w in related('SELECT show_id,episode FROM watches WHERE finished=1 AND show_id IN ({marks})',ids):
                self.watched[w['show_id']].add(w['episode'])
            for batch in chunks(ids):
                marks=','.join('?' for _ in batch)
                sql=f'SELECT show_id,kind FROM quarter_members WHERE show_id IN ({marks})'
                params=batch
                if quarter:sql+=' AND quarter=?';params=[*batch,quarter]
                else:sql+=" AND kind='continuing'"
                for r in c.execute(sql,params):self.memberships[r['show_id']]=r['kind']
            if not with_boards or not ids:return
            keys=[hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32]
                for eps in self.episodes.values() for e in eps if e['status']=='complete' and e['path']]
            for p in related('SELECT media_id,position,duration FROM web_progress WHERE media_id IN ({marks})',list(set(keys))):
                self.progress[p['media_id']]=p
            raw_airings=defaultdict(list)
            for a in related('SELECT * FROM airings WHERE show_id IN ({marks}) ORDER BY show_id,episode',ids):
                raw_airings[a['show_id']].append(a)
            for sid,s in self.shows.items():
                self.airings[sid]=Store.adjust_airings(json.loads(s['mapping']),raw_airings[sid],kv.get(f'airing_sort_offset_{sid}',0))
            # Inactive boards ignore missing-download gaps. Do not parse their release history.
            active=[sid for sid,s in self.shows.items() if Store.show_eligible(s)]
            for r in related('SELECT show_id,title FROM candidates WHERE show_id IN ({marks})',active):
                if not blacklist_blocker(kv.get(KEY,[]),r['title']):self.candidates[r['show_id']].append(r)
