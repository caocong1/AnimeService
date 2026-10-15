import json
from pathlib import Path
import pytest
import time
from test_reliability import setup,torrent
from test_source_review import ready_review
from test_thunder_bridge import accept
from anime.subtitles import Subtitles
from anime.subtitle_studio import archive_for,authored,lab_core


def ready(setup):
    db,q,e,r,c=ready_review(setup)
    db.set('subtitle_studio_enabled',True)
    e.source_review.save(42,{'mode':'selected','primary':'existing'})
    return db,q,e,r,c


def test_late_group_episode_two_queues_without_regular_reservation(setup):
    db,q,e,r,_=ready(setup);before=db.show(42)
    c=setup[3]('[LateGroup] Example S01E02 [1080p][CHS]',url='https://mikanani.me/ep2.torrent')
    e.sources.data=torrent((2,));candidate=next(c for c in e.subtitle_studio.candidates(42) if c['episode']==2)
    e.subtitle_studio.queue(candidate)
    task=db.one('SELECT * FROM tasks');assert task['purpose']=='subtitle_sample'
    assert list(json.loads(task['selection'])['selected'])==['2']
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')
    job=e.thunder.claim();e.thunder.preflight(task['hash'],job['lease']);accept(r,task,8)
    f=json.loads(task['selection'])['selected']['2'];src=Path(task['staging_path'])/f['name']
    src.parent.mkdir(parents=True,exist_ok=True);src.write_bytes(b'x'*f['size'])
    e.reconcile()
    sample=db.one('SELECT * FROM subtitle_samples');assert Path(sample['path']).is_file()
    assert db.one('SELECT status FROM tasks')['status']=='complete'
    assert db.show(42)==before and not db.rows('SELECT * FROM episodes') and q.adds==0


def test_episode_scope_rechecked_before_final_click(setup):
    db,q,e,r,_=ready(setup);setup[3]('[Late] Example S01E02 [1080p][CHS]',url='https://mikanani.me/ep2.torrent')
    e.sources.data=torrent((2,));e.subtitle_studio.queue(next(c for c in e.subtitle_studio.candidates(42) if c['episode']==2))
    job=e.thunder.claim();db.execute('UPDATE shows SET selected=0 WHERE id=42')
    with pytest.raises(ValueError):e.thunder.preflight(job['hash'],job['lease'])
    assert q.adds==0


def test_banned_publisher_never_enters_subtitle_candidates(setup):
    db,q,e,r,_=ready(setup)
    db.set('source_publisher_blacklist',[{'publisher':'Skymoon','aliases':['Skymoon-Raws','Skymoon'],'reason':'实证正片广告'}])
    setup[3]('[Skymoon-Raws] Example S01E02 [1080p][CHS]',url='https://mikanani.me/bad.torrent')
    assert all('Skymoon' not in c['title'] for c in e.subtitle_studio.candidates(42))


def test_unwanted_or_wrong_episode_cannot_acquire(setup):
    db,q,e,r,_=ready(setup)
    db.execute('UPDATE shows SET selected=0 WHERE id=42')
    with pytest.raises(ValueError):e.subtitle_studio.ensure(42,2,True)
    db.execute('UPDATE shows SET selected=1 WHERE id=42')
    mapping=db.mapping(42);mapping['end']=1;db.execute('UPDATE shows SET mapping=?',(json.dumps(mapping),))
    with pytest.raises(ValueError):e.subtitle_studio.ensure(42,2,True)


