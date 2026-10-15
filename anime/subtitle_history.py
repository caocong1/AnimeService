"""Append-only subtitle assessment snapshots; publication is not translation credit."""
import hashlib,json,re,time,unicodedata


def validate_findings(findings):
    if not isinstance(findings,list) or len(findings)>100:raise ValueError('字幕差异需为最多100条记录')
    for f in findings:
        if not isinstance(f,dict) or f.get('severity') not in ('major','minor','observation'):raise ValueError('字幕差异需注明重要/轻微/观察')
        if type(f.get('seconds')) not in (int,float) or not 0<=f['seconds']<=86400:raise ValueError('字幕差异需有效时间点')
        if not all(isinstance(f.get(k),str) and 0<len(f[k])<=4000 for k in ('finding','basis')):raise ValueError('字幕差异需描述和核验依据')


class SubtitleHistory:
    def __init__(self,owner):self.owner=owner;self.db=owner.db

    def snapshot(self,sample,review):
        # Runtime import avoids a module cycle with the existing review parser.
        from .source_review import cues,family
        candidate=self.db.one('SELECT title,published FROM candidates WHERE id=?',(sample['candidate_id'],))
        show=self.db.show(sample['show_id']);source=family(candidate['title'])
        evidence=json.loads(sample['evidence']) if isinstance(sample['evidence'],str) else sample['evidence']
        bound=bool(review.get('fingerprint') and review['fingerprint']==evidence.get('fingerprint'))
        tracks=[]
        for t in evidence.get('tracks',[]):
            language=(t.get('language') or '').lower()
            # OCR and Japanese Han characters never establish a Chinese text identity.
            if language not in ('chi','zho','zh','chs','cht','zh-cn','zh-tw'):continue
            item={k:t.get(k) for k in ('index','language','label','file')}
            item.update(text_sha256=None,cues=None,identity_method='unavailable')
            name=t.get('file') or ''
            if bound and re.fullmatch(r'track-\d+\.vtt',name):
                path=self.owner.root/'evidence'/str(sample['candidate_id'])/name
                if path.is_file():
                    rows=cues(path.read_text(encoding='utf-8'))
                    text='\n'.join(re.sub(r'\s+',' ',unicodedata.normalize('NFC',r['text'])).strip() for r in rows)
                    if text:
                        item.update(text_sha256=hashlib.sha256(text.encode()).hexdigest(),cues=len(rows),identity_method='normalized_text_without_timing')
            tracks.append(item)
        return {'schema_version':1,'show_id':sample['show_id'],'show_title':show['title'],
            'season':self.db.mapping(sample['show_id']).get('season'),'episode':1,'candidate_id':sample['candidate_id'],
            'publisher':source['group'],'release_family':sample['family'],'claimed_video_provider':source['provider'],
            'release_title':candidate['title'],'processing':source['processing'],'file_hash':sample['hash'],
            'analysis_fingerprint':evidence.get('fingerprint'),'review_fingerprint':review.get('fingerprint'),
            'text_identity_bound_to_review':bound,
            'published_at':candidate['published'],'release_delay_hours':self.owner.delay(sample['show_id'],candidate['published']),
            'reviewed_at':review.get('reviewed_at'),'decision':review.get('decision','reviewed'),
            'score_reviewed_at':review.get('original_reviewed_at',review.get('reviewed_at')),
            'chinese_verified':review.get('chinese_verified') is True,'translation_score':review.get('translation'),
            'confidence':review.get('confidence','qualification'),'reviewer':review.get('reviewer'),
            'evidence':review.get('evidence'),'original_basis':review.get('original_basis'),
            'findings':review.get('subtitle_findings',[]),'findings_coverage':'sampled' if review.get('subtitle_findings') else 'not_structured',
            'chinese_text_tracks':tracks,'translation_credit':None,
            'translation_credit_note':'发布者/视频平台不等于译者；同文只能证明文本相同，不能证明谁先翻译。',
            'evidence_directory':f'data/source-review/evidence/{sample["candidate_id"]}'}

    def append(self,con,payload):
        content={k:v for k,v in payload.items() if k not in ('reviewed_at','score_reviewed_at')}
        digest=hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        old=con.execute('SELECT id,revision_id,content_hash FROM subtitle_review_history WHERE candidate_id=? ORDER BY id DESC LIMIT 1',(payload['candidate_id'],)).fetchone()
        if old and old['content_hash']==digest:return False
        # Chain revisions so reverting A -> B -> A retains the final A as latest.
        revision=hashlib.sha256((digest+(old['revision_id'] if old else '')).encode()).hexdigest()
        con.execute('INSERT INTO subtitle_review_history(revision_id,content_hash,candidate_id,show_id,episode,recorded,payload) VALUES(?,?,?,?,?,?,?)',
            (revision,digest,payload['candidate_id'],payload['show_id'],payload['episode'],time.time(),json.dumps(payload,ensure_ascii=False)))
        return True

    def archive_existing(self):
        inserted=0
        with self.db.gate:
            for sample in self.db.rows("SELECT * FROM source_samples WHERE review<>'{}'"):
                review=json.loads(sample['review'])
                if not review.get('evidence') or not review.get('reviewer'):continue
                payload=self.snapshot(sample,review)
                with self.db.connect() as con:inserted+=self.append(con,payload)
        return {'ok':True,'inserted':inserted}

    def report(self,show_id=0,latest=True):
        where=' WHERE show_id=?' if show_id else '';args=(show_id,) if show_id else ()
        sql='SELECT * FROM subtitle_review_history'
        if latest:sql+=' WHERE id IN (SELECT max(id) FROM subtitle_review_history'+where+' GROUP BY candidate_id)'
        else:sql+=where
        records=[]
        for row in self.db.rows(sql+' ORDER BY id DESC',args):
            r=json.loads(row['payload']);r.update(record_id=row['id'],revision_id=row['revision_id'],recorded_at=row['recorded'])
            r['current_playback_exclusion']='原片保真偏好排除画面加工，字幕评审仍保留' if self.db.get('source_fidelity')=='original' and r['processing'] else ''
            r['shared_text_with']=[]
            records.append(r)
        index={}
        for r in records:
            for t in r['chinese_text_tracks']:
                if t['text_sha256']:index.setdefault((r['show_id'],r['episode'],t['text_sha256']),[]).append(r)
        for r in records:
            matches={}
            for t in r['chinese_text_tracks']:
                if not t['text_sha256']:continue
                for other in index[(r['show_id'],r['episode'],t['text_sha256'])]:
                    if other['candidate_id']==r['candidate_id']:continue
                    m=matches.setdefault(other['candidate_id'],{'candidate_id':other['candidate_id'],'publisher':other['publisher'],'text_sha256':[]})
                    if t['text_sha256'] not in m['text_sha256']:m['text_sha256'].append(t['text_sha256'])
            r['shared_text_with']=list(matches.values())
        return {'schema_version':1,'latest_only':latest,'records':records,
            'history_versions':self.db.one('SELECT count(*) AS n FROM subtitle_review_history'+where,args)['n'],
            'note':'分数为实物抽样相对评审，统计每候选只取最新评审，不把修订版本当多次实评；缺中文不记零分。同作品同集的相同中文文本不重复充当独立译稿；硬字幕OCR不用于译稿身份或错误率。发布时间仅为本集观测，未核实译者归属及整季可靠性。'}
