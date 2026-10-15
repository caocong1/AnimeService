import json,time
from pathlib import Path
import pytest
from test_reliability import setup,torrent
from test_thunder_bridge import FakeThunder,accept
from anime.thunder_bridge import ThunderBridge
from anime.releases import mapped_episodes,select_files,Rejected
from anime.source_review import family,cues,subtitle_metrics


def ready_review(setup):
    db,q,e,c=setup
    db.set('downloader_preference','thunder_first');db.set('thunder_ui_enabled',True)
    e.incomplete=db.path.parent/'incomplete'
    r=FakeThunder();e.thunder=ThunderBridge(e,r)
    db.execute("INSERT INTO sources(id,show_id,kind,url,enabled) VALUES(1,42,'mikan','https://mikanani.me/RSS/Bangumi?bangumiId=1',1)")
    candidate=c('[Group] Example S01E01 [1080p][CHS]')
    return db,q,e,r,candidate


def finish_sample(e,r):
    t=e.db.one('SELECT * FROM tasks');job=e.thunder.claim();e.thunder.preflight(t['hash'],job['lease']);accept(r,t,8)
    f=json.loads(t['selection'])['selected']['1'];src=Path(t['staging_path'])/f['name'];src.parent.mkdir(parents=True,exist_ok=True);src.write_bytes(b'x'*f['size'])
    e.reconcile();s=e.db.one('SELECT * FROM source_samples')
    fp=e.source_review.sub.fingerprint({'path':s['path'],'size':s['size']})
    e.db.execute("UPDATE source_samples SET state='ready',evidence=?",(json.dumps({'fingerprint':fp}),))
    return t,s,fp


def test_samples_share_queue_but_never_reserve_episode_or_watch(setup):
    db,q,e,r,c=ready_review(setup);before=db.show(42)
    e.source_review.start(42)
    assert len(db.rows('SELECT * FROM tasks'))==1 and db.one('SELECT purpose FROM tasks')['purpose']=='source_sample'
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches') and q.adds==0
    e.source_review.start(42);assert len(db.rows('SELECT * FROM tasks'))==1
    finish_sample(e,r)
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches') and db.show(42)==before


def test_sample_gate_at_preflight_after_drop_and_preference_disabled(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);job=e.thunder.claim()
    db.set('thunder_ui_enabled',False)
    with pytest.raises(ValueError):e.thunder.preflight(job['hash'],job['lease'])
    db.set('thunder_ui_enabled',True);e.state(42,'dropped')
    with pytest.raises(ValueError):e.thunder.preflight(job['hash'],job['lease'])
    with pytest.raises(ValueError):e.source_review.start(42)
    assert q.adds==0


