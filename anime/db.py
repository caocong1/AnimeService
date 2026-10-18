import contextlib, json, os, sqlite3, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATES = {'wish','trial','watching','paused','dropped','completed'}
DEFAULTS = {'poll_seconds':180,'resolution':'1080','subtitle':'any_zh','groups':[],
            'dandan_exe':str(Path.home()/'AppData/Roaming/弹弹play/dandanplay.exe'),
            'dandan_port':26666, 'minimum_free_gb':10}

def subject_episode_total(item):
    # v0 total_episodes counts database chapters, including specials. eps is
    # parsed from the wiki episode count; never prefer chapters over that count.
    total=item.get('eps') or 0
    if not total:
        for field in item.get('infobox',[]):
            if field.get('key') in ('话数','話数') and str(field.get('value','')).isdigit():
                total=int(field['value']);break
    return total or item.get('total_episodes') or 0

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
            CREATE TABLE IF NOT EXISTS bangumi_sync_queue(show_id INTEGER PRIMARY KEY,
              revision INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'pending',
              error TEXT DEFAULT '',failures INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,
              updated REAL DEFAULT 0,last_success REAL,resolve_revision INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bangumi_sync_receipts(account_id INTEGER,show_id INTEGER,
              payload TEXT NOT NULL,PRIMARY KEY(account_id,show_id));
            CREATE TABLE IF NOT EXISTS quarter_members(show_id INTEGER,quarter TEXT,kind TEXT,evidence TEXT,
              PRIMARY KEY(show_id,quarter));
            ''')
            if 'acknowledged' not in {r[1] for r in c.execute('PRAGMA table_info(tasks)')}:
                c.execute('ALTER TABLE tasks ADD COLUMN acknowledged INTEGER DEFAULT 0')
            task_columns={r[1] for r in c.execute('PRAGMA table_info(tasks)')}
            for name,definition in [('provider',"TEXT NOT NULL DEFAULT 'qbit'"),('external_id','INTEGER'),('staging_path',"TEXT DEFAULT ''")]:
                if name not in task_columns:c.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
            if 'purpose' not in task_columns:c.execute("ALTER TABLE tasks ADD COLUMN purpose TEXT NOT NULL DEFAULT 'episode'")
            if 'published' not in {r[1] for r in c.execute('PRAGMA table_info(candidates)')}:
                c.execute('ALTER TABLE candidates ADD COLUMN published REAL')
            if 'source_metadata' not in {r[1] for r in c.execute('PRAGMA table_info(candidates)')}:
                c.execute("ALTER TABLE candidates ADD COLUMN source_metadata TEXT NOT NULL DEFAULT '{}'")
            c.executescript('''
            CREATE TABLE IF NOT EXISTS source_policies(show_id INTEGER PRIMARY KEY,config TEXT NOT NULL,updated REAL);
            CREATE TABLE IF NOT EXISTS source_samples(candidate_id INTEGER PRIMARY KEY,show_id INTEGER NOT NULL,
                family TEXT NOT NULL,hash TEXT,state TEXT,path TEXT DEFAULT '',size INTEGER DEFAULT 0,
                evidence TEXT DEFAULT '{}',review TEXT DEFAULT '{}',error TEXT DEFAULT '',updated REAL);
            CREATE TABLE IF NOT EXISTS subtitle_review_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL UNIQUE,content_hash TEXT NOT NULL,candidate_id INTEGER NOT NULL,
                show_id INTEGER NOT NULL,episode INTEGER NOT NULL,recorded REAL NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS subtitle_review_history_candidate ON subtitle_review_history(candidate_id,id);
            ''')
            c.execute('''CREATE TABLE IF NOT EXISTS thunder_queue(hash TEXT PRIMARY KEY,action TEXT,state TEXT,
                lease TEXT DEFAULT '',expires REAL DEFAULT 0,committing INTEGER DEFAULT 0,error TEXT DEFAULT '',updated REAL)''')
            columns={r[1] for r in c.execute('PRAGMA table_info(shows)')}
            for name in ('auto_download','cleanup_hold'):
                if name not in columns:c.execute(f'ALTER TABLE shows ADD COLUMN {name} INTEGER DEFAULT 0')
            if 'first_completed' not in {r[1] for r in c.execute('PRAGMA table_info(episodes)')}:
                c.execute('ALTER TABLE episodes ADD COLUMN first_completed REAL')
            for k,v in DEFAULTS.items():c.execute('INSERT OR IGNORE INTO kv VALUES(?,?)',(k,json.dumps(v)))
            # Queue changes in the SAME transaction as every writer, including dandan history.
            # Timestamp/position-only updates and catalog refreshes never export watched flags.
            enqueue = """INSERT INTO bangumi_sync_queue(show_id,revision,state,updated)
                VALUES({sid},1,'pending',strftime('%s','now')) ON CONFLICT(show_id) DO UPDATE SET
                revision=revision+1,state='pending',error='',next_attempt=0,updated=excluded.updated;"""
            c.executescript('''CREATE TRIGGER IF NOT EXISTS bangumi_show_update
                AFTER UPDATE OF selected,state,rating,feedback,mapping ON shows
                WHEN NEW.selected IS NOT OLD.selected OR NEW.state IS NOT OLD.state OR
                     NEW.rating IS NOT OLD.rating OR NEW.feedback IS NOT OLD.feedback OR NEW.mapping IS NOT OLD.mapping
                BEGIN ''' + enqueue.format(sid='NEW.id') + ''' END;
                CREATE TRIGGER IF NOT EXISTS bangumi_watch_insert AFTER INSERT ON watches
                WHEN EXISTS(SELECT 1 FROM shows WHERE id=NEW.show_id AND selected=1)
                BEGIN ''' + enqueue.format(sid='NEW.show_id') + ''' END;
                CREATE TRIGGER IF NOT EXISTS bangumi_watch_update AFTER UPDATE OF finished ON watches
                WHEN NEW.finished IS NOT OLD.finished AND EXISTS(SELECT 1 FROM shows WHERE id=NEW.show_id AND selected=1)
                BEGIN ''' + enqueue.format(sid='NEW.show_id') + ''' END;
                CREATE TRIGGER IF NOT EXISTS bangumi_watch_delete AFTER DELETE ON watches
                WHEN EXISTS(SELECT 1 FROM shows WHERE id=OLD.show_id AND selected=1)
                BEGIN ''' + enqueue.format(sid='OLD.show_id') + ''' END;''')
            c.execute("""INSERT OR IGNORE INTO bangumi_sync_queue(show_id,revision,state,updated)
                SELECT id,1,'pending',strftime('%s','now') FROM shows WHERE selected=1""")
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
    def prune_events(self,keep_days=None,now=None):
        """Drop events older than events_keep_days (default 30) that are not among the newest 2000 rows."""
        keep_days=keep_days if keep_days is not None else self.get('events_keep_days',30);now=now or time.time()
        try:keep_days=min(max(int(keep_days),1),3650)
        except (TypeError,ValueError):keep_days=30
        floor=self.one('SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET 2000')  # the newest 2000 rows always stay
        if not floor:return 0
        with self.connect() as c:
            return c.execute('DELETE FROM events WHERE time<? AND id<=?',(now-keep_days*86400,floor['id'])).rowcount
    def show(self,sid):return self.one('SELECT * FROM shows WHERE id=?',(sid,))
    def mapping(self,sid):return json.loads(self.show(sid)['mapping'])
    def local_airings(self,sid):
        # Catalog rows retain Bangumi `sort` numbering; consumers use the explicit airing offset,
        # else Bangumi's own sort-to-ep shift, never the release/file offset or a guessed total.
        mapping=self.mapping(sid)
        fallback=self.get('airing_sort_offset_'+str(sid),0) if not mapping.get('airing_offset') else 0
        return self.adjust_airings(mapping,
            self.rows('SELECT * FROM airings WHERE show_id=? ORDER BY episode',(sid,)),
            fallback)
    @staticmethod
    def adjust_airings(mapping,airings,fallback_offset=0):
        offset=mapping.get('airing_offset') or fallback_offset
        return [{**a,'source_episode':a['episode'],'episode':a['episode']-offset}
            for a in airings
            if a['episode']-offset>0]
    def eligible(self,sid):
        return self.show_eligible(self.show(sid))
    @staticmethod
    def show_eligible(s):
        m=json.loads(s['mapping']) if s else {}
        return bool(s and s['authorized'] and not s['cleanup_hold'] and
            (s['state'] in ('trial','watching') or (s['auto_download'] and s['state']=='wish')) and
            m.get('confirmed') and m.get('inventory_checked'))
    def allowed(self,sid,ep):
        s=self.show(sid);m=json.loads(s['mapping'])
        return self.eligible(sid) and m.get('start',1)<=ep<=m.get('end',s['total'] or 999)
    def review_wanted(self,sid):
        s=self.show(sid)
        return bool(s and s['selected'] and not s['cleanup_hold'] and s['state'] in ('wish','trial','watching'))
    def upsert_subject(self,item,quarter=''):
        # Deliberately never update state, selection, mapping, authorization or feedback on metadata sync.
        previous=self.show(item['id'])
        item={**(json.loads(previous['metadata']) if previous else {}),**item}
        total=subject_episode_total(item)
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