def test_native_archiving_makes_pending_workbook_not_default(setup,monkeypatch):
    db,q,e,r,_=ready(setup);p=db.path.parent/'original.mkv';p.write_bytes(b'fixture')
    db.execute("INSERT INTO episodes(show_id,episode,path,status) VALUES(42,2,?,'complete')",(str(p),))
    zh=db.path.parent/'zh.vtt';ja=db.path.parent/'ja.vtt'
    zh.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n明天再见。\n',encoding='utf-8')
    ja.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nまた明日。\n',encoding='utf-8')
    monkeypatch.setattr(e.subtitle_studio.sub,'tracks',lambda item:[{'index':0,'supported':True,'codec':'subrip','language':'chi'}, {'index':1,'supported':True,'codec':'subrip','language':'jpn'}])
    monkeypatch.setattr(e.subtitle_studio.sub,'extract',lambda item,index,fmt:zh if index==0 else ja)
    item=e.subtitle_studio.media()[0];result=e.subtitle_studio.collect(item)
    assert len(result['draft_ids'])==2
    job=db.one('SELECT * FROM subtitle_studio_jobs');assert job['state']=='waiting_review'
    book=json.loads(Path(json.loads(job['payload'])['workbook']).read_text(encoding='utf-8'))
    assert book['cues'][0]['status']=='pending' and book['cues'][0]['ja']==''
    assert authored(db,item) is None
    before=Path(json.loads(job['payload'])['workbook']).read_bytes()
    e.subtitle_studio.refresh_job(42,2)
    assert before==Path(json.loads(job['payload'])['workbook']).read_bytes()
    assert p.read_bytes()==b'fixture' and not db.rows('SELECT * FROM watches')


def test_uncertain_same_hash_is_never_replaced(setup):
    db,q,e,r,_=ready(setup);setup[3]('[Late] Example S01E02 [1080p][CHS]',url='https://mikanani.me/ep2.torrent')
    e.sources.data=torrent((2,));c=next(c for c in e.subtitle_studio.candidates(42) if c['episode']==2)
    e.subtitle_studio.queue(c);db.execute("UPDATE tasks SET status='uncertain'")
    original=db.one('SELECT * FROM tasks');e.subtitle_studio.queue(c)
    assert db.one('SELECT * FROM tasks')==original and q.adds==0


def test_episode_two_uses_audited_ui_fault_fallback(setup):
    from anime.download_recovery import recover_downloads
    db,q,e,r,_=ready(setup);setup[3]('[Late] Example S01E02 [1080p][CHS]',url='https://mikanani.me/ep2.torrent')
    e.sources.data=torrent((2,));c=next(c for c in e.subtitle_studio.candidates(42) if c['episode']==2)
    e.subtitle_studio.queue(c);h=db.one('SELECT hash FROM tasks')['hash']
    db.set('qbit_fallback_enabled',True);db.set('thunder_ui_fault',{'active':True,'reason':'Computer Use fixture failure'})
    recover_downloads(e)
    task=db.one('SELECT * FROM tasks');assert task['provider']=='qbit' and task['status']=='downloading'
    assert db.get('qbit_fallback_'+h)['thunder_absent'] and q.adds==1
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')


def test_uncertain_thunder_submission_cannot_fall_back(setup):
    from anime.download_recovery import recover_downloads
    db,q,e,r,_=ready(setup);setup[3]('[Late] Example S01E02 [1080p][CHS]',url='https://mikanani.me/ep2.torrent')
    e.sources.data=torrent((2,));e.subtitle_studio.queue(next(c for c in e.subtitle_studio.candidates(42) if c['episode']==2))
    db.set('qbit_fallback_enabled',True);db.set('thunder_ui_fault',{'active':True,'reason':'fixture failure'})
    db.execute("UPDATE thunder_queue SET committing=1")
    recover_downloads(e)
    assert q.adds==0 and db.one('SELECT provider FROM tasks')['provider']=='thunder'


def test_metadata_discovery_tracks_midseason_start_without_download(setup):
    db,q,e,r,_=ready(setup);db.set('downloader_preference','qbit')
    db.set('qbit_health',{'ok':False,'time':time.time()})
    setup[3]('[Late] Example S01E02 [1080p][CHS]',url='https://mikanani.me/ep2.torrent')
    e.subtitle_studio.discover()
    overview=e.subtitle_studio.overview();ep2=next(j for j in overview['jobs'] if j['episode']==2)
    late=next(g for g in ep2['payload']['source_coverage'] if g['publisher']=='Late')
    assert late['first_seen_episode']==2 and late['available_this_episode']
    group=next(g for g in ep2['payload']['source_coverage'] if g['publisher']=='Group')
    assert group['last_seen_episode']==1 and not group['available_this_episode']
    assert not db.rows('SELECT * FROM tasks') and q.adds==0


