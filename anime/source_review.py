"""First-episode research and reviewed source failover, separate from viewing state.

Only measured facts get automatic metrics. Translation/visual scores require a
reviewer, evidence and confidence. No API key or unattended model call is implied.
"""
import hashlib,json,math,os,re,shutil,subprocess,threading,time
from pathlib import Path
from urllib.parse import urlencode
from fastapi.responses import FileResponse
from .download_policy import require_submission_ready
from .fidelity import fidelity_blocker,processing_flags,preference_blocker
from .source_blacklist import KEY as BLACKLIST_KEY,blacklist_blocker,media_blocker
from .net import safe_error
from .releases import Rejected,bdecode,validate_title,select_files,norm
from .subtitles import Subtitles
from .subtitle_history import SubtitleHistory,validate_findings
from .native_indexes import register_sources,candidate_mapping,clean_blocker,acquisition_priority


def family(title):
    group=re.match(r'^\s*[\[【]([^\]】]+)',title)
    group=group[1].strip() if group else '未标明发布者'
    provider=next((p for p in ('NETFLIX','ABEMA','B-GLOBAL','BAHA','CR')
                   if re.search(r'(?i)(?<![a-z])'+re.escape(p)+r'(?![a-z])',title)), '未标明片源')
    res=re.search(r'(?i)(2160|1080|720)(?:p|i)|(?:3840x(2160)|1920x(1080)|1280x(720))',title)
    resolution=next((x for x in res.groups() if x), '') if res else '未知'
    upscale=bool(re.search(r'(?i)AI\s*2160|upscal|超分|放大|AI修复',title))
    mode='soft' if re.search(r'内封|內封|外挂|外掛|softsub',title,re.I) else 'hard' if re.search(r'内嵌|內嵌|MP4|hardsub',title,re.I) else 'unknown'
    label=f'{group} · {provider} · {resolution}'+('（放大）' if upscale else '')+' · '+mode
    return {'key':hashlib.sha256(norm(label).encode()).hexdigest()[:20], 'label':label,
            'group':group,'provider':provider,'resolution':resolution,'upscale':upscale,'mode':mode,
            'processing':processing_flags(title)}


def cues(text):
    result=[]
    for block in re.split(r'\r?\n\s*\r?\n',text):
        match=re.search(r'((?:\d+:)?\d+:\d+\.\d+)\s*-->\s*((?:\d+:)?\d+:\d+\.\d+)[^\n]*\n(.*)',block,re.S)
        if not match:continue
        def seconds(t):
            parts=t.split(':');h,m,s=parts if len(parts)==3 else ['0']+parts
            return int(h)*3600+int(m)*60+float(s)
        body=re.sub(r'<[^>]*>','',match[3]).strip()
        if body:result.append({'start':seconds(match[1]),'end':seconds(match[2]),'text':body})
    return result


def subtitle_metrics(rows,duration,language=''):
    # Readability/timing warnings are not a translation-quality score.
    zh=sum(bool(re.search(r'[\u3400-\u9fff]',r['text'])) for r in rows)
    # Japanese contains Han characters too. A declared Japanese track is not Chinese.
    return {'cues':len(rows),'han_cues':zh,'chinese_cues':0 if language.lower() in ('jpn','ja','ja-jp') else zh,
        'invalid_timing':sum(r['end']<=r['start'] or r['start']<0 or r['end']>duration+3 for r in rows),
        'fast_cues':sum(len(re.sub(r'\s','',r['text']))/max(.1,r['end']-r['start'])>18 for r in rows),
        'long_lines':sum(any(len(line)>42 for line in r['text'].splitlines()) for r in rows),
        'replacement_characters':sum(r['text'].count('\ufffd') for r in rows),
        'translation_score':None,'timing_is_estimated':False}


