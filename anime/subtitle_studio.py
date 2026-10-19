"""Episode subtitle research. Native downloads stay behind the host's existing gates."""
import hashlib,importlib.util,json,re,shutil,sys,threading,time
from pathlib import Path
from fastapi.responses import FileResponse
from .download_policy import require_submission_ready
from .fidelity import fidelity_blocker,preference_blocker
from .source_blacklist import blacklist_blocker,KEY as BLACKLIST_KEY
from .releases import bdecode,validate_title,select_files,Rejected
from .source_review import family
from .subtitles import Subtitles
from .net import safe_error
from .native_indexes import candidate_mapping,clean_blocker,acquisition_priority
from .viewing_workflow import (enabled as split_enabled,production_wanted,base_remaining,
                              require_base_slot,require_reference_slot,queue_snapshot,RESEARCH_MODES)


def lab_core():
    name='_anime_subtitle_lab_core'
    if name not in sys.modules:
        path=Path('D:/SubtitleSkillLab/subtitle_lab/core.py')
        if not path.is_file():raise ValueError('字幕研究项目未安装')
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        sys.modules[name]=module
    return sys.modules[name]


def archive_for(db):
    return lab_core().Archive(db.path.parent/'subtitle-studio')


def authored(db,item):
    """Only accepted builds bound to the current verified library episode are playable."""
    if not db.get('subtitle_studio_enabled',False) or item.get('unverified') or not item.get('show_id') or not item.get('episode'):
        return None
    try:
        archive=archive_for(db);target={k:item[k] for k in ('path','size')}
        target['fingerprint']=lab_core().fingerprint(item['path'],item['size'])
        from .subtitle_targets import default_binding,accepted_build
        managed,identity=default_binding(db,{**item,**target})
        selected=(accepted_build(archive,lab_core(),identity,target) if identity else None) if managed else archive.selected(item['show_id'],db.mapping(item['show_id']).get('season',1),item['episode'],target)
        if selected:
            book=json.loads((archive.root/'builds'/selected['id']/'decisions.json').read_text(encoding='utf-8'))
            for cue in book['cues']:
                for field in ('source','japanese_source'):
                    ref=cue[field]
                    if not ref.get('draft_id'):continue
                    base=archive.draft(ref['draft_id'])
                    if fidelity_blocker(db,base['metadata'].get('release','')):return None
        return selected
    except (ValueError,OSError,KeyError):return None