def test_banned_subtitle_provenance_revokes_authored_default_on_clean_video(setup):
    import hashlib,struct,zlib
    db,q,e,r,_=ready(setup);core=lab_core();a=archive_for(db)
    video=db.path.parent/'clean.mkv';video.write_bytes(b'clean original fixture')
    target={'path':str(video),'size':video.stat().st_size,'fingerprint':core.fingerprint(video)}
    refs={}
    for lang,text in [('chi','明天再见。'),('jpn','また明日。')]:
        p=db.path.parent/(lang+'.vtt');p.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n'+text+'\n',encoding='utf-8')
        refs[lang]=a.add_base(42,1,2,p,{'source_key':lang,'language':lang,'media_fingerprint':target['fingerprint'],'release':'[Group] Example S01E02'})
    path=a.workbook(42,1,2,refs['chi'],target,5000);b=json.loads(path.read_text(encoding='utf-8'))
    b['cues'][0].update(ja='また明日。',japanese_source={'draft_id':refs['jpn'],'cue_id':1},status='accepted',reviewer='original fixture',basis='synthetic test text')
    manifest=a.compile(b)
    def chunk(kind,payload):return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
    images=[]
    for width,height in ((1920,1080),(640,360)):
        png=a.root/f'test-{width}.png';png.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+bytes(width*3))*height))+chunk(b'IEND',b''))
        images.append({'path':str(png),'sha256':hashlib.sha256(png.read_bytes()).hexdigest(),'player_width':width})
    cert={k:{'status':'accepted','reviewer':'fixture','evidence':'original synthetic fixture'} for k in ('semantic','timing','screen_text','render')}
    cert.update(build_id=manifest['id'],target_fingerprint=target['fingerprint']);cert['render']['images']=images
    # Synthetic state-machine proof only; no actual media/font certification.
    fonts={}
    for lang,family in [('zh','Noto Sans CJK SC'),('ja','Noto Sans CJK JP')]:
        font=a.root/(lang+'-synthetic-font.fixture');font.write_bytes(('original '+lang+' font fixture').encode())
        fonts[lang]={'path':str(font),'family':family,'sha256':core.digest(font.read_bytes()),'weight':b['style'].get('font_weight_'+lang,400),
                     'ass_name':core.ass_font_name(core.layout_style(b['style']),lang),'postscript_names':['fixture-'+lang]}
    report={'input_hash':core.layout_input_hash(b),'engine':'original synthetic geometry fixture','fonts':fonts,
            'rows':[{'cue':c['id'],'language':lang,'width_px':200} for c in b['cues'] for lang in ('zh','ja')],'errors':[]}
    metrics=a.root/'layout-fixture.json';core.write_json(metrics,report)
    cert['render']['layout_report']={'path':str(metrics),'sha256':core.digest(metrics.read_bytes())}
    renderproof=a.root/'render-fixture.json';core.write_json(renderproof,{'input_hash':core.layout_input_hash(b),'target_fingerprint':target['fingerprint'],
        'font_selection_verified':True,'font_selection_errors':[],'font_files':list(fonts.values()),'frames':cert['render']['images'],
        'font_selection':[f'({f["ass_name"]}, {f["weight"]}, 0) -> fixture-{lang}, 0, fixture-{lang}' for lang,f in fonts.items()]})
    cert['render']['font_selection_report']={'path':str(renderproof),'sha256':core.digest(renderproof.read_bytes())}
    a.accept(manifest['id'],cert)
    item={**target,'show_id':42,'episode':2}
    assert authored(db,item)['id']==manifest['id']
    db.set('source_publisher_blacklist',[{'publisher':'Group','aliases':['Group'],'reason':'fixture verified advertisement'}])
    assert authored(db,item) is None
    assert video.read_bytes()==b'clean original fixture'