def test_review_required_and_completed_sample_promoted_without_redownload(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    key=s['family']
    with pytest.raises(ValueError):e.source_review.choose(42,key,[])
    e.source_review.record_review(42,c['id'],{'visual':85,'translation':80,'chinese_verified':True,'confidence':'medium',
        'evidence':'00:03 fixture review','original_basis':'Japanese dialogue and official character terminology','reviewer':'test'})
    e.source_review.choose(42,key,[])
    episode=db.one('SELECT * FROM episodes')
    assert episode['status']=='complete' and Path(episode['path']).samefile(s['path'])
    assert db.one('SELECT purpose FROM tasks')['purpose']=='episode' and not db.rows('SELECT * FROM watches') and q.adds==0


def test_sampler_rejects_foreign_hash_invalid_candidate_and_rechecks_state(setup):
    db,q,e,r,c=ready_review(setup)
    with pytest.raises(ValueError):e.source_review.start(42,[999])
    assert e.source_review.policy(42)=={}
    old=e.sources.torrent
    def dropped(c):
        result=old(c);db.execute("UPDATE shows SET state='dropped' WHERE id=42");return result
    e.sources.torrent=dropped;e.source_review.start(42)
    assert not db.rows('SELECT * FROM tasks') and not db.rows('SELECT * FROM episodes')


def test_backup_wait_and_primary_precedence(setup):
    db,q,e,r,c=ready_review(setup)
    primary=family('[Main] Example S01E02 [1080p][CHS]')['key'];backup=family('[Backup] Example S01E02 [1080p][CHS]')['key']
    e.source_review.save(42,{'mode':'selected','primary':primary,'backups':[backup],'wait_hours':24})
    candidate=setup[3]('[Backup] Example S01E02 [1080p][CHS]',url='https://mikanani.me/backup.torrent')
    assert not e.source_review.permits(candidate)
    db.execute('UPDATE candidates SET discovered=? WHERE id=?',(time.time()-25*3600,candidate['id']))
    assert e.source_review.permits(candidate)
    setup[3]('[Main] Example S01E02 [1080p][CHS]',url='https://mikanani.me/main.torrent')
    assert not e.source_review.permits(candidate)
    e.ingest(candidate);assert not db.rows('SELECT * FROM episodes')


def test_explicit_mixed_numbering_and_dual_number_crosscheck():
    m={'aliases':['Kusuriya'],'season':3,'numbering':[{'min':1,'max':12,'offset':0},{'min':49,'max':60,'offset':48}]}
    assert mapped_episodes('[Kusuriya S3][01(49)]',m)==[1]
    assert mapped_episodes('Kusuriya - 49 (1080p)',m)==[1]
    assert mapped_episodes('Kusuriya S03E01',m)==[1]
    with pytest.raises(Rejected):mapped_episodes('[Kusuriya][02(49)]',m)
    with pytest.raises(Rejected):mapped_episodes('Kusuriya - 48',m)
    assert select_files([{'name':'Kusuriya S03E01.mkv','size':1,'index':0}],m,[1],[1])[1]==[0]


def test_family_variants_and_metrics_do_not_fabricate_semantics():
    assert family('[Group] Example - 01 [1080p][简体]')['key']==family('[Group] Example - 02 [1080p][繁体]')['key']
    assert family('[Group] Example [CR AI2160p]')['key']!=family('[Group] Example [CR 1080p]')['key']
    rows=cues('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n你好\n\n00:00:04.000 --> 00:00:05.000\n<b>再见</b>')
    m=subtitle_metrics(rows,100)
    assert m['cues']==2 and m['chinese_cues']==2 and m['translation_score'] is None
    assert cues('WEBVTT\n\n00:01.000 --> 00:03.000\n你好')[0]['start']==1
    japanese=subtitle_metrics([{'start':1,'end':3,'text':'蝗害ですか'}],100,'jpn')
    assert japanese['han_cues']==1 and japanese['chinese_cues']==0


def test_review_file_replacement_requires_new_review(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    e.source_review.record_review(42,c['id'],{'visual':80,'translation':80,'chinese_verified':True,'confidence':'high',
        'evidence':'actual frames','original_basis':'Japanese text','reviewer':'test'})
    Path(s['path']).write_bytes(b'z'*s['size'])
    with pytest.raises(ValueError):e.source_review.choose(42,s['family'],[])
    assert not db.rows('SELECT * FROM episodes')


def test_additional_samples_keep_selected_source_and_cap_all_families(setup):
    db,q,e,r,c=ready_review(setup)
    policy={'mode':'selected','primary':'chosen','backups':['backup'],'wait_hours':24}
    e.source_review.save(42,policy)
    e.source_review.start(42)
    assert e.source_review.policy(42)==policy
    for i in range(11):
        item=setup[3](f'[Group{i}] Example S01E01 [1080p][CHS]',url=f'https://mikanani.me/group{i}.torrent')
        db.execute("INSERT INTO source_samples(candidate_id,show_id,family,state,updated) VALUES(?,42,?,'error',?)",
            (item['id'],family(item['title'])['key'],time.time()))
    item=setup[3]('[NewGroup] Example S01E01 [1080p][CHS]',url='https://mikanani.me/newgroup.torrent')
    before=len(db.rows('SELECT * FROM tasks'))
    with pytest.raises(ValueError,match='最多12种'):e.source_review.start(42,[item['id']])
    assert len(db.rows('SELECT * FROM tasks'))==before and e.source_review.policy(42)==policy


def test_media_analysis_does_not_start_a_second_worker(setup,monkeypatch):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);finish_sample(e,r)
    db.execute("UPDATE source_samples SET state='analyzing'")
    e.source_review.processing.add(999)
    def unexpected(*args,**kwargs):raise AssertionError('parallel media worker')
    monkeypatch.setattr('anime.source_review.threading.Thread',unexpected)
    e.source_review.tick()
    assert e.source_review.processing=={999}


def test_failed_promotion_does_not_enable_selected_policy(setup,monkeypatch):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    e.source_review.record_review(42,c['id'],{'visual':80,'translation':80,'chinese_verified':True,'confidence':'medium',
        'evidence':'actual frames','original_basis':'Japanese text','reviewer':'test'})
    before=e.source_review.policy(42)
    def failed(*args):raise OSError('library unavailable')
    monkeypatch.setattr(e.source_review,'promote',failed)
    with pytest.raises(OSError):e.source_review.choose(42,s['family'],[])
    assert e.source_review.policy(42)==before and not db.rows('SELECT * FROM episodes')


def test_non_chinese_exclusion_keeps_evidence_without_fake_translation_score(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    e.source_review.record_review(42,c['id'],{'decision':'excluded','visual':90,'translation':0,
        'evidence':'Actual subtitle tracks are Japanese only; inspected frames have no Chinese.',
        'original_basis':'ffprobe and extracted Japanese ASS','reviewer':'test'})
    review=json.loads(db.one('SELECT review FROM source_samples')['review'])
    assert review['decision']=='excluded' and 'translation' not in review and 'visual' not in review
    assert e.source_review.status(42)['recommendation']==[]
    with pytest.raises(ValueError):e.source_review.choose(42,s['family'],[])
    assert Path(s['path']).exists() and not db.rows('SELECT * FROM episodes')


def test_known_airtime_wait_is_not_restarted_by_late_backup_discovery(setup):
    db,q,e,r,c=ready_review(setup)
    main=family('[Main] Example S01E02 [1080p][CHS]')['key'];backup=family('[Backup] Example S01E02 [1080p][CHS]')['key']
    e.source_review.save(42,{'mode':'selected','primary':main,'backups':[backup],'wait_hours':24,
        'airing_epoch':time.time()-168*3600-25*3600,'interval_hours':168})
    candidate=setup[3]('[Backup] Example S01E02 [1080p][CHS]',url='https://mikanani.me/backup.torrent')
    assert e.source_review.permits(candidate)
