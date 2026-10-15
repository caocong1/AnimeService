"""Controlled research targets and idle-only publication; never submit downloads."""
import json,time,hashlib
from pathlib import Path
from .source_blacklist import media_blocker
from .fidelity import fidelity_blocker


def media_id(path):return hashlib.sha256(str(path).lower().encode()).hexdigest()[:32]


def exists(db,name):return bool(db.one('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(name,)))


def tables(db):
    with db.connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS subtitle_targets(
          id TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,season INTEGER,
          payload TEXT NOT NULL,created REAL);
          CREATE TABLE IF NOT EXISTS subtitle_target_heads(
          show_id INTEGER,episode INTEGER,target_id TEXT NOT NULL,
          PRIMARY KEY(show_id,episode));
          CREATE TABLE IF NOT EXISTS subtitle_publications(
          id INTEGER PRIMARY KEY AUTOINCREMENT,show_id INTEGER,episode INTEGER,
          target_id TEXT,build_id TEXT,previous_episode TEXT,payload TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS subtitle_viewing_leases(
          session TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,media_id TEXT,expires REAL);''')


def head(db,sid,ep):
    if not exists(db,'subtitle_target_heads'):return None
    row=db.one('''SELECT t.* FROM subtitle_target_heads h JOIN subtitle_targets t ON t.id=h.target_id
                  WHERE h.show_id=? AND h.episode=?''',(sid,ep))
    return {**row,'payload':json.loads(row['payload'])} if row else None


def proof(root,record):
    if not isinstance(record,dict):raise ValueError('需要私有证据文件及hash')
    p=Path(record.get('path','')).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file():raise ValueError('证据须在私有字幕档案内')
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record.get('sha256'):raise ValueError('证据hash已变化')
    return json.loads(raw)


def retained_item(db,key):
    """Old URLs remain usable by already opened sessions; history is not a new default."""
    if not exists(db,'subtitle_publications'):return None
    for row in db.rows('SELECT previous_episode FROM subtitle_publications ORDER BY id DESC'):
        old=json.loads(row['previous_episode'])
        if old.get('path') and media_id(old['path'])==key and not media_blocker(db,old):
            return {**old,'id':key,'name':Path(old['path']).name,'folder':db.show(old['show_id'])['title'],
                    'legacy':not bool(old.get('hash')),'unverified':False,'retained_publication':True}
    return None


def default_binding(db,item):
    """Core acceptance alone must not publish a frozen research target."""
    if not exists(db,'subtitle_targets'):return False,None
    rows=db.rows('SELECT payload FROM subtitle_targets WHERE show_id=? AND episode=? ORDER BY created',
                 (item['show_id'],item['episode']))
    if not rows:return False,None
    for row in db.rows('SELECT payload,build_id FROM subtitle_publications WHERE show_id=? AND episode=? ORDER BY id DESC',
                       (item['show_id'],item['episode'])):
        if json.loads(row['payload'])['target']['fingerprint']==item['fingerprint']:return True,row['build_id']
    for row in rows:
        d=json.loads(row['payload']);old=d.get('previous_episode') or {}
        if old.get('path')==item['path']:return True,d.get('previous_build_id')
    return True,None


def accepted_build(archive,core,identity,target):
    """Revalidate an immutable previously published build even if newer research was accepted."""
    with archive.connect() as c:
        row=c.execute('SELECT manifest,target_fingerprint FROM builds WHERE id=? AND accepted=1',(identity,)).fetchone()
    if not row or row['target_fingerprint']!=target['fingerprint']:return None
    manifest=json.loads(row['manifest']);directory=(archive.root/'builds'/identity).resolve()
    if manifest.get('renderer_revision',1)!=core.RENDERER_REVISION:return None
    for f in manifest['files'].values():
        p=Path(f['path']).resolve()
        if not p.is_relative_to(directory) or not p.is_file() or core.digest(p.read_bytes())!=f['sha256']:return None
    cert=json.loads((directory/'acceptance.json').read_bytes());assets=cert['render']['font_assets']
    if set(assets)!={'zh','ja'}:return None
    for f in assets.values():
        p=Path(f['path']).resolve()
        if not p.is_relative_to(directory/'fonts') or not p.is_file() or core.digest(p.read_bytes())!=f['sha256']:return None
    manifest['font_assets']=assets
    return manifest


class ProductionTargets:
    def __init__(self,studio):
        self.studio=studio;self.db=studio.db;self.root=studio.root;tables(self.db)

    def eligible_media(self,sid,ep,fp):
        self.studio.ensure(sid,ep)
        if self.db.show(sid)['state']!='watching':raise ValueError('母片冻结和发布只用于当前在追作品')
        items=[m for m in self.studio.media() if m['show_id']==sid and m['episode']==ep and m['fingerprint']==fp]
        if len(items)!=1:raise ValueError('母片须有当前完成、身份、manifest及归属证明')
        item=items[0]
        task=self.db.one('SELECT * FROM tasks WHERE hash=?',(item.get('hash'),))
        if not task or task['status'] not in ('complete','linked'):raise ValueError('母片任务未完成或归属待核')
        selected=json.loads(task['selection'])['selected'].get(str(ep))
        if not selected or selected['size']!=item['size']:raise ValueError('母片单集manifest不符')
        expected=(Path(task['save_path'])/selected['name']).resolve()
        if Path(item['path']).resolve()!=expected:raise ValueError('母片路径与任务manifest不符')
        if fidelity_blocker(self.db,task['title'],task['selection']) or media_blocker(self.db,item):
            raise ValueError('母片被黑名单或保真规则排除')
        return item,task,selected

    def freeze(self,sid,ep,p):
        from .subtitle_studio import archive_for,lab_core,authored
        # An explicit review is required; codec/tags or static sampling are not cleanliness/audio proof.
        with self.db.gate:
            self.studio.ensure(sid,ep)
            old=head(self.db,sid,ep)
            if p.get('expected_target_id')!=(old['id'] if old else None):raise ValueError('母片版本冲突，请重读当前目标')
            item,task,selected=self.eligible_media(sid,ep,p.get('fingerprint'))
            review=proof(self.root,p.get('base_review'))
            if review.get('target_fingerprint')!=item['fingerprint'] or not review.get('reviewer'):
                raise ValueError('母片评审未绑定当前精确媒体')
            for name in ('identity','clean','fidelity','content_audio'):
                record=review.get(name,{})
                if record.get('status')!='verified' or not record.get('evidence'):
                    raise ValueError('母片尚缺实际'+name+'评审')
            if review['clean'].get('method') not in ('raw_continuous','soft_disabled_continuous'):
                raise ValueError('尚未连续核实洁净画面')
            if review['content_audio'].get('actual_listening') is not True:
                raise ValueError('原声/完整剪辑未实际听核')
            archive=archive_for(self.db);season=self.db.mapping(sid).get('season',1)
            draft=archive.draft(p.get('base_id',''))
            if fidelity_blocker(self.db,draft['metadata'].get('release','')):raise ValueError('字幕来源被排除')
            target={k:item[k] for k in ('path','size','fingerprint')}
            # Cross-version mappings are verified by the core; never clone accepted timing from old media.
            work=archive.workbook(sid,season,ep,draft['id'],target,mappings=p.get('alignments') or {})
            existing=self.db.one('SELECT * FROM episodes WHERE show_id=? AND episode=?',(sid,ep))
            prior=authored(self.db,{**existing,'size':existing.get('size') or 0}) if existing and existing.get('path') else None
            payload={'target':target,'workbook':str(work),'base_id':draft['id'],'base_review':p['base_review'],
                     'task_hash':task['hash'],'file_index':selected['index'],'previous_target_id':old['id'] if old else None,
                     'previous_episode':existing,'previous_build_id':prior['id'] if prior else None}
            identity=lab_core().digest(lab_core().canonical({**payload,'created_ns':time.time_ns()}).encode())
            with self.db.connect() as c:
                current=c.execute('SELECT target_id FROM subtitle_target_heads WHERE show_id=? AND episode=?',(sid,ep)).fetchone()
                if (current[0] if current else None)!=p.get('expected_target_id'):raise ValueError('并发目标版本冲突')
                c.execute('INSERT INTO subtitle_targets VALUES(?,?,?,?,?,?)',(identity,sid,ep,season,json.dumps(payload,ensure_ascii=False),time.time()))
                c.execute('INSERT INTO subtitle_target_heads VALUES(?,?,?) ON CONFLICT(show_id,episode) DO UPDATE SET target_id=excluded.target_id',(sid,ep,identity))
            refresh_pending=False
            try:self.studio.refresh_job(sid,ep)
            except (ValueError,OSError,KeyError) as ex:
                from .net import safe_error
                refresh_pending=True;self.db.event('subtitle-target',safe_error(ex),'warning')
            return {'ok':True,'target_id':identity,'fingerprint':target['fingerprint'],'published':False,'job_summary_pending':refresh_pending}

    def lease(self,item,session,closed=False):
        import re
        if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',session):raise ValueError('播放会话标识无效')
        with self.db.gate:
            self.db.execute('DELETE FROM subtitle_viewing_leases WHERE expires<?',(time.time(),))
            if closed:self.db.execute('DELETE FROM subtitle_viewing_leases WHERE session=? AND media_id=?',(session,item['id']))
            else:self.db.execute('''INSERT INTO subtitle_viewing_leases VALUES(?,?,?,?,?) ON CONFLICT(session)
                 DO UPDATE SET show_id=excluded.show_id,episode=excluded.episode,media_id=excluded.media_id,expires=excluded.expires''',
                 (session,item.get('show_id'),item.get('episode'),item['id'],time.time()+180))
        return {'ok':True}

    def busy(self,sid,ep):
        if self.db.one('SELECT 1 FROM subtitle_viewing_leases WHERE show_id=? AND episode=? AND expires>?',(sid,ep,time.time())):return True
        # Older clients have no lease: recent ordinary playback is also conservative protection.
        row=self.db.one('SELECT path FROM episodes WHERE show_id=? AND episode=?',(sid,ep))
        if row and row['path'] and exists(self.db,'web_progress'):
            recent=self.db.one('SELECT updated FROM web_progress WHERE media_id=?',(media_id(row['path']),))
            if recent and recent['updated']>time.time()-300:return True
        return False

    def publish(self,sid,ep,p):
        from .subtitle_studio import archive_for,lab_core
        with self.db.gate:
            self.studio.ensure(sid,ep);target=head(self.db,sid,ep)
            if not target or target['id']!=p.get('target_id'):raise ValueError('冻结母片版本已变化')
            frozen=target['payload'];item,task,selected=self.eligible_media(sid,ep,frozen['target']['fingerprint'])
            if {k:item[k] for k in frozen['target']}!=frozen['target']:raise ValueError('冻结母片路径或大小已变化')
            proof(self.root,frozen['base_review'])
            archive=archive_for(self.db);build=archive.selected(sid,target['season'],ep,frozen['target'])
            if not build or build['id']!=p.get('build_id'):raise ValueError('尚无与母片匹配的整集验收成品')
            decision=Path(build['files']['decisions.json']['path']) if 'decisions.json' in build['files'] else archive.root/'builds'/build['id']/'decisions.json'
            book=json.loads(decision.read_bytes());current=json.loads(Path(frozen['workbook']).read_bytes())
            if lab_core().canonical(book)!=lab_core().canonical(current):raise ValueError('成品与当前工作稿不一致')
            for cue in book['cues']:
                for field in ('source','japanese_source'):
                    ref=cue.get(field,{})
                    if ref.get('draft_id') and fidelity_blocker(self.db,archive.draft(ref['draft_id'])['metadata'].get('release','')):
                        raise ValueError('成品字幕来源被排除')
            player=proof(self.root,p.get('player_review'))
            if (player.get('build_id')!=build['id'] or player.get('target_fingerprint')!=item['fingerprint']
                or player.get('status')!='verified' or not player.get('reviewer') or not player.get('evidence')
                or player.get('actual_playback') is not True):raise ValueError('还缺精确目标播放器的真实验收')
            old=self.db.one('SELECT * FROM episodes WHERE show_id=? AND episode=?',(sid,ep))
            if not old or old['status']!='complete':raise ValueError('原过渡分集归属待核，禁止直接覆盖')
            if self.busy(sid,ep):return {'ok':False,'state':'waiting_idle','activated':False}
            idle=proof(self.root,p.get('idle_review'))
            if (idle.get('show_id')!=sid or idle.get('episode')!=ep or idle.get('status')!='verified'
                or idle.get('web_idle') is not True or idle.get('external_player_idle') is not True
                or not idle.get('reviewer') or not idle.get('evidence')
                or type(idle.get('observed_at')) not in (int,float)
                or not 0<=time.time()-idle['observed_at']<=60):
                raise ValueError('需当前网页及本机播放器空闲证明；未知会话不强换')
            # Same completed publication is idempotent, retaining its old-episode snapshot.
            previous=self.db.one('SELECT * FROM subtitle_publications WHERE show_id=? AND episode=? ORDER BY id DESC LIMIT 1',(sid,ep))
            if previous and previous['target_id']==target['id'] and previous['build_id']==build['id'] and old['path']==item['path']:
                return {'ok':True,'state':'published','activated':True,'build_id':build['id']}
            with self.db.connect() as c:
                fresh=c.execute('SELECT * FROM episodes WHERE show_id=? AND episode=?',(sid,ep)).fetchone()
                h=c.execute('SELECT target_id FROM subtitle_target_heads WHERE show_id=? AND episode=?',(sid,ep)).fetchone()
                if dict(fresh)!=old or h[0]!=target['id']:raise ValueError('正式分集或母片并发变化')
                payload={'target':frozen['target'],'player_review':p['player_review'],'idle_review':p['idle_review'],'build_id':build['id']}
                c.execute('INSERT INTO subtitle_publications(show_id,episode,target_id,build_id,previous_episode,payload,created) VALUES(?,?,?,?,?,?,?)',
                    (sid,ep,target['id'],build['id'],json.dumps(old,ensure_ascii=False),json.dumps(payload,ensure_ascii=False),time.time()))
                c.execute("UPDATE episodes SET hash=?,file_index=?,path=?,size=?,progress=1,status='complete' WHERE show_id=? AND episode=?",
                    (task['hash'],selected['index'],item['path'],item['size'],sid,ep))
            # No watched/progress timestamps are copied across edits. Old files and URLs remain.
            return {'ok':True,'state':'published','activated':True,'build_id':build['id'],'media_id':media_id(item['path'])}