class SourceReview:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.sub=Subtitles(self.db)
        self.root=self.db.path.parent/'source-review';self.lock=threading.Lock()
        self.processing=set()
        self.history=SubtitleHistory(self)
    def policy(self,sid):
        row=self.db.one('SELECT config FROM source_policies WHERE show_id=?',(sid,))
        return json.loads(row['config']) if row else {}
    def save(self,sid,config):
        self.db.execute('''INSERT INTO source_policies VALUES(?,?,?) ON CONFLICT(show_id)
            DO UPDATE SET config=excluded.config,updated=excluded.updated''',(sid,json.dumps(config,ensure_ascii=False),time.time()))
    def ensure(self,sid,submission=True):
        if not self.db.review_wanted(sid):raise ValueError('只评审用户已加入想看、试看或在追的作品')
        if not self.db.eligible(sid):raise ValueError('需要已核验并授权的作品；弃番、暂搁、看完及清理保留禁止取样')
        if not self.db.allowed(sid,1):raise ValueError('首集不在授权范围；旧番不会自动回补首集')
        if submission:require_submission_ready(self.db)
    def candidates(self,sid):
        return self.filter_candidates(self.db.rows('''SELECT candidates.* FROM candidates JOIN sources ON sources.id=candidates.source_id
            WHERE candidates.show_id=? AND sources.enabled=1 ORDER BY candidates.id''',(sid,)),
            self.db.mapping(sid),self.db.get('source_fidelity',''),self.db.get(BLACKLIST_KEY,[]))
    def discover_sources(self):
        """Search both indexes for the existing personally authorized review cohort.

        Additional search feeds do not change the independently confirmed mapping.
        Release titles and torrent files still pass the ordinary season/episode gates.
        """
        for row in self.db.rows('SELECT show_id FROM source_policies'):
            sid=row['show_id']
            if self.policy(sid).get('mode') not in ('sampling','selected','subtitle_only'):continue
            try:self.ensure(sid,submission=False)
            except ValueError:continue
            with self.db.gate:
                if self.policy(sid).get('mode') not in ('sampling','selected','subtitle_only'):continue
                try:self.ensure(sid,submission=False)
                except ValueError:continue
                show=self.db.show(sid);mapping=self.db.mapping(sid)
                register_sources(self.db,sid)
                from .general_indexes import register_general_sources
                register_general_sources(self.db,sid)
                # A sequel title search needs explicit season proof; do not inherit
                # Mikan's subject-ID proof for unnumbered search results.
                if mapping.get('season',1)>1 and not mapping.get('require_season'):continue
                terms=[show['title'],show['original']]
                terms.extend(a for a in mapping.get('aliases',[]) if re.fullmatch(r'[A-Za-z0-9 :\-]+',a))
                terms=list(dict.fromkeys(t.strip() for t in terms if isinstance(t,str) and 3<=len(t.strip())<=200))[:3]
                with self.db.connect() as con:
                    for term in terms:
                        query=urlencode({'keyword':term})
                        url='https://share.dmhy.org/topics/rss/rss.xml?'+query
                        # Preserve explicit source toggles and other subjects' ownership.
                        if con.execute('SELECT 1 FROM sources WHERE url=?',(url,)).fetchone():continue
                        con.execute('INSERT INTO sources(show_id,kind,url,archive_url) VALUES(?,?,?,?)',
                            (sid,'dmhy',url,'https://share.dmhy.org/topics/list?'+query))
    @staticmethod
    def filter_candidates(rows,mapping,fidelity,blacklist=()):
        out=[]
        for c in rows:
            try:
                eps=validate_title(c['title'],candidate_mapping(mapping,c),{},False)
                if eps!=[1]:continue
            except Rejected:continue
            f=family(c['title'])
            if blacklist_blocker(blacklist,c['title']) or preference_blocker(fidelity,c['title']):continue
            out.append({**c,'family':f})
        return out
    def status(self,sid):
        # Bound database work independently of sample/candidate count. No shared cache:
        # every request sees policy, source toggles, tasks and reviews from one snapshot.
        with self.db.connect() as c:
            c.execute('BEGIN')
            show=c.execute('SELECT mapping FROM shows WHERE id=?',(sid,)).fetchone()
            if not show:raise ValueError('作品不存在')
            row=c.execute('SELECT config FROM source_policies WHERE show_id=?',(sid,)).fetchone()
            cfg=json.loads(row['config']) if row else {}
            preferences={r['key']:json.loads(r['value']) for r in c.execute(
                'SELECT key,value FROM kv WHERE key IN (?,?)',('source_fidelity',BLACKLIST_KEY))}
            fidelity=preferences.get('source_fidelity','')
            blacklist=preferences.get(BLACKLIST_KEY,[])
            samples=[dict(r) for r in c.execute('''SELECT s.*,c.title,c.published,
                t.hash task_hash,t.selection task_selection,t.status task_status,t.error task_error,t.provider task_provider
                FROM source_samples s JOIN candidates c ON c.id=s.candidate_id
                LEFT JOIN tasks t ON t.hash=s.hash WHERE s.show_id=? ORDER BY s.candidate_id''',(sid,))]
            rows=[dict(r) for r in c.execute('''SELECT candidates.* FROM candidates JOIN sources ON sources.id=candidates.source_id
                WHERE candidates.show_id=? AND sources.enabled=1 ORDER BY candidates.id''',(sid,))]
        for s in samples:
            s['evidence']=json.loads(s['evidence']);s['review']=json.loads(s['review'])
            s['family_info']=family(s['title'])
            selection=s.pop('task_selection')
            s['source_blocker']=blacklist_blocker(blacklist,s['title'],selection)
            s['processing_blocker']=s['source_blocker'] or preference_blocker(fidelity,s['title'],selection)
            s['release_delay_hours']=self.policy_delay(cfg,s['published'])
            task_hash=s.pop('task_hash')
            task={key:s.pop('task_'+key) for key in ('status','error','provider')}
            if s['hash']:
                s['download']=task if task_hash is not None else {}
            # Paths are useful only on the local machine; never accept arbitrary paths for analysis.
        recommendations=sorted([s for s in samples if self.reviewed(s) and not s['processing_blocker']],key=lambda s:(
            -s['review']['visual'],s['release_delay_hours'] if s['release_delay_hours'] is not None else math.inf,
            -s['review']['translation']))
        candidates=self.filter_candidates(rows,json.loads(show['mapping']),fidelity,blacklist)
        families={c['family']['key'] for c in candidates};sampled={s['family'] for s in samples}
        return {'policy':cfg,'candidates':candidates,'samples':samples,
                'sampling_limit':{'max_families':12,'max_gib':20,'available_families':len(families),
                                  'sampled_families':len(sampled),'remaining_families':len(families-sampled)},
                'recommendation':[s['family'] for s in recommendations],
                'fidelity':fidelity,'publisher_blacklist':blacklist,
                'priority':['原片保真','实测画质','发布延迟','字幕翻译'] if fidelity=='original' else ['实测画质','发布延迟','字幕翻译'],
                'note':'首集评测需核对实际画面、日语原声/文本及原作术语；未知项不打分。评测副本不计已看、不自动删除。'}
    def delay(self,sid,published,episode=1):
        return self.policy_delay(self.policy(sid),published,episode)
    @staticmethod
    def policy_delay(cfg,published,episode=1):
        air=cfg.get('airing_epoch')
        return round((published-air-(episode-1)*cfg.get('interval_hours',168)*3600)/3600,2) if published and air else None
    def overview(self):
        """Read-only coverage of every enrolled series, including unfinished reviews."""
        shows=[]
        for row in self.db.rows('SELECT show_id,config FROM source_policies ORDER BY show_id'):
            sid=row['show_id'];show=self.db.show(sid);cfg=json.loads(row['config'])
            if not show or not self.db.review_wanted(sid):continue
            counts={};reviewed=excluded=0;times=[];labels={}
            for s in self.db.rows('''SELECT s.*,c.title FROM source_samples s JOIN candidates c
                ON c.id=s.candidate_id WHERE s.show_id=?''',(sid,)):
                counts[s['state']]=counts.get(s['state'],0)+1
                s['review']=json.loads(s['review']);evidence=json.loads(s['evidence'])
                labels[s['family']]=family(s['title'])['label']
                task=self.db.one('SELECT selection FROM tasks WHERE hash=?',(s['hash'],))
                blocked=fidelity_blocker(self.db,s['title'],task['selection'] if task else None)
                excluded+=int(s['review'].get('decision')=='excluded' or bool(blocked))
                reviewed+=int(self.reviewed(s) and not blocked)
                observed=evidence.get('completion_observed_at')
                if not observed and s['state']=='ready':
                    packet=self.root/'evidence'/str(s['candidate_id'])/'packet.json'
                    if packet.is_file():observed=packet.stat().st_mtime
                if observed:times.append(observed)
            eligible=self.db.allowed(sid,1)
            pending=sum(counts.get(k,0) for k in ('preparing','queued','analyzing'))
            errors=sum(counts.get(k,0) for k in ('error','analysis_error'))
            stage='selected' if cfg.get('mode')=='selected' else 'waiting_samples' if pending else 'waiting_review' if counts.get('ready') else 'waiting_source'
            if not eligible:stage='protected'
            shows.append({'show_id':sid,'title':show['title'],'quarter':show['quarter'],
                'season':self.db.mapping(sid).get('season'),'mode':cfg.get('mode'), 'eligible':eligible,'stage':stage,
                'samples':counts,'ready_samples':counts.get('ready',0),'pending_samples':pending,'errored_samples':errors,
                'reviewed_chinese_samples':reviewed,'excluded_samples':excluded,
                'first_completed_observed_at':min(times) if times else None,
                'completion_time_note':'首次确认完成/已有分析包时间，保守计算；不冒充下载器实际完成时刻。',
                'primary':labels.get(cfg.get('primary'),'') if cfg.get('primary') else '',
                'backups':[labels.get(k,k) for k in cfg.get('backups',[])]})
        queue=sorted([s for s in shows if s['eligible'] and s['mode']=='sampling' and s['ready_samples']],
            key=lambda s:(s['first_completed_observed_at'] or math.inf,s['show_id']))
        return {'schema_version':1,'shows':shows,'review_queue':[s['show_id'] for s in queue],
            'summary':{'enrolled':len(shows),'selected':sum(s['mode']=='selected' for s in shows),
                'sampling':sum(s['mode']=='sampling' for s in shows),'protected':sum(not s['eligible'] for s in shows)},
            'note':'仅用户已加入想看、试看或在追并核验授权的新番参与取样、实评与自动选源；季度默认授权不代表想看。记录持续累计，待下载/待评不会当已完成。'}
    def start(self,sid,candidate_ids=None):
        # Continuing shows can join subtitle production without reopening the
        # watched premiere or claiming that a primary source was reviewed.
        if self.policy(sid).get('mode')=='subtitle_only':return
        self.ensure(sid)
        available=self.candidates(sid)
        available=sorted((c for c in available if not clean_blocker(self.db,c)),key=acquisition_priority)
        if candidate_ids is not None:
            if not isinstance(candidate_ids,list) or len(candidate_ids)>12:raise ValueError('最多选择12个首集样本')
            ids={int(i) for i in candidate_ids}
            if ids-{c['id'] for c in available}:raise ValueError('包含未经核验的首集候选')
            available=[c for c in available if c['id'] in ids]
        # One simplified/traditional variant per encoding family, retaining all text tracks in the file.
        chosen={}
        for c in available:
            if c['family']['key'] not in chosen:chosen[c['family']['key']]=c
        with self.db.gate:
            self.ensure(sid)
            existing_rows=self.db.rows('SELECT family,candidate_id FROM source_samples WHERE show_id=?',(sid,))
            existing={s['family'] for s in existing_rows}
            # A later simplified/traditional release must not create a second sample
            # of the same family. Retry the original candidate when it is still available.
            prior={s['family']:s['candidate_id'] for s in existing_rows}
            available_ids={c['id']:c for c in available}
            chosen={key:available_ids[prior[key]] if key in prior and prior[key] in available_ids else c
                    for key,c in chosen.items() if key not in prior or prior[key] in available_ids}
            if candidate_ids is None:
                remaining=max(0,12-len(existing));bounded={}
                for key,c in chosen.items():
                    if key in existing:bounded[key]=c
                    elif remaining:bounded[key]=c;remaining-=1
                chosen=bounded
            if len(existing|set(chosen))>12:raise ValueError('本作品最多12种首集片源；已有样本也计入上限')
            cfg=self.policy(sid)
            # Adding evidence after choosing a source must not suspend its episode downloads.
            if cfg.get('mode')!='selected':
                cfg.update(mode='sampling',primary='',backups=[],wait_hours=cfg.get('wait_hours',24))
                self.save(sid,cfg)
        for c in chosen.values():
            with self.db.gate:
                self.ensure(sid)
                old=self.db.one('SELECT * FROM source_samples WHERE candidate_id=?',(c['id'],))
                if old:
                    if old['state']=='analysis_error':
                        if not self.db.get('play_completed_premiere_enabled',False) or time.time()>=self.db.get('source_analysis_retry_'+str(c['id']),{}).get('next_attempt',0):
                            self.db.execute("UPDATE source_samples SET state='analyzing' WHERE candidate_id=?",(c['id'],))
                    expired_preparation=old['state']=='preparing' and time.time()-old['updated']>180
                    if old['state'] not in ('error','scope_hold') and not expired_preparation:continue
                    self.db.execute('UPDATE source_samples SET updated=? WHERE candidate_id=?',(time.time(),c['id']))
                else:
                    self.db.execute('INSERT INTO source_samples(candidate_id,show_id,family,state,updated) VALUES(?,?,?,\'preparing\',?)',
                        (c['id'],sid,c['family']['key'],time.time()))
            try:self.queue(c)
            except Exception as e:
                self.db.execute("UPDATE source_samples SET state='error',error=?,updated=? WHERE candidate_id=?",
                    (str(e)[:300] if isinstance(e,ValueError) else safe_error(e),time.time(),c['id']))
        self.engine.wake.set()
        return self.status(sid)
    def queue(self,c):
        sid=c['show_id'];self.ensure(sid)
        if clean_blocker(self.db,c):raise ValueError(clean_blocker(self.db,c))
        if fidelity_blocker(self.db,c['title']):raise ValueError(fidelity_blocker(self.db,c['title']))
        thunder=self.db.get('downloader_preference','qbit')=='thunder_first'
        from .download_policy import require_submission_ready
        require_submission_ready(self.db)
        data=self.engine.sources.torrent(c);h,files=bdecode(data)
        expected=re.search(r'(?i)([a-f0-9]{40})\.torrent',c['url'])
        if expected and expected[1].lower()!=h:raise ValueError('样本hash与来源不符')
        from .source_recovery import select_verified_release
        selected,ids=select_verified_release(self.engine,c,files,[1],[1])
        blocked=fidelity_blocker(self.db,c['title'],{'files':files,'ids':ids})
        if blocked:raise ValueError(blocked)
        if len(selected)!=1:raise ValueError('评测仅支持首集单版本')
        with self.db.gate:
            self.ensure(sid);self.engine.check_legacy_owner(sid)
            blocked=fidelity_blocker(self.db,c['title'],{'files':files,'ids':ids})
            if blocked:raise ValueError(blocked)
            old=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            if old:
                if old['show_id']!=sid or old['status'] in ('review','cleaned','linked'):raise ValueError('已有hash不能作为所属样本接管')
                self.db.execute("UPDATE source_samples SET hash=?,state='queued',error='',size=?,updated=? WHERE candidate_id=?",
                    (h,selected[1]['size'],time.time(),c['id']));return
            from .viewing_workflow import require_base_slot
            require_base_slot(self.db,c,1)
            if self.engine.thunder.reader.by_hash(h):raise ValueError('迅雷已有此hash，禁止接管或重复下载')
            if not thunder and self.engine.qb.info(h):raise ValueError('qB已有此hash，禁止接管或重复下载')
            if not thunder:self.engine.qb.check_storage()
            total=self.db.one('SELECT coalesce(sum(size),0) n FROM source_samples WHERE show_id=?',(sid,))['n']
            size=sum(f['size'] for f in files if f['index'] in ids)
            if size+total>20*1024**3:raise ValueError('本作品首集评测超过20GiB上限')
            if shutil.disk_usage(self.engine.incomplete.anchor or self.db.path.parent).free<size+self.db.get('minimum_free_gb',10)*1024**3:
                raise ValueError('评测磁盘空间不足')
            stage=self.engine.incomplete/h;dest=self.root/'samples'/h
            if not thunder:
                title=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',self.db.show(sid)['title']).strip('. ')[:80]
                dest=self.engine.library/(title+f' [bgm-{sid}]')/f'Season {self.db.mapping(sid).get("season",1):02}'/'Source samples'/h
            stage.mkdir(parents=True,exist_ok=True);dest.mkdir(parents=True,exist_ok=True)
            (self.engine.cache/(h+'.torrent')).write_bytes(data)
            selection={'files':files,'ids':ids,'selected':{'1':selected[1]}}
            with self.db.connect() as con:
                con.execute('''INSERT INTO tasks(hash,show_id,title,status,selection,save_path,created,updated,provider,staging_path,purpose)
                    VALUES(?,?,?,'intent',?,?,?,?,?,?,'source_sample')''',
                    (h,sid,c['title'],json.dumps(selection,ensure_ascii=False),str(dest),time.time(),time.time(),'thunder' if thunder else 'qbit',str(stage)))
                con.execute("UPDATE source_samples SET hash=?,state='queued',error='',size=?,updated=? WHERE candidate_id=?",(h,selected[1]['size'],time.time(),c['id']))
            task=self.db.one('SELECT * FROM tasks WHERE hash=?',(h,))
            if thunder:self.engine.thunder.enqueue(task)
            else:self.engine.dispatch(task)
    def tick(self):
        with self.lock:
            if self.processing:return
        states="'queued','analyzing','analysis_error'" if self.db.get('play_completed_premiere_enabled',False) else "'queued','analyzing'"
        for s in self.db.rows("SELECT * FROM source_samples s WHERE state IN ("+states+") ORDER BY CASE WHEN EXISTS(SELECT 1 FROM episodes e WHERE e.show_id=s.show_id AND e.episode=1 AND e.status='complete') THEN 1 ELSE 0 END,updated,candidate_id"):
            if not self.db.review_wanted(s['show_id']) or not self.db.allowed(s['show_id'],1):continue
            if s['state']=='analysis_error' and time.time()<self.db.get('source_analysis_retry_'+str(s['candidate_id']),{}).get('next_attempt',0):continue
            t=self.db.one('SELECT * FROM tasks WHERE hash=?',(s['hash'],))
            if not t or t['status'] not in ('complete','linked'):continue
            if t['status']=='linked':
                readonly=self.db.get('thunder_readonly_'+t['hash'],{})
                if readonly.get('mode')!='read_only' or readonly.get('path')!=s['path'] or readonly.get('size')!=s['size']:continue
                p=Path(s['path'])
            else:
                f=json.loads(t['selection'])['selected']['1'];p=Path(t['save_path'])/f['name']
            if not p.is_file() or p.stat().st_size!=s['size']:continue
            evidence=json.loads(s['evidence']);evidence.setdefault('completion_observed_at',time.time())
            self.db.execute("UPDATE source_samples SET path=?,state='analyzing',evidence=?,updated=? WHERE candidate_id=?",
                (str(p),json.dumps(evidence,ensure_ascii=False),time.time(),s['candidate_id']))
            with self.lock:
                if self.processing:return
                self.processing.add(s['candidate_id'])
            threading.Thread(target=self.analyze,args=(s['candidate_id'],),daemon=True,name='source-analysis').start()
            # Bound background work: one media analysis at a time.
            break
    def analyze(self,cid):
        try:
            row=self.db.one('SELECT * FROM source_samples WHERE candidate_id=?',(cid,))
            item={'path':row['path'],'size':row['size']};fingerprint=self.sub.fingerprint(item)
            directory=self.root/'evidence'/str(cid);directory.mkdir(parents=True,exist_ok=True)
            raw=self.sub.run([self.sub.tool('ffprobe'),'-v','error','-protocol_whitelist','file,pipe',
                '-show_entries','format=duration,bit_rate:stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,bit_rate:stream_tags=language,title',
                '-of','json',item['path']],30)
            probe=json.loads(raw);duration=float(probe.get('format',{}).get('duration',0))
            if not 60<duration<7200:raise ValueError('视频时长不可作为正常首集评测')
            evidence={'fingerprint':fingerprint,'probe':probe,'duration':duration,'average_total_bitrate':round(row['size']*8/duration),
                'visual_score':None,'translation_score':None,'tracks':[],'frames':[],
                'caution':'分辨率、编码、码率是事实，不直接证明画质；OCR与机器转写不直接证明翻译正确。'}
            evidence['completion_observed_at']=json.loads(row['evidence']).get('completion_observed_at')
            for track in self.sub.tracks(item):
                record={**track}
                if track['supported']:
                    target=self.sub.extract(item,track['index']);text=target.read_text(encoding='utf-8-sig')
                    dest=directory/f'track-{track["index"]}.vtt';dest.write_text(text,encoding='utf-8')
                    record.update(file=dest.name,metrics=subtitle_metrics(cues(text),duration,track.get('language','')))
                    if track['codec'] in ('ass','ssa'):
                        ass=self.sub.extract(item,track['index'],'ass');(directory/f'track-{track["index"]}.ass').write_bytes(ass.read_bytes())
                evidence['tracks'].append(record)
            task=self.db.one('SELECT * FROM tasks WHERE hash=?',(row['hash'],))
            selection=json.loads(task['selection'])
            for f in selection['files']:
                if f['index'] not in selection['ids'] or Path(f['name']).suffix.lower() not in ('.ass','.ssa','.srt'):continue
                external=(Path(task['save_path'])/f['name']).resolve()
                if not external.is_relative_to(Path(task['save_path']).resolve()) or not external.is_file() or external.stat().st_size!=f['size']:
                    raise ValueError('外挂字幕与完成清单不符')
                text=self.sub.run([self.sub.tool('ffmpeg'),'-nostdin','-v','error','-protocol_whitelist','file,pipe',
                    '-i',str(external),'-f','webvtt','pipe:1'],30).decode('utf-8-sig')
                name=f'track-{100000+f["index"]}.vtt';(directory/name).write_text(text,encoding='utf-8')
                evidence['tracks'].append({'index':100000+f['index'],'label':'外挂字幕','file':name,
                    'metrics':subtitle_metrics(cues(text),duration)})
            # Identical timeline fractions for direct A/B frame comparisons. Preserve original resolution.
            for n,fraction in enumerate((.08,.23,.39,.55,.71,.87)):
                stamp=round(duration*fraction,3);target=directory/f'frame-{n}.png'
                data=self.sub.run([self.sub.tool('ffmpeg'),'-nostdin','-v','error','-protocol_whitelist','file,pipe',
                    '-ss',str(stamp),'-i',item['path'],'-frames:v','1','-f','image2pipe','-c:v','png','pipe:1'],40)
                # A native 4K 16-bit RGB PNG can exceed 32 MiB without being invalid.
                if not data.startswith(b'\x89PNG') or len(data)>64*1024**2:raise ValueError('截图无效或超过64MiB安全上限')
                target.write_bytes(data);evidence['frames'].append({'file':target.name,'seconds':stamp})
            if not any(t.get('metrics',{}).get('chinese_cues',0)>20 for t in evidence['tracks']):
                evidence['ocr']=self.ocr(item,directory,duration)
            if fingerprint!=self.sub.fingerprint(item):raise ValueError('分析期间文件变化')
            (directory/'packet.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
            self.db.execute("UPDATE source_samples SET state='ready',evidence=?,error='',updated=? WHERE candidate_id=?",
                (json.dumps(evidence,ensure_ascii=False),time.time(),cid))
            self.db.set('source_analysis_retry_'+str(cid),{'attempts':0,'next_attempt':0})
        except Exception as e:
            self.db.execute("UPDATE source_samples SET state='analysis_error',error=?,updated=? WHERE candidate_id=?",
                (str(e)[:300] if isinstance(e,ValueError) else safe_error(e),time.time(),cid))
            attempts=self.db.get('source_analysis_retry_'+str(cid),{}).get('attempts',0)+1
            self.db.set('source_analysis_retry_'+str(cid),{'attempts':attempts,'next_attempt':time.time()+min(1800,30*2**min(attempts,6))})
        finally:
            with self.lock:self.processing.discard(cid)
    def ocr(self,item,directory,duration):
        """Local Windows OCR, approximate cues only; no cloud upload or false semantic score."""
        if os.name!='nt':return {'status':'unavailable','note':'本机Windows OCR不可用；压制字幕需人工核对'}
        crops=directory/'ocr-frames';crops.mkdir(exist_ok=True)
        step=max(1,math.ceil(duration/2400))
        try:
            self.sub.run([self.sub.tool('ffmpeg'),'-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',item['path'],
                '-vf',f'fps=1/{step},scale=min(1920\\,iw):-2,crop=iw:ih*0.45:0:ih*0.55',
                '-frames:v','2400','-y',str(crops/'%05d.png')],600)
            script=Path(__file__).resolve().parents[1]/'scripts/Ocr-SourceFrames.ps1'
            result=self.sub.run(['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe','-NoProfile','-File',str(script),
                '-Directory',str(crops)],600)
            data=json.loads(result.decode('utf-8-sig'));data['step_seconds']=step
            data['note']='本机OCR下方45%画面；时间约±采样间隔，可能误字/漏短句/漏画面上方文字，需人工核对'
            if data['status']=='ok':
                rows=[]
                for f in data.pop('frames'):
                    text=f['text'].strip()
                    if not text:continue
                    start=(int(Path(f['file']).stem)-1)*step
                    if rows and norm(rows[-1]['text'])==norm(text) and rows[-1]['end']==start:rows[-1]['end']=start+step
                    else:rows.append({'start':start,'end':start+step,'text':text})
                (directory/'ocr.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                data['cues']=len(rows);data['file']='ocr.json';data['timing_is_estimated']=True
            return data
        except Exception:return {'status':'unavailable','note':'本机OCR失败或中文语言不可用；保留截图，需人工核对'}
    @staticmethod
    def reviewed(s):
        r=s.get('review') or {}
        return s['state']=='ready' and r.get('decision')!='excluded' and r.get('recommendation_eligible') is not False and r.get('fidelity_qualification',{}).get('eligible') is not False and r.get('confidence') in ('medium','high') and r.get('chinese_verified') is True and all(
            type(r.get(k)) in (int,float) and 0<=r[k]<=100 for k in ('visual','translation')) and bool(r.get('evidence'))
    def record_review(self,sid,cid,review):
        with self.db.gate:return self._record_review(sid,cid,review)
    def _record_review(self,sid,cid,review):
        if not self.db.review_wanted(sid):raise ValueError('本作品不在用户想看评审范围，历史记录保留')
        sample=self.db.one('SELECT * FROM source_samples WHERE candidate_id=? AND show_id=?',(cid,sid))
        if not sample or sample['state']!='ready':raise ValueError('需要实际完成并分析的样本')
        if not isinstance(review,dict) or len(json.dumps(review))>30000:raise ValueError('评审记录无效')
        if not review.get('reviewer') or not review.get('original_basis'):raise ValueError('需记录评审者及日语原声/台词/原作依据')
        if 'subtitle_findings' in review:validate_findings(review['subtitle_findings'])
        if review.get('decision')=='excluded':
            if not review.get('evidence'):raise ValueError('排除样本须记录实际核查依据')
            cfg=self.policy(sid)
            if sample['family'] in [cfg.get('primary')]+cfg.get('backups',[]):raise ValueError('请先换掉当前主源或移除此替补，再排除样本')
            # Absence of Chinese is a eligibility finding, not a zero translation score.
            review={k:review[k] for k in ('decision','evidence','original_basis','reviewer')}
            review['chinese_verified']=False
        elif not self.reviewed({**sample,'review':{**review,'recommendation_eligible':True,'fidelity_qualification':{'eligible':True}}}):raise ValueError('需画质/翻译评分、中文字幕核验、证据及中/高置信度')
        review={**review,'fingerprint':json.loads(sample['evidence'])['fingerprint'],'reviewed_at':time.time()}
        previous=json.loads(sample['review'])
        old=self.history.snapshot(sample,previous) if previous.get('evidence') and previous.get('reviewer') else None
        new=self.history.snapshot(sample,review)
        with self.db.connect() as con:
            if old:self.history.append(con,old)
            con.execute('UPDATE source_samples SET review=?,updated=? WHERE candidate_id=?',(json.dumps(review,ensure_ascii=False),time.time(),cid))
            self.history.append(con,new)
    def choose(self,sid,primary,backups,wait_hours=24,replace_first_hash=None):
        with self.db.gate:
            self.ensure(sid)
            if not isinstance(backups,list) or len(backups)>4 or len(set([primary]+backups))!=len([primary]+backups):raise ValueError('主源及最多4个替补须各不相同')
            if type(wait_hours) not in (int,float) or not 1<=wait_hours<=168:raise ValueError('等待主源需1–168小时')
            reviewed={s['family']:s for s in self.status(sid)['samples'] if self.reviewed(s) and not s['processing_blocker']}
            if any(k not in reviewed for k in [primary]+backups):raise ValueError('主源和替补必须先完成实物评审')
            for key in [primary]+backups:
                s=reviewed[key]
                if self.sub.fingerprint({'path':s['path'],'size':s['size']})!=s['review'].get('fingerprint'):raise ValueError('评审文件变化，请重新分析')
            cfg=self.policy(sid);cfg.update(mode='selected',primary=primary,backups=backups,wait_hours=wait_hours,chosen_at=time.time())
            # Promote the selected completed sample rather than download the first episode again.
            if replace_first_hash is None:self.promote(reviewed[primary])
            else:self.promote(reviewed[primary],replace_first_hash)
            self.save(sid,cfg)
            self.db.execute("UPDATE candidates SET status='discovered',reason='' WHERE show_id=? AND status IN ('source_wait','covered','rejected')",(sid,))
            self.engine.wake.set()
        return self.status(sid)
    def premiere_reserved(self,sid):
        current=self.db.one('SELECT * FROM episodes WHERE show_id=? AND episode=1',(sid,))
        if not current:return False
        if current['status']!='complete' or not media_blocker(self.db,current):return True
        # A blacklisted completed copy is history, never a playable reservation.
        # In-flight/uncertain ownership remains a reservation until reconciled.
        if current['hash']:
            task=self.db.one('SELECT status FROM tasks WHERE hash=?',(current['hash'],))
            if not task or task['status'] not in ('complete','linked'):return True
            queue=self.db.one('SELECT committing,lease,state FROM thunder_queue WHERE hash=?',(current['hash'],))
            if queue and (queue['committing'] or queue['lease'] or queue['state'] in ('claimed','review')):return True
        return False
    def store_premiere(self,con,values,previous=None):
        if previous is None:
            con.execute('''INSERT INTO episodes(show_id,episode,hash,file_index,path,status,progress,size,first_completed)
                VALUES(?,1,?,?,?,'complete',1,?,?)''',values)
            return
        sid,h,index,path,size,completed=values
        changed=con.execute('''UPDATE episodes SET hash=?,file_index=?,path=?,status='complete',progress=1,size=?,first_completed=?
            WHERE show_id=? AND episode=1 AND status='complete' AND hash IS ? AND path=?''',
            (h,index,path,size,previous['first_completed'] or completed,sid,previous['hash'],previous['path']))
        if changed.rowcount!=1:raise ValueError('首集记录已变化，重新核对')
        con.execute('INSERT INTO kv VALUES(?,?)',('source_excluded_premiere_'+str(sid)+'_'+str(time.time_ns()),
            json.dumps({'previous_episode':previous,'new_hash':h,'changed_at':time.time(),
                        'reason':'已封禁的完成片源退出有效分集，复用合格完成样本；原文件和台账保留'},ensure_ascii=False)))
    def publish_available(self):
        """Expose a verified completed premiere while honest source reviews continue."""
        if not self.db.get('play_completed_premiere_enabled',False):return
        # Earlier manually published premieres also need a provisional family for later episodes.
        for row in self.db.rows("SELECT show_id FROM source_policies"):
            sid=row['show_id'];cfg=self.policy(sid)
            if cfg.get('mode')!='sampling' or cfg.get('playback_family') or not self.db.review_wanted(sid) or not self.db.allowed(sid,1):continue
            ep=self.db.one("SELECT * FROM episodes WHERE show_id=? AND episode=1 AND status='complete'",(sid,))
            if not ep:continue
            for s in self.db.rows('''SELECT s.*,c.title FROM source_samples s JOIN candidates c ON c.id=s.candidate_id WHERE s.show_id=?''',(sid,)):
                if json.loads(s['review']).get('decision')=='excluded' or fidelity_blocker(self.db,s['title']):continue
                task=self.db.one('SELECT selection,save_path FROM tasks WHERE hash=?',(s['hash'],))
                if task and not self.selected_subtitles_complete(task):continue
                if not s['path'] or not Path(s['path']).is_file() or not Path(ep['path']).is_file():continue
                if not os.path.samefile(s['path'],ep['path']):continue
                with self.db.gate:
                    if self.policy(sid)!=cfg or not self.db.review_wanted(sid) or not self.db.allowed(sid,1):break
                    cfg.update(playback_family=s['family'],playback_candidate=s['candidate_id'],playback_at=time.time())
                    self.save(sid,cfg)
                    self.db.execute("UPDATE candidates SET status='discovered',reason='' WHERE show_id=? AND status='source_wait'",(sid,))
                break
        rows=self.db.rows('''SELECT s.*,c.title FROM source_samples s JOIN candidates c ON c.id=s.candidate_id
            JOIN tasks t ON t.hash=s.hash WHERE t.status IN ('complete','linked') AND t.purpose='source_sample'
            AND s.state NOT IN ('scope_hold','error') ORDER BY s.updated,s.candidate_id''')
        for s in rows:
            sid=s['show_id']
            if self.premiere_reserved(sid):continue
            try:
                with self.db.gate:
                    self.ensure(sid,submission=False)
                    t=self.db.one('SELECT * FROM tasks WHERE hash=?',(s['hash'],))
                    if not t or t['status'] not in ('complete','linked') or t['purpose']!='source_sample':continue
                    if self.premiere_reserved(sid):continue
                    cfg=self.policy(sid)
                    if cfg.get('mode')=='selected' and not self.permits(s):continue
                    if json.loads(s['review']).get('decision')=='excluded':continue
                    if fidelity_blocker(self.db,s['title'],t['selection']):continue
                    if validate_title(s['title'],self.db.mapping(sid),{},False)!=[1]:continue
                    chinese_declared=bool(re.search(r'(?i)CHS|CHT|简|簡|繁|中文|BIG5|GB(?:2312)?|繁體',s['title']))
                    selection=json.loads(t['selection']);f=selection['selected']['1']
                    # A Chinese title cannot stand in for a selected external file.
                    # Reconciliation publishes complete sidecars; this path is read-only.
                    if not self.selected_subtitles_complete(t):continue
                    readonly=self.db.get('thunder_readonly_'+t['hash'],{}) if t['status']=='linked' else {}
                    p=Path(s['path']) if readonly else Path(t['save_path'])/f['name']
                    if not readonly and not p.resolve().is_relative_to(Path(t['save_path']).resolve()):continue
                    if not p.is_file() or p.stat().st_size!=f['size']:continue
                    if not chinese_declared:
                        evidence=json.loads(s['evidence'])
                        chinese_track=any(x.get('rank',2)<=1 and x.get('metrics',{}).get('chinese_cues',0)>20 for x in evidence.get('tracks',[]))
                        if s['state']!='ready' or not chinese_track or evidence.get('fingerprint')!=self.sub.fingerprint({'path':str(p),'size':f['size']}):continue
                    if t['status']=='linked':
                        if t['provider']!='thunder' or readonly.get('mode')!='read_only' or readonly.get('path')!=str(p) or readonly.get('size')!=f['size']:continue
                        root=p
                        for _ in Path(f['name']).parts:root=root.parent
                        if (root/f['name']).resolve()!=p.resolve():continue
                        found=self.engine.thunder.reader.verify(readonly['task_id'],t['hash'],str(root),selection['files'],selection['ids'])
                        if not found or not found['verified_complete']:continue
                    elif t['provider']=='thunder':
                        found=self.engine.thunder.inspect(t)
                        if not found or not found['verified_complete']:continue
                    elif t['provider']=='qbit':
                        found=self.engine.qb.info(t['hash'])
                        if not found or not self.engine.qb.owned(found[0]):continue
                        if Path(found[0]['save_path']).resolve()!=Path(t['save_path']).resolve():continue
                        actual=next((x for x in self.engine.qb.files(t['hash']) if x['name'].replace('\\','/')==f['name']),{})
                        if actual.get('progress',0)<1 or actual.get('size')!=f['size'] or actual.get('name')!=f['name']:continue
                    else:continue
                    self.promote(s,provisional=True)
                    cfg=self.policy(sid)
                    if cfg.get('mode')=='sampling':
                        cfg.update(playback_family=s['family'],playback_candidate=s['candidate_id'],playback_at=time.time())
                        self.save(sid,cfg)
                        self.db.execute("UPDATE candidates SET status='discovered',reason='' WHERE show_id=? AND status='source_wait'",(sid,))
            except Exception as error:
                message=str(error)[:250] if isinstance(error,ValueError) else safe_error(error)
                # Protected shows are skipped; actual failures remain observable and retryable.
                if self.db.review_wanted(sid):self.db.event('premiere:'+str(sid),message,'warning')
    @staticmethod
    def selected_subtitles_complete(task):
        selection=json.loads(task['selection']);root=Path(task['save_path']).resolve()
        for f in selection['files']:
            if f['index'] not in selection['ids'] or Path(f['name']).suffix.lower() not in ('.ass','.ssa','.srt'):continue
            p=(root/f['name']).resolve()
            if not p.is_relative_to(root) or not p.is_file() or p.stat().st_size!=f['size']:return False
        return True
    def promote(self,s,replace_hash=None,provisional=False):
        sid=s['show_id']
        title=s.get('title') or self.db.one('SELECT title FROM candidates WHERE id=?',(s['candidate_id'],))['title']
        task=self.db.one('SELECT selection,save_path FROM tasks WHERE hash=?',(s['hash'],))
        blocked=fidelity_blocker(self.db,title,task['selection'] if task else None)
        if blocked:raise ValueError(blocked)
        if task and not self.selected_subtitles_complete(task):raise ValueError('外挂字幕与完成清单不符')
        current=self.db.one('SELECT * FROM episodes WHERE show_id=? AND episode=1',(sid,))
        excluded_previous=current if current and replace_hash is None and not self.premiere_reserved(sid) else None
        if current and replace_hash is None and excluded_previous is None:return
        old=None
        if replace_hash is not None:
            if not current or current['hash']!=replace_hash or current['status']!='complete':raise ValueError('首集记录已变化，禁止替换')
            old=self.db.one('SELECT * FROM tasks WHERE hash=? AND show_id=?',(replace_hash,sid))
            if not old or old['status']!='complete' or old['purpose']!='episode' or old['provider']!='thunder':raise ValueError('只允许切换本项目已核验的迅雷首集')
            old_selection=json.loads(old['selection'])
            if list(old_selection['selected'])!=['1']:raise ValueError('原任务并非独立首集，禁止切换')
            found=self.engine.thunder.inspect(old)
            if not found or not found['verified_complete']:raise ValueError('原首集尚未核验完成')
            old_file=old_selection['selected']['1']
            if Path(current['path']).resolve()!=(Path(old['save_path'])/old_file['name']).resolve() or not os.path.samefile(current['path'],Path(old['staging_path'])/old_file['name']):raise ValueError('原首集文件归属已变化')
        t=self.db.one('SELECT * FROM tasks WHERE hash=?',(s['hash'],))
        if t and t['status']=='linked' and t['purpose']=='source_sample' and t['show_id']==sid and replace_hash is None:
            evidence=self.db.get('thunder_readonly_'+t['hash'],{})
            p=Path(s['path'])
            if evidence.get('mode')!='read_only' or evidence.get('path')!=str(p) or evidence.get('size')!=s['size'] or not p.is_file() or p.stat().st_size!=s['size']:
                raise ValueError('只读样本文件已变化，禁止归入正片')
            # Preserve outside ownership and cleanup exclusion when this source wins.
            with self.db.connect() as con:
                self.store_premiere(con,(sid,None,None,str(p),s['size'],None),excluded_previous)
            return
        if not t or t['show_id']!=sid or t['status']!='complete' or t['purpose']!='source_sample':raise ValueError('样本还不能归入正片')
        if t['provider']=='qbit' and replace_hash is None:
            # qB keeps its registered save path. Changing only our ledger would break ownership.
            f=json.loads(t['selection'])['selected']['1'];p=Path(t['save_path'])/f['name']
            if not p.is_file() or p.stat().st_size!=f['size']:raise ValueError('完成样本文件已变化')
            actual=next(x for x in self.engine.qb.files(t['hash']) if x['name'].replace('\\','/')==f['name'])
            with self.db.connect() as con:
                con.execute("UPDATE tasks SET purpose='episode' WHERE hash=?",(t['hash'],))
                self.store_premiere(con,(sid,t['hash'],actual['index'],str(p),f['size'],time.time()),excluded_previous)
            self.db.event('source:'+str(sid),'完成首集已自动加入追番播放；评分继续，尚未确认最佳主源' if provisional else '已确认主源；首集复用qB完成样本，未重复下载')
            return
        if old or excluded_previous:
            found=self.engine.thunder.inspect(t) if t['provider']=='thunder' else None
            if not found or not found['verified_complete']:raise ValueError('新样本尚未核验完成')
        title=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',self.db.show(sid)['title']).strip('. ')[:80]
        dest=self.engine.library/(title+f' [bgm-{sid}]')/f'Season {self.db.mapping(sid).get("season",1):02}'
        selection=json.loads(t['selection'])
        for f in selection['files']:
            if f['index'] not in selection['ids']:continue
            src=Path(t['save_path'])/f['name'];out=dest/f['name'];out.parent.mkdir(parents=True,exist_ok=True)
            if out.exists() and not os.path.samefile(src,out):raise ValueError('正片目标存在其他文件，禁止覆盖')
            if not out.exists():os.link(src,out)
        f=selection['selected']['1']
        with self.db.connect() as con:
            con.execute("UPDATE tasks SET purpose='episode',save_path=? WHERE hash=?",(str(dest),t['hash']))
            if old:
                con.execute("UPDATE episodes SET hash=?,file_index=?,path=?,size=? WHERE show_id=? AND episode=1 AND hash=?",
                    (t['hash'],f['index'],str(dest/f['name']),f['size'],sid,replace_hash))
                # Keep the previous library alias and task; no deletion or moving.
                con.execute("UPDATE tasks SET purpose='source_sample' WHERE hash=?",(replace_hash,))
                con.execute('INSERT INTO kv(key,value) VALUES(?,?)',('source_replacement_'+str(sid)+'_'+str(time.time_ns()),json.dumps({'previous_episode':current,'new_hash':t['hash'],'changed_at':time.time()},ensure_ascii=False)))
            else:
                self.store_premiere(con,(sid,t['hash'],f['index'],str(dest/f['name']),f['size'],time.time()),excluded_previous)
        self.db.event('source:'+str(sid),'完成首集已自动加入追番播放；评分继续，尚未确认最佳主源' if provisional else '已按明确操作切换首集至完成样本，原文件和观看历史保留' if old else '已确认主源；首集复用完成样本归库，未重复下载')
    def permits(self,c):
        cfg=self.policy(c['show_id'])
        if cfg.get('mode')=='subtitle_only':
            return not fidelity_blocker(self.db,c['title'])
        if clean_blocker(self.db,c):return False
        if fidelity_blocker(self.db,c['title']):return False
        if not cfg:return True
        if cfg.get('mode')=='sampling':
            return bool(self.db.get('play_completed_premiere_enabled',False) and self.db.review_wanted(c['show_id'])
                        and cfg.get('playback_family')==family(c['title'])['key']
                        and self.db.one("SELECT 1 FROM episodes WHERE show_id=? AND episode=1 AND status='complete'",(c['show_id'],)))
        if cfg.get('mode')!='selected':return True
        key=family(c['title'])['key'];primary=cfg['primary'];backups=cfg.get('backups',[])
        if key==primary:return True
        if key not in backups:return False
        sid=c['show_id'];ep=validate_title(c['title'],self.db.mapping(sid),{},False)
        if len(ep)!=1:return False
        # A primary release that is already known gets the slot before a backup.
        rows=self.db.rows('SELECT title,published,discovered FROM candidates WHERE show_id=?',(sid,))
        valid=[]
        for r in rows:
            if fidelity_blocker(self.db,r['title']):continue
            try:
                if validate_title(r['title'],self.db.mapping(sid),{},False)==ep:valid.append(r)
            except Rejected:continue
        if any(family(r['title'])['key']==primary for r in valid):return False
        earliest=min((r['discovered'] for r in valid),default=time.time())
        air=cfg.get('airing_epoch')
        expected=air+(ep[0]-1)*cfg.get('interval_hours',168)*3600 if air else earliest
        if time.time()<expected+cfg.get('wait_hours',24)*3600:return False
        for higher in backups[:backups.index(key)]:
            if any(family(r['title'])['key']==higher for r in valid):return False
        self.db.event('source:'+str(sid),f'主源本集逾期，使用已评审替补：{family(c["title"])["label"]}')
        return True
    def priority(self,c):
        cfg=self.policy(c['show_id']);order=[cfg.get('primary')]+cfg.get('backups',[]);key=family(c['title'])['key']
        return order.index(key) if key in order else 99


def register_source_review(app,engine):
    review=engine.source_review
    from .db import ROOT
    from fastapi.responses import JSONResponse
    @app.get('/subtitle-reviews')
    def subtitle_page():return FileResponse(ROOT/'static/subtitle-reviews.html')
    @app.get('/api/source-review/subtitles/history')
    def subtitle_history(show_id:int=0,latest:bool=True):return review.history.report(show_id,latest)
    @app.get('/api/source-review/overview')
    def source_overview():return review.overview()
    @app.get('/api/source-review/subtitles/export')
    def subtitle_export(show_id:int=0):return JSONResponse(review.history.report(show_id,False),headers={'Content-Disposition':'attachment; filename="subtitle-comparisons.json"'})
    @app.post('/api/source-review/subtitles/archive')
    def subtitle_archive():return review.history.archive_existing()
    @app.get('/api/shows/{sid}/source-review')
    def status(sid:int):return review.status(sid)
    @app.post('/api/shows/{sid}/source-review/start')
    def start(sid:int,p:dict):return review.start(sid,p.get('candidate_ids'))
    @app.post('/api/shows/{sid}/source-review/review')
    def record(sid:int,p:dict):
        review.record_review(sid,int(p['candidate_id']),p['review']);return {'ok':True}
    @app.post('/api/shows/{sid}/source-review/choose')
    def choose(sid:int,p:dict):return review.choose(sid,p['primary'],p.get('backups',[]),p.get('wait_hours',24),p.get('replace_first_hash'))
    @app.get('/api/shows/{sid}/source-review/{cid}/file/{name}')
    def artifact(sid:int,cid:int,name:str):
        sample=review.db.one('SELECT * FROM source_samples WHERE candidate_id=? AND show_id=?',(cid,sid))
        if not sample or sample['state']!='ready':raise ValueError('样本未准备好')
        if not re.fullmatch(r'(?:packet\.json|ocr\.json|frame-[0-5]\.png|track-\d+\.(?:ass|vtt))',name):raise ValueError('不可访问此评测文件')
        target=review.root/'evidence'/str(cid)/name
        if not target.is_file():raise ValueError('文件不存在')
        return FileResponse(target)
