import contextlib, json, os, sqlite3, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATES = {'wish','trial','watching','paused','dropped','completed'}
DEFAULTS = {'poll_seconds':180,'resolution':'1080','subtitle':'any_zh','groups':[],
            'dandan_exe':str(Path.home()/'AppData/Roaming/弹弹play/dandanplay.exe'),
            'dandan_port':26666, 'minimum_free_gb':10}

class Store:
    def __init__(self, path=None):
        self.path=Path(path or os.environ.get('ANIMESERVICE_DB') or ROOT/'data/anime.db'); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.gate=threading.RLock()
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS shows(id INTEGER PRIMARY KEY, title TEXT NOT NULL, original TEXT DEFAULT '',
              summary TEXT DEFAULT '', image TEXT DEFAULT '', tags TEXT DEFAULT '[]', score REAL DEFAULT 0,
              air_date TEXT DEFAULT '', total INTEGER DEFAULT 0, quarter TEXT DEFAULT '', metadata TEXT DEFAULT '{}',
              state TEXT NOT NULL DEFAULT 'wish', selected INTEGER DEFAULT 0, watched INTEGER DEFAULT 0,
              rating INTEGER, feedback TEXT DEFAULT '', drop_reason TEXT DEFAULT '', mapping TEXT DEFAULT '{}',
              authorized INTEGER DEFAULT 0, updated REAL);
            CREATE TABLE IF NOT EXISTS airings(show_id INTEGER, episode INTEGER, airdate TEXT,
              title TEXT, PRIMARY KEY(show_id,episode));
            CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, show_id INTEGER NOT NULL, kind TEXT,
              url TEXT UNIQUE, archive_url TEXT DEFAULT '', last_attempt REAL, last_success REAL,
              archive_success REAL, next_attempt REAL DEFAULT 0, failures INTEGER DEFAULT 0,
              error TEXT DEFAULT '', archive_error TEXT DEFAULT '', latest TEXT DEFAULT '', enabled INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY, show_id INTEGER, source_id INTEGER,
              title TEXT, url TEXT, detail TEXT DEFAULT '', hash TEXT, status TEXT DEFAULT 'discovered',
              reason TEXT DEFAULT '', discovered REAL, UNIQUE(source_id,url));
            CREATE TABLE IF NOT EXISTS tasks(hash TEXT PRIMARY KEY,show_id INTEGER,title TEXT,
              status TEXT,selection TEXT,save_path TEXT,error TEXT DEFAULT '',created REAL,updated REAL,acknowledged INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS episodes(show_id INTEGER,episode INTEGER,hash TEXT,file_index INTEGER,
              path TEXT DEFAULT '',status TEXT,progress REAL DEFAULT 0,size INTEGER DEFAULT 0,
              PRIMARY KEY(show_id,episode));
            CREATE TABLE IF NOT EXISTS watches(show_id INTEGER,episode INTEGER,finished INTEGER DEFAULT 0,
              position REAL DEFAULT 0,method TEXT DEFAULT 'manual',updated REAL,PRIMARY KEY(show_id,episode));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,time REAL,level TEXT,scope TEXT,message TEXT);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS quarter_members(show_id INTEGER,quarter TEXT,kind TEXT,evidence TEXT,
              PRIMARY KEY(show_id,quarter));
            ''')
            if 'acknowledged' not in {r[1] for r in c.execute('PRAGMA table_info(tasks)')}:
                c.execute('ALTER TABLE tasks ADD COLUMN acknowledged INTEGER DEFAULT 0')
            task_columns={r[1] for r in c.execute('PRAGMA table_info(tasks)')}
            for name,definition in [('provider',"TEXT NOT NULL DEFAULT 'qbit'"),('external_id','INTEGER'),('staging_path',"TEXT DEFAULT ''")]:
                if name not in task_columns:c.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
            c.execute('''CREATE TABLE IF NOT EXISTS thunder_queue(hash TEXT PRIMARY KEY,action TEXT,state TEXT,
                lease TEXT DEFAULT '',expires REAL DEFAULT 0,committing INTEGER DEFAULT 0,error TEXT DEFAULT '',updated REAL)''')
            columns={r[1] for r in c.execute('PRAGMA table_info(shows)')}
            for name in ('auto_download','cleanup_hold'):
                if name not in columns:c.execute(f'ALTER TABLE shows ADD COLUMN {name} INTEGER DEFAULT 0')
            if 'first_completed' not in {r[1] for r in c.execute('PRAGMA table_info(episodes)')}:
                c.execute('ALTER TABLE episodes ADD COLUMN first_completed REAL')
            for k,v in DEFAULTS.items():c.execute('INSERT OR IGNORE INTO kv VALUES(?,?)',(k,json.dumps(v)))
    @contextlib.contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=20); c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA synchronous=FULL')
        try:
            yield c;c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def rows(self,sql,args=()):
        with self.connect() as c:return [dict(x) for x in c.execute(sql,args).fetchall()]
    def one(self,sql,args=()):
        r=self.rows(sql,args);return r[0] if r else None
    def execute(self,sql,args=()):
        with self.connect() as c:return c.execute(sql,args).lastrowid
    def get(self,k,default=None):
        r=self.one('SELECT value FROM kv WHERE key=?',(k,));return json.loads(r['value']) if r else default
    def set(self,k,v):self.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False)))
    def event(self,scope,message,level='info'):
        self.execute('INSERT INTO events(time,level,scope,message) VALUES(?,?,?,?)',(time.time(),level,scope,message[:1000]))
    def show(self,sid):return self.one('SELECT * FROM shows WHERE id=?',(sid,))
    def mapping(self,sid):return json.loads(self.show(sid)['mapping'])
    def local_airings(self,sid):
        # Catalog rows retain Bangumi `sort` numbering; consumers use the explicit airing offset,
        # else Bangumi's own sort-to-ep shift, never the release/file offset or a guessed total.
        offset=self.mapping(sid).get('airing_offset') or self.get('airing_sort_offset_'+str(sid),0)
        return [{**a,'source_episode':a['episode'],'episode':a['episode']-offset}
            for a in self.rows('SELECT * FROM airings WHERE show_id=? ORDER BY episode',(sid,))
            if a['episode']-offset>0]
    def eligible(self,sid):
        s=self.show(sid);m=json.loads(s['mapping']) if s else {}
        return bool(s and s['authorized'] and not s['cleanup_hold'] and
            (s['state'] in ('trial','watching') or (s['auto_download'] and s['state']=='wish')) and
            m.get('confirmed') and m.get('inventory_checked'))
    def allowed(self,sid,ep):
        s=self.show(sid);m=json.loads(s['mapping'])
        return self.eligible(sid) and m.get('start',1)<=ep<=m.get('end',s['total'] or 999)
    def upsert_subject(self,item,quarter=''):
        # Deliberately never update state, selection, mapping, authorization or feedback on metadata sync.
        previous=self.show(item['id'])
        item={**(json.loads(previous['metadata']) if previous else {}),**item}
        total=item.get('total_episodes') or item.get('eps') or 0
        if not total:
            for field in item.get('infobox',[]):
                if field.get('key') in ('话数','話数') and str(field.get('value','')).isdigit():
                    total=int(field['value'])
        total=total or (previous['total'] if previous else 0)
        with self.connect() as c:
            c.execute('''INSERT INTO shows(id,title,original,summary,image,tags,score,air_date,total,quarter,metadata,updated)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,
             original=excluded.original,summary=excluded.summary,image=excluded.image,tags=excluded.tags,
             score=excluded.score,air_date=excluded.air_date,total=excluded.total,
             quarter=CASE WHEN excluded.quarter='' THEN shows.quarter ELSE excluded.quarter END,
             metadata=excluded.metadata,updated=excluded.updated''',
             (item['id'],item.get('name_cn') or item.get('name') or str(item['id']),item.get('name',''),
              item.get('summary',''),item.get('images',{}).get('medium',''),json.dumps(item.get('tags',[]),ensure_ascii=False),
              item.get('rating',{}).get('score',0),item.get('date',''),total,
              quarter,json.dumps(item,ensure_ascii=False),time.time()))