class SubtitleStudio:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.sub=Subtitles(self.db)
        self.root=self.db.path.parent/'subtitle-studio';self.lock=threading.Lock()
        with self.db.connect() as con:
            con.executescript('''CREATE TABLE IF NOT EXISTS subtitle_samples(
              candidate_id INTEGER PRIMARY KEY,show_id INTEGER NOT NULL,episode INTEGER NOT NULL,
              family TEXT NOT NULL,hash TEXT,state TEXT,path TEXT DEFAULT '',size INTEGER DEFAULT 0,
              error TEXT DEFAULT '',updated REAL,evidence TEXT DEFAULT '{}');
              CREATE INDEX IF NOT EXISTS subtitle_samples_episode ON subtitle_samples(show_id,episode);
              CREATE TABLE IF NOT EXISTS subtitle_studio_media(
              fingerprint TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,path TEXT,payload TEXT,updated REAL);
              CREATE TABLE IF NOT EXISTS subtitle_studio_jobs(
              show_id INTEGER,episode INTEGER,state TEXT,payload TEXT,error TEXT DEFAULT '',updated REAL,
              PRIMARY KEY(show_id,episode));''')
        from .subtitle_targets import ProductionTargets
        self.production_targets=ProductionTargets(self)
        from .subtitle_delivery import SubtitleDelivery
        self.delivery=SubtitleDelivery(self.db)

    def ensure(self,sid,ep,submission=False):
        if not self.db.get('subtitle_studio_enabled',False):raise ValueError('逐集字幕制作未启用')
        if not production_wanted(self.db,sid):raise ValueError('逐集自制字幕只在个人在追流程执行；原稿和历史保留')
        row=self.db.one('SELECT config FROM source_policies WHERE show_id=?',(sid,))
        if not row or json.loads(row['config']).get('mode') not in RESEARCH_MODES:
            raise ValueError('不在想看新番字幕研究范围')
        if not self.db.review_wanted(sid) or not self.db.allowed(sid,1) or not self.db.allowed(sid,ep):
            raise ValueError('个人意愿、独立授权、映射/盘点或季集门禁未满足')
        if submission and self.db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1',(sid,ep)):
            raise ValueError('已看集保持原字幕，不自动新增制作取样')
        if submission:require_submission_ready(self.db)

    def candidates(self,sid):
        out=[];mapping=self.db.mapping(sid)
        for c in self.db.rows('''SELECT c.* FROM candidates c JOIN sources s ON s.id=c.source_id
               WHERE c.show_id=? AND s.enabled=1 ORDER BY c.id''',(sid,)):
            try:
                eps=validate_title(c['title'],candidate_mapping(mapping,c),{},False)
                if len(eps)!=1:continue
                self.ensure(sid,eps[0])
                if fidelity_blocker(self.db,c['title']):continue
            except (ValueError,Rejected):continue
            out.append({**c,'episode':eps[0],'family':family(c['title'])})
        return out

    def queue(self,c,role='clean_base'):
        sid,ep=c['show_id'],c['episode'];self.ensure(sid,ep,True)
        if role not in ('clean_base','subtitle_reference'):raise ValueError('未知研究角色')
        if (ep==1 and role=='clean_base'
                and self.engine.source_review.policy(sid).get('mode')!='subtitle_only'):
            return  # Existing premiere sampler retains these.
        if role=='subtitle_reference':require_reference_slot(self.db,sid,ep)
        elif clean_blocker(self.db,c):raise ValueError(clean_blocker(self.db,c))
        data=self.engine.sources.torrent(c);h,files=bdecode(data)
        expected=re.search(r'(?i)([a-f0-9]{40})\.torrent',c['url'])
        if expected and expected[1].lower()!=h:raise ValueError('字幕样本hash不符')
        selected,ids=select_files(files,candidate_mapping(self.db.mapping(sid),c),[ep],[ep])
        if set(selected)!={ep}:raise ValueError('字幕研究只取该集单版本')
        selection={'files':files,'ids':ids,'selected':{str(ep):selected[ep]}}
        with self.db.gate:
            self.ensure(sid,ep,True);self.engine.check_legacy_owner(sid)
            if (ep==1 and role=='clean_base'
                    and self.engine.source_review.policy(sid).get('mode')!='subtitle_only'):return
            blocked=fidelity_blocker(self.db,c['title'],selection)
            if blocked:raise ValueError(blocked)
            if self.db.one('SELECT 1 FROM tasks WHERE hash=?',(h,)):return
            if role=='subtitle_reference':require_reference_slot(self.db,sid,ep)
            else:require_base_slot(self.db,c,ep)
            if self.engine.thunder.reader.by_hash(h) or self.engine.qb.info(h):return
            size=sum(f['size'] for f in files if f['index'] in ids)
            used=self.db.one('SELECT coalesce(sum(size),0) n FROM subtitle_samples WHERE show_id=? AND episode=?',(sid,ep))['n']
            if used+size>20*1024**3:raise ValueError('本集字幕研究超过20GiB上限')
            if shutil.disk_usage(self.engine.incomplete.anchor or self.db.path.parent).free<size+self.db.get('minimum_free_gb',10)*1024**3:
                raise ValueError('字幕研究空间不足')
            thunder=self.db.get('downloader_preference')=='thunder_first'
            stage=self.engine.incomplete/h
            title=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',self.db.show(sid)['title']).strip('. ')[:80]
            dest=self.engine.library/(title+f' [bgm-{sid}]')/f'Season {self.db.mapping(sid).get("season",1):02}'/'Subtitle samples'/h
            stage.mkdir(parents=True,exist_ok=True);dest.mkdir(parents=True,exist_ok=True)
            (self.engine.cache/(h+'.torrent')).write_bytes(data)
            with self.db.connect() as con:
                con.execute('''INSERT INTO tasks(hash,show_id,title,status,selection,save_path,created,updated,provider,staging_path,purpose)
                VALUES(?,?,?,'intent',?,?,?,?,?,?,'subtitle_sample')''',
                (h,sid,c['title'],json.dumps(selection,ensure_ascii=False),str(dest),time.time(),time.time(),'thunder' if thunder else 'qbit',str(stage)))
                con.execute('''INSERT INTO subtitle_samples(candidate_id,show_id,episode,family,hash,state,size,updated,evidence)
                   VALUES(?,?,?,?,?,'queued',?,?,?)''',(c['id'],sid,ep,c['family']['key'],h,selected[ep]['size'],time.time(),
                   json.dumps({'workflow_role':role},ensure_ascii=False)))
            task=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            if thunder:self.engine.thunder.enqueue(task)
            else:self.engine.dispatch(task)

    def discover(self,limit=4):
        if not self.db.get('subtitle_studio_enabled',False):return
        count=0;attempted=0
        health=self.db.get('qbit_health',{})
        can_submit=not (self.db.get('downloader_preference')=='qbit' and health.get('ok') is False and time.time()-health.get('time',0)<300)
        for row in self.db.rows('SELECT show_id FROM source_policies ORDER BY show_id'):
            sid=row['show_id']
            try:self.ensure(sid,1)
            except ValueError:continue
            watched={r['episode'] for r in self.db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1',(sid,))}
            subtitle_only=self.engine.source_review.policy(sid).get('mode')=='subtitle_only'
            fresh=[c['id'] for c in sorted(self.engine.source_review.candidates(sid),key=acquisition_priority)
                   if not clean_blocker(self.db,c) and not self.db.one('SELECT 1 FROM source_samples WHERE candidate_id=?',(c['id'],))]
            if 1 in watched or subtitle_only:fresh=[]
            if split_enabled(self.db):fresh=fresh[:base_remaining(self.db,sid,1)]
            if fresh and can_submit and attempted<limit:
                attempted+=1
                try:self.engine.source_review.start(sid,fresh[:min(12,limit)])
                except Exception as ex:self.db.set('subtitle_studio_premiere_'+str(sid),{'time':time.time(),'error':safe_error(ex)})
            # Premiere samples keep their current source-review history and protection.
            choices={}
            current=[c for c in sorted(self.candidates(sid),key=acquisition_priority)
                     if c['episode'] not in watched]
            archive=archive_for(self.db)
            seen_jobs=set()
            for c in current:
                key=archive.register(sid,self.db.mapping(sid).get('season',1),c['episode'])
                record={'candidate_id':c['id'],'publisher':c['family']['group'],'platform':c['family']['provider'],
                        'episode':c['episode'],'title':c['title'],'published':c.get('published')}
                value=lab_core().canonical(record);identity=lab_core().digest(value.encode())
                with archive.connect() as con:
                    con.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?,?)',(identity,key,value,time.time()))
                if c['episode'] not in seen_jobs:
                    seen_jobs.add(c['episode']);key_error=f'subtitle_studio_job_error_{sid}_{c["episode"]}'
                    try:
                        self.refresh_job(sid,c['episode'],archive)
                        if self.db.get(key_error) is not None:self.db.execute('DELETE FROM kv WHERE key=?',(key_error,))
                    except Exception as ex:  # a changed frozen target or missing core for one episode must not stop the others
                        message=safe_error(ex)
                        if self.db.get(key_error,{}).get('error')!=message:self.db.event('subtitle-studio',f'{sid} E{c["episode"]} {message}','warning')
                        self.db.set(key_error,{'time':time.time(),'error':message})
                if c['episode']==1 and not subtitle_only:continue
                if clean_blocker(self.db,c):continue
                choices.setdefault((c['episode'],c['family']['key']),c)
            for (ep,fam),c in sorted(choices.items()):
                if not can_submit or attempted>=limit:continue
                if self.db.one('SELECT 1 FROM subtitle_samples WHERE show_id=? AND episode=? AND family=?',(sid,ep,fam)):continue
                if split_enabled(self.db) and base_remaining(self.db,sid,ep)==0:continue
                if self.db.one('SELECT count(*) n FROM subtitle_samples WHERE show_id=? AND episode=?',(sid,ep))['n']>=12:continue
                prior=self.db.get('subtitle_studio_discovery_'+str(c['id']),{})
                if time.time()-prior.get('time',0)<600:continue
                attempted+=1
                try:
                    before=self.db.one('SELECT count(*) n FROM subtitle_samples')['n'];self.queue(c)
                    count+=self.db.one('SELECT count(*) n FROM subtitle_samples')['n']>before
                except Exception as ex:
                    self.db.set('subtitle_studio_discovery_'+str(c['id']),{'time':time.time(),'error':safe_error(ex)})

    def media(self):
        # The ordinary episode and sample must each carry their own completion evidence.
        rows=self.db.rows("SELECT show_id,episode,path,hash FROM episodes WHERE status='complete' AND path<>''")
        for row in self.db.rows("SELECT show_id,path,hash,candidate_id FROM source_samples WHERE path<>'' AND state IN ('ready','analyzing')"):
            rows.append({**row,'episode':1})
        rows+=self.db.rows("SELECT show_id,episode,path,hash,candidate_id FROM subtitle_samples WHERE path<>''")
        # A downloader outage must not hide a package already verified complete in
        # our ledger merely because its sample summary has not acquired a path.
        # This derives read-only inputs; it does not complete or ready any sample.
        for table,purpose in (('source_samples','source_sample'),('subtitle_samples','subtitle_sample')):
            episode_column='s.episode' if table=='subtitle_samples' else '1 AS episode'
            for row in self.db.rows(f"""SELECT s.show_id,s.path,s.hash,s.candidate_id,s.size,{episode_column}
                FROM {table} s JOIN tasks t ON t.hash=s.hash
                WHERE s.path='' AND t.status IN ('complete','linked')
                AND s.state IN ('queued','analyzing','ready','analysis_error','error')"""):
                rows.append({**row,'episode':row.get('episode',1),'_derived_purpose':purpose})
        out=[];seen={}
        for row in rows:
            sid,ep=row['show_id'],row['episode']
            try:self.ensure(sid,ep)
            except ValueError:continue
            task=self.db.one('SELECT * FROM tasks WHERE hash=?',(row['hash'],)) if row['hash'] else None
            candidate=self.db.one('SELECT title FROM candidates WHERE id=?',(row['candidate_id'],)) if row.get('candidate_id') else None
            title=task['title'] if task else candidate['title'] if candidate else ''
            if row.get('_derived_purpose'):
                try:
                    if (not task or task['show_id']!=sid or task['purpose']!=row['_derived_purpose']
                            or task.get('provider') not in ('qbit','thunder')):continue
                    queue=self.db.one('SELECT state,lease,committing FROM thunder_queue WHERE hash=?',(task['hash'],))
                    if queue and (queue['lease'] or queue['committing'] or queue['state'] in ('claimed','review')):continue
                    if self.db.get('thunder_dialog_hold_'+task['hash'],{}).get('unconfirmed'):continue
                    candidate=self.db.one('SELECT * FROM candidates WHERE id=?',(row['candidate_id'],))
                    source=self.db.one('SELECT show_id FROM sources WHERE id=?',(candidate['source_id'],)) if candidate else None
                    if (not candidate or candidate['show_id']!=sid or not source or source['show_id']!=sid
                            or candidate['title']!=title or candidate.get('hash') not in (None,'',task['hash'])):continue
                    mapping=candidate_mapping(self.db.mapping(sid),candidate)
                    if validate_title(title,mapping,{},False)!=[ep]:continue
                    selection=json.loads(task['selection'])
                    if set(selection['selected'])!={str(ep)}:continue
                    h,files=bdecode((self.engine.cache/(task['hash']+'.torrent')).read_bytes(),include_padding=True)
                    plain=[{k:f[k] for k in ('index','name','size')} for f in files]
                    if h!=task['hash'] or plain!=selection['files']:continue
                    chosen,ids=select_files(plain,mapping,[ep],[ep]);f=chosen[ep]
                    if (selection['selected'][str(ep)]!=f or selection['ids']!=ids
                            or row['size']!=f['size']):continue
                    root=Path(task['save_path']).resolve()
                    if task['status']=='linked':
                        receipt=self.db.get('thunder_readonly_'+task['hash'],{})
                        if (receipt.get('mode')!='read_only' or receipt.get('hash')!=task['hash']
                                or receipt.get('size')!=f['size']):continue
                        path=Path(receipt['path']).resolve()
                    else:
                        label='Source samples' if task['purpose']=='source_sample' else 'Subtitle samples'
                        try:parts=root.relative_to(self.engine.library.resolve()).parts
                        except ValueError:parts=()
                        owned=(len(parts)==4 and parts[0].endswith(f' [bgm-{sid}]')
                               and parts[1]==f'Season {self.db.mapping(sid).get("season",1):02}'
                               and parts[2]==label and parts[3]==task['hash'])
                        if not owned and not (task['purpose']=='source_sample' and
                                root==(self.engine.source_review.root/'samples'/task['hash']).resolve()):continue
                        path=(root/f['name']).resolve()
                        if not path.is_relative_to(root):continue
                    row={k:v for k,v in row.items() if k!='_derived_purpose'}
                    row['path']=str(path)
                except (ValueError,KeyError,TypeError,OSError):continue
            if fidelity_blocker(self.db,title,task['selection'] if task else None):continue
            if task and task['status'] not in ('complete','linked'):continue
            path=Path(row['path'])
            if not path.is_file():continue
            if task:
                f=json.loads(task['selection'])['selected'].get(str(ep))
                if not f or path.stat().st_size!=f['size']:continue
            from .source_blacklist import media_blocker
            if media_blocker(self.db,row):continue
            fp=lab_core().fingerprint(path,path.stat().st_size)
            if fp in seen:
                if row.get('candidate_id'):seen[fp]['candidate_id']=row['candidate_id']
                continue
            entry={**row,'title':title,'size':path.stat().st_size,'fingerprint':fp}
            seen[fp]=entry;out.append(entry)
        for item in out:
            sample=self.db.one('SELECT evidence FROM source_samples WHERE candidate_id=?',(item.get('candidate_id'),)) if item.get('candidate_id') else None
            evidence=json.loads(sample['evidence']) if sample else {}
            item['inventory_revision']=lab_core().digest(lab_core().canonical({'candidate_id':item.get('candidate_id'),
                'tracks':evidence.get('tracks',[]),'ocr':evidence.get('ocr',{})}).encode())
        return out

    def collect(self,item):
        sid,ep=item['show_id'],item['episode'];self.ensure(sid,ep)
        archive=archive_for(self.db);season=self.db.mapping(sid).get('season',1)
        metadata={'source_key':item.get('hash') or 'verified-library','release':item['title'],
                  'media_fingerprint':item['fingerprint'],'candidate_id':item.get('candidate_id'),
                  'publisher':family(item['title'])['group'],'platform':family(item['title'])['provider'],
                  'translator':None,'origin':'embedded','rights':{'redistribution':'unknown'}}
        tracks=self.sub.tracks(item);ids=[];errors=[]
        for track in tracks:
            if not track['supported']:continue
            language=track.get('language','und').lower()
            if language not in lab_core().ZH|lab_core().JA:continue
            try:
                fmt='ass' if track['codec'] in ('ass','ssa') else 'vtt'
                path=self.sub.extract(item,track['index'],fmt)
                ids.append(archive.add_base(sid,season,ep,path,{**metadata,'track':track['index'],'language':language}))
            except (ValueError,OSError) as ex:errors.append(str(ex)[:200])
        # Already verified sidecar tracks/OCR remain identifiable, estimated OCR is not native timing.
        if item.get('candidate_id'):
            sample=self.db.one('SELECT evidence FROM source_samples WHERE candidate_id=?',(item['candidate_id'],))
            evidence=json.loads(sample['evidence']) if sample else {}
            for track in evidence.get('tracks',[]):
                index=track.get('index',track.get('track',-1))
                if type(index) is not int or index<100000:continue
                lang=track.get('language','und').lower()
                if lang not in lab_core().ZH|lab_core().JA:
                    # Missing language is a review task, not permission to infer from Han characters.
                    errors.append('sidecar '+str(index)+' language unverified');continue
                # Evidence paths are generated by SourceReview, restricted to its evidence directory.
                directory=self.engine.source_review.root/'evidence'/str(item['candidate_id'])
                path=directory/('track-'+str(index)+'.vtt')
                if not path.is_file():continue
                ids.append(archive.add_base(sid,season,ep,path,{**metadata,'track':index,'language':lang,
                   'origin':'source-review-sidecar','timing_is_estimated':track.get('timing_is_estimated',False)}))
            ocr=evidence.get('ocr',{})
            if ocr.get('status')=='ok':
                path=self.engine.source_review.root/'evidence'/str(item['candidate_id'])/'ocr.json'
                if path.is_file():
                    ids.append(archive.add_base(sid,season,ep,path,{**metadata,'track':'ocr','language':'chi',
                         'origin':'windows-chinese-ocr','timing_is_estimated':True,'uncertainty':ocr.get('note')}))
        archive.register(sid,season,ep)
        if item.get('hash'):
            task=self.db.one('SELECT selection,save_path FROM tasks WHERE hash=?',(item['hash'],))
            if task:
                selection=json.loads(task['selection']);root=Path(task['save_path']).resolve()
                for f in selection['files']:
                    if f['index'] not in selection['ids'] or Path(f['name']).suffix.lower() not in ('.ass','.ssa','.srt','.vtt'):continue
                    path=(root/f['name']).resolve()
                    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size!=f['size']:continue
                    hint=Path(f['name']).name.lower()
                    lang='jpn' if re.search(r'(?<![a-z])(?:ja|jpn|jp)(?![a-z])|日语|日文|日本語',hint) else 'chi' if re.search(r'(?<![a-z])(?:chs|cht|chi|zho|zh)(?![a-z])|简体|繁体|繁體|中文',hint) else 'und'
                    if lang=='und':errors.append('sidecar '+str(f['index'])+' language unverified');continue
                    ids.append(archive.add_base(sid,season,ep,path,{**metadata,'track':100000+f['index'],'language':lang,'origin':'original-sidecar'}))
        if item['fingerprint']!=lab_core().fingerprint(item['path'],item['size']):raise ValueError('字幕归档期间媒体发生变化')
        payload={'draft_ids':ids,'tracks':len(tracks),'errors':errors,'title':item['title'],'inventory_revision':item.get('inventory_revision')}
        self.db.execute('INSERT OR REPLACE INTO subtitle_studio_media VALUES(?,?,?,?,?,?)',
            (item['fingerprint'],sid,ep,item['path'],json.dumps(payload,ensure_ascii=False),time.time()))
        self.refresh_job(sid,ep,archive)
        return payload

    def refresh_job(self,sid,ep,archive=None):
        archive=archive or archive_for(self.db);season=self.db.mapping(sid).get('season',1)
        bases=[b for b in archive.bases(sid,season,ep) if not fidelity_blocker(self.db,b['metadata'].get('release',''))]
        # A responsible episode chat freezes its editorial input. New immutable
        # drafts remain discoverable for V2 without a background refresh writing
        # into that chat's workbook or silently changing its selected target.
        owned=self.db.one("""SELECT 1 FROM subtitle_delivery_jobs WHERE show_id=? AND episode=?
            AND kind='episode' AND (thread_id<>'' OR dispatch_state IN ('committing','unknown','acknowledged')
            OR lease_expires>?)""",(sid,ep,time.time()))
        if self.db.get('subtitle_delivery_policy',{}).get('enabled') and owned:
            previous=self.db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?',(sid,ep))
            if previous:
                old=json.loads(previous['payload'])
                from .subtitle_targets import head
                frozen=head(self.db,sid,ep)
                if frozen and (old.get('research_target_id')!=frozen['id']):
                    # The controlled freeze already created this new workbook.
                    # Update metadata only; never refresh its editorial bytes.
                    old.update(target=frozen['payload']['target'],workbook=frozen['payload']['workbook'],
                               research_target_id=frozen['id'])
                    self.db.execute('UPDATE subtitle_studio_jobs SET payload=?,updated=? WHERE show_id=? AND episode=?',
                        (json.dumps(old,ensure_ascii=False),time.time(),sid,ep))
                observed={
                    'draft_ids':sorted(b['id'] for b in bases),'target':old.get('target'),
                    'workbook':old.get('workbook')}
                key=f'subtitle_delivery_pending_inventory_{sid}_{ep}'
                prior=self.db.get(key,{})
                if {k:prior.get(k) for k in observed}!=observed:
                    self.db.set(key,{**observed,'observed_at':time.time()})
                return
        from .subtitle_targets import head
        frozen=head(self.db,sid,ep)
        existing=({'path':frozen['payload']['target']['path']} if frozen else self.db.one("SELECT path FROM episodes WHERE show_id=? AND episode=? AND status='complete'",(sid,ep)))
        target=None;workbook=None
        if existing and Path(existing['path']).is_file():
            p=Path(existing['path']);target={'path':str(p),'size':p.stat().st_size,'fingerprint':lab_core().fingerprint(p)}
            if frozen and target!=frozen['payload']['target']:raise ValueError('冻结媒体已变化，保留旧目标及工作稿等待重新评审')
            native=([b for b in bases if b['id']==frozen['payload']['base_id']] if frozen else [b for b in bases if b['language'] in lab_core().ZH and b['metadata'].get('media_fingerprint')==target['fingerprint'] and not b['metadata']['timing_is_estimated'] and not b['metadata'].get('mixed_language_suspected')])
            if native:
                draft=native[0]
                previous_job=self.db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?',(sid,ep))
                previous_path=json.loads(previous_job['payload']).get('workbook') if previous_job else None
                workpath=Path(frozen['payload']['workbook']) if frozen else Path(previous_path) if previous_path else archive.workbook_path(sid,season,ep,target,draft['id'])
                old=json.loads(workpath.read_text(encoding='utf-8')) if workpath.exists() else None
                if old and old['target']['fingerprint']!=target['fingerprint']:old=None
                fresh_ids={b['id'] for b in bases}
                if old and old['target']['fingerprint']==target['fingerprint'] and old['base_id'] in fresh_ids:
                    draft=next(b for b in bases if b['id']==old['base_id'])
                changed=not old or set(old.get('inventory',[]))!=fresh_ids
                workbook=archive.workbook(sid,season,ep,draft['id'],target,refresh=changed)
        payload={'season':season,'base_count':len(bases),'chinese_texts':len({b['text_hash'] for b in bases if b['language'] in lab_core().ZH and not b['metadata']['timing_is_estimated']}),
          'estimated_bases':sum(b['metadata']['timing_is_estimated'] for b in bases),
          'japanese_drafts':sum(b['language'] in lab_core().JA for b in bases),'draft_ids':[b['id'] for b in bases],
          'workbook':str(workbook) if workbook else None,'target':target,
          'research_target_id':frozen['id'] if frozen else None,
          'note':'待逐句校订及实际画面验收；同文档案不是独立译者。'}
        previous=self.db.one('SELECT payload,state FROM subtitle_studio_jobs WHERE show_id=? AND episode=?',(sid,ep))
        state='waiting_review' if workbook else 'waiting_alignment' if bases else 'waiting_drafts'
        if previous and json.loads(previous['payload'])==payload:return
        self.db.execute('''INSERT INTO subtitle_studio_jobs VALUES(?,?,?,?,'',?) ON CONFLICT(show_id,episode)
            DO UPDATE SET state=excluded.state,payload=excluded.payload,error='',updated=excluded.updated''',
            (sid,ep,state,json.dumps(payload,ensure_ascii=False),time.time()))
        directory=archive.root/'episodes'/lab_core().episode_key(sid,season,ep)
        textgroups={}
        for b in bases:textgroups.setdefault(b['text_hash'],[]).append(b['id'])
        lab_core().write_json(directory/'comparison.json',{'show_id':sid,'season':season,'episode':ep,
          'bases':[{'id':b['id'],'language':b['language'],'cue_count':b['metadata']['cue_count'],'raw_hash':b['raw_hash'],
                    'text_hash':b['text_hash'],'publisher':b['metadata'].get('publisher'),'platform':b['metadata'].get('platform'),
                    'origin':b['metadata'].get('origin','unknown'),'timing_is_estimated':b['metadata']['timing_is_estimated']} for b in bases],
          'identical_text_groups':[ids for ids in textgroups.values() if len(ids)>1],
          'decisions':'pending full semantic/scene/timing review','workbook':str(workbook) if workbook else None})

    def tick(self):
        if not self.db.get('subtitle_studio_enabled',False) or not self.lock.acquire(False):return
        def worker():
            try:
                count=0
                for item in self.media():
                    if self.engine.stop_event.is_set():break
                    prior=self.db.one('SELECT payload FROM subtitle_studio_media WHERE fingerprint=?',(item['fingerprint'],))
                    if prior and json.loads(prior['payload']).get('inventory_revision')==item['inventory_revision']:continue
                    try:self.collect(item)
                    except Exception as ex:
                        self.db.event('subtitle-studio',safe_error(ex),'warning')
                    count+=1
                    if count>=2:break
                if self.db.get('subtitle_delivery_policy',{}).get('enabled'):
                    try:self.delivery.refresh()
                    except Exception as ex:  # keep the worker alive and the failure visible
                        self.db.event('subtitle-delivery',safe_error(ex),'error')
                        self.db.set('subtitle_studio_health',{'time':time.time(),'ok':False,'error':safe_error(ex)});return
                self.db.set('subtitle_studio_health',{'time':time.time(),'ok':True})
            finally:self.lock.release()
        threading.Thread(target=worker,name='subtitle-studio-archive',daemon=True).start()

    def transcribe(self,item,model='large-v3-turbo'):
        """Generate an explicitly unreviewed Japanese fallback for this exact media."""
        self.ensure(item['show_id'],item['episode'])
        archive=archive_for(self.db);season=self.db.mapping(item['show_id']).get('season',1)
        bases=archive.bases(item['show_id'],season,item['episode'])
        if any(b['language'] in lab_core().JA and b['metadata'].get('media_fingerprint')==item['fingerprint'] for b in bases):return None
        probe=json.loads(self.sub.run([self.sub.tool('ffprobe'),'-v','error','-select_streams','a','-show_entries',
                'stream=index:stream_tags=language','-of','json',item['path']],30))
        audio=[s['index'] for s in probe.get('streams',[]) if s.get('tags',{}).get('language','').lower() in lab_core().JA]
        if len(audio)!=1:raise ValueError('需要独立核验唯一日语声轨，未自行猜声轨')
        runtime=Path('D:/SubtitleSkillLab/.venv/Scripts/python.exe')
        if not runtime.is_file():raise ValueError('本机字幕研究ASR环境不可用')
        out=self.root/'asr'/item['fingerprint']
        import subprocess
        result=subprocess.run([str(runtime),'-X','utf8','D:/SubtitleSkillLab/scripts/transcribe_media.py',
                '--media',item['path'],'--audio-stream',str(audio[0]),'--ffmpeg',self.sub.tool('ffmpeg'),
                '--output',str(out),'--model',model],capture_output=True,timeout=3600,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('本地日语转写失败，保留工作稿；不上传媒体或伪造日语')
        self.ensure(item['show_id'],item['episode'])
        record=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
        if record['target_fingerprint']!=item['fingerprint']:raise ValueError('ASR媒体指纹已变化')
        draft=archive.add_base(item['show_id'],season,item['episode'],out/'japanese-candidate.vtt',
          {'source_key':'local-asr-'+model,'track':audio[0],'language':'jpn','release':item['title'],
           'media_fingerprint':item['fingerprint'],'origin':'local-asr','model':model,
           'manifest':str(out/'manifest.json'),'timing_is_estimated':True,'uncertainty':'逐句听核，尤其人物名、重叠对白、拟声与音乐。'})
        self.refresh_job(item['show_id'],item['episode'],archive)
        return draft

    def overview(self,include_history=False):
        jobs=[];coverage={};accepted=0
        fidelity=self.db.get('source_fidelity','');blacklist=self.db.get(BLACKLIST_KEY,[])
        blocked=lambda title:blacklist_blocker(blacklist,title) or preference_blocker(fidelity,title)
        if self.root.exists():
            archive=archive_for(self.db)
            with archive.connect() as con:
                rows=con.execute('SELECT episode_key,payload FROM discoveries').fetchall()
            for discovery in rows:
                record=json.loads(discovery['payload'])
                if blocked(record['title']):continue
                sid=int(discovery['episode_key'].split('/')[0]);publisher=record['publisher']
                coverage.setdefault((sid,publisher),set()).add(record['episode'])
        for row in self.db.rows('SELECT * FROM subtitle_studio_jobs ORDER BY show_id,episode'):
            if not include_history and not production_wanted(self.db,row['show_id']):continue
            row['title']=self.db.show(row['show_id'])['title'];row['payload']=json.loads(row['payload'])
            archive=archive_for(self.db)
            valid=[b for b in archive.bases(row['show_id'],row['payload']['season'],row['episode']) if not blocked(b['metadata'].get('release',''))]
            row['payload'].update(base_count=len(valid),chinese_texts=len({b['text_hash'] for b in valid if b['language'] in lab_core().ZH and not b['metadata']['timing_is_estimated']}),japanese_drafts=sum(b['language'] in lab_core().JA for b in valid),estimated_bases=sum(b['metadata']['timing_is_estimated'] for b in valid))
            row['payload']['source_coverage']=[{'publisher':name,'first_seen_episode':min(eps),'last_seen_episode':max(eps),
                 'available_this_episode':row['episode'] in eps} for (sid,name),eps in sorted(coverage.items()) if sid==row['show_id']]
            if row['payload']['target'] and authored(self.db,{**row['payload']['target'],'show_id':row['show_id'],'episode':row['episode']}):
                accepted+=1
                if not valid or set(row['payload']['draft_ids'])=={b['id'] for b in valid}:row['state']='accepted'
            jobs.append(row)
        return {'enabled':self.db.get('subtitle_studio_enabled',False),'jobs':jobs,
          'summary':{'episodes':len(jobs),'bases':sum(j['payload']['base_count'] for j in jobs),
                     'accepted':accepted},
          'health':self.db.get('subtitle_studio_health',{}),'skill':'video-subtitle-studio'}

    def library(self):
        """Read-only catalogue: editorial progress never implies viewing acceptance."""
        from .webplayer import WebPlayer
        jobs=self.db.rows('SELECT * FROM subtitle_studio_jobs ORDER BY show_id,episode')
        if not jobs:return {'entries':[],'summary':{'episodes':0,'shows':0,'accepted':0}}
        player=WebPlayer(self.db);media=player.media();archive=archive_for(self.db)
        fidelity=self.db.get('source_fidelity','');blacklist=self.db.get(BLACKLIST_KEY,[])
        entries=[]
        for job in jobs:
            sid,ep=job['show_id'],job['episode'];payload=json.loads(job['payload'])
            item=next((m for m in media.values() if m.get('show_id')==sid and m.get('episode')==ep),None)
            selected=None;target_matches_current=False
            if item:
                try:
                    current=player.item(item['id']);selected=authored(self.db,current)
                    target_matches_current=bool(payload.get('target') and payload['target'].get('fingerprint')==lab_core().fingerprint(current['path'],current['size']))
                except (ValueError,OSError):pass
            progress=None;workpath=Path(payload.get('workbook') or '')
            try:
                if workpath.is_file() and workpath.resolve().is_relative_to(archive.root.resolve()):
                    book=json.loads(workpath.read_text(encoding='utf-8'));cues=book.get('cues',[])
                    progress={'total':len(cues),'reviewed':sum(c.get('status') in ('translation_reviewed','accepted') for c in cues)}
            except (OSError,ValueError,TypeError):pass
            with archive.connect() as con:
                builds=con.execute('SELECT id,accepted,created FROM builds WHERE episode_key=? ORDER BY created DESC',
                    (lab_core().episode_key(sid,payload['season'],ep),)).fetchall()
                drafts=con.execute('SELECT metadata FROM drafts WHERE episode_key=?',
                    (lab_core().episode_key(sid,payload['season'],ep),)).fetchall()
            releases=[json.loads(d['metadata']).get('release','') for d in drafts]
            base_count=sum(not (blacklist_blocker(blacklist,title) or preference_blocker(fidelity,title)) for title in releases)
            versions=[{'id':b['id'],'accepted':bool(b['accepted']),'created':b['created'],
                       'current':bool(selected and selected['id']==b['id'])} for b in builds]
            entries.append({'show_id':sid,'title':self.db.show(sid)['title'],'season':payload['season'],'episode':ep,
                'state':'accepted' if selected else job['state'] if job['state']!='accepted' else 'waiting_review',
                'progress':progress,'base_count':base_count,'versions':versions,
                'media_id':item['id'] if item else None,'video_file':item['name'] if item else None,
                'build_id':selected['id'] if selected else None,
                'subtitle_file':Path(selected['files']['bilingual.ass']['path']).name if selected else None,
                'editable':bool(progress is not None and production_wanted(self.db,sid)),
                'active':production_wanted(self.db,sid),
                'target_matches_current':target_matches_current,
                'updated':job['updated']})
        return {'entries':entries,'summary':{'episodes':len(entries),
            'shows':len({j['show_id'] for j in entries}),'accepted':sum(bool(j['build_id']) for j in entries)}}


def register_subtitle_studio(app,engine):
    studio=engine.subtitle_studio
    @app.get('/api/subtitle-delivery')
    def delivery_queue():return studio.delivery.snapshot()
    @app.post('/api/subtitle-delivery/refresh')
    def delivery_refresh():return studio.delivery.refresh()
    @app.post('/api/subtitle-delivery/report')
    def delivery_report(p:dict):
        if p.get('result')=='blocked':
            result=studio.delivery.record_blocked(p['job_id'],p['thread_id'],p.get('reasons',[]),
                owner_stopped=p.get('owner_stopped') is True)
        elif p.get('result')=='accepted':
            result=studio.delivery.record_accepted(p['job_id'],p['thread_id'],p['build_id'])
        else:raise ValueError('只允许真实blocked或经宿主复核的完整成品回报')
        return {k:v for k,v in result.items() if k not in ('lease_token','dispatch_token')}
    @app.get('/api/subtitle-studio/{sid}/{ep}/production-target')
    def production_target(sid:int,ep:int):
        from .subtitle_targets import head
        studio.ensure(sid,ep);record=head(studio.db,sid,ep)
        return {'target_id':record['id'] if record else None,'fingerprint':record['payload']['target']['fingerprint'] if record else None}
    @app.post('/api/subtitle-studio/{sid}/{ep}/production-target')
    def freeze_target(sid:int,ep:int,p:dict):return studio.production_targets.freeze(sid,ep,p)
    @app.post('/api/subtitle-studio/{sid}/{ep}/publish')
    def publish(sid:int,ep:int,p:dict):return studio.production_targets.publish(sid,ep,p)
    @app.get('/api/viewing-workflow')
    def workflow():return queue_snapshot(studio.db)
    @app.post('/api/subtitle-studio/{sid}/{ep}/acquire')
    def acquire(sid:int,ep:int,p:dict):
        studio.ensure(sid,ep,True)
        ids=p.get('candidate_ids');role=p.get('role','clean_base')
        if role not in ('clean_base','subtitle_reference'):raise ValueError('未知研究角色')
        if not isinstance(ids,list) or not ids or len(ids)>2 or any(type(cid) is not int for cid in ids):
            raise ValueError('每批须明确选择一到两个已登记候选')
        available={c['id']:c for c in studio.candidates(sid) if c['episode']==ep}
        if set(ids)-available.keys():raise ValueError('候选身份、季集、来源或授权不符')
        for cid in dict.fromkeys(ids):
            if (ep==1 and role=='clean_base'
                    and engine.source_review.policy(sid).get('mode')!='subtitle_only'):
                engine.source_review.start(sid,[cid])
            else:studio.queue(available[cid],role)
        return {'ok':True,'show_id':sid,'episode':ep,'role':role}
    @app.get('/api/subtitle-studio')
    def overview():return studio.overview()
    @app.get('/subtitle-studio')
    def page():return FileResponse(Path(__file__).resolve().parents[1]/'static/subtitle-studio.html')
    @app.get('/api/subtitle-library')
    def library():return studio.library()
    @app.get('/subtitle-library')
    def library_page():return FileResponse(Path(__file__).resolve().parents[1]/'static/subtitle-library.html')
    from .subtitle_queue import production_queue
    @app.get('/api/subtitle-queue')
    def subtitle_queue(show:int|None=None):return production_queue(app.state.db,show=show)
    @app.get('/subtitle-queue')
    def subtitle_queue_page():return FileResponse(Path(__file__).resolve().parents[1]/'static/subtitle-queue.html')
    @app.get('/api/subtitle-studio/{sid}/{ep}/workbook')
    def workbook(sid:int,ep:int):
        studio.ensure(sid,ep)
        row=studio.db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=? AND episode=?',(sid,ep))
        path=Path(json.loads(row['payload']).get('workbook') or '') if row else None
        if not path or not path.is_file():raise ValueError('该集暂无可核对工作稿')
        return FileResponse(path,media_type='application/json',filename=f'{sid}-E{ep:03d}-workbook.json')
