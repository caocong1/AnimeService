import json,time
from pathlib import Path
import pytest
from test_reliability import setup,encode
from test_source_review import ready_review,finish_sample
from test_thunder_bridge import ready,accept
from anime.fidelity import processing_flags,fidelity_blocker
from anime.source_review import family


@pytest.mark.parametrize('title',['[G] Example AI2160p','Example [RIFE]','Example [补帧]','Example [Topaz Video AI]','Example [Upscaled 4K]','Example [AI降噪]'])
def test_declared_processing_is_excluded(title,setup):
    db=setup[0];db.set('source_fidelity','original')
    assert fidelity_blocker(db,title)


@pytest.mark.parametrize('title',['[G] Example [2160p][H265][10bit]','Example [1080p][60fps]','AI电子世界 S01E01','Example [AV1]'])
def test_format_alone_does_not_prove_processing(title):
    assert not processing_flags(title)


def test_selected_filenames_and_ingest_recheck_after_fetch(setup):
    db,q,e,c=setup;db.set('source_fidelity','original')
    selection={'ids':[0],'files':[{'index':0,'name':'Example S01E01.mkv'},{'index':1,'name':'AI2160p-preview.mkv'}]}
    assert not fidelity_blocker(db,'Example',selection)
    selection['ids']=[1];assert fidelity_blocker(db,'Example',selection)
    data=encode({b'info':{b'name':b'Example',b'piece length':16384,b'pieces':b'x'*20,b'files':[{b'length':100,b'path':[b'Example S01E01 AI2160p.mkv']}]}})
    e.sources.data=data;e.ingest(c())
    assert db.one('SELECT status FROM candidates')['status']=='rejected'
    assert not db.rows('SELECT * FROM tasks') and q.adds==0


def test_source_queue_and_review_do_not_offer_processed_source(setup):
    db,q,e,r,c=ready_review(setup)
    db.execute('UPDATE candidates SET title=? WHERE id=?',('[G] Example S01E01 [AI2160p][CHS]',c['id']))
    e.source_review.start(42);t,s,fp=finish_sample(e,r)
    e.source_review.record_review(42,c['id'],{'visual':99,'translation':99,'chinese_verified':True,'confidence':'medium','evidence':'fixture','original_basis':'fixture','reviewer':'test'})
    db.set('source_fidelity','original')
    status=e.source_review.status(42)
    assert status['samples'][0]['review']['visual']==99 and status['samples'][0]['processing_blocker']
    assert status['recommendation']==[] and status['candidates']==[]
    with pytest.raises(ValueError):e.source_review.choose(42,s['family'],[])
    with pytest.raises(ValueError):e.source_review.queue(db.one('SELECT * FROM candidates'))
    assert not db.rows('SELECT * FROM episodes') and q.adds==0


def test_preference_changed_while_claimed_blocks_final_click(setup):
    db,q,e,r,t=ready(setup)
    db.execute('UPDATE tasks SET title=?',('Example S01E01 [AI2160p]',))
    job=e.thunder.claim();db.set('source_fidelity','original')
    with pytest.raises(ValueError,match='保真'):e.thunder.preflight(job['hash'],job['lease'])
    assert db.one('SELECT committing FROM thunder_queue')['committing']==0
    e.thunder.report(job['hash'],job['lease'],'Preference changed before click')
    e.reconcile()
    assert db.one('SELECT state FROM thunder_queue')['state']=='cancelled'
    assert not db.rows('SELECT * FROM episodes') and q.adds==0


def test_cancel_unsubmitted_but_preserve_uncertain_or_foreign_task(setup):
    db,q,e,r,t=ready(setup);db.execute('UPDATE tasks SET title=?',('Example [AI2160p]',))
    job=e.thunder.claim();e.thunder.preflight(job['hash'],job['lease'])
    db.set('source_fidelity','original')
    e.thunder.report(job['hash'],job['lease'],'Timeout after click')
    assert db.one('SELECT state,committing FROM thunder_queue')=={'state':'review','committing':1}
    assert db.one('SELECT hash FROM episodes')['hash']==t['hash'] and q.adds==0


def test_known_processed_primary_does_not_block_original_backup(setup):
    db,q,e,c=setup;db.set('source_fidelity','original')
    main=c('[AI] Example S01E02 [AI2160p][CHS]')
    backup=c('[Raw] Example S01E02 [1080p][CHS]',url='https://mikanani.me/backup.torrent')
    e.source_review.save(42,{'mode':'selected','primary':family(main['title'])['key'],'backups':[family(backup['title'])['key']],'airing_epoch':time.time()-200*3600,'wait_hours':24})
    assert not e.source_review.permits(main) and e.source_review.permits(backup)


def test_explicit_completed_first_switch_preserves_files_history_and_normal_choose(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    review={'visual':80,'translation':80,'chinese_verified':True,'confidence':'medium','evidence':'fixture','original_basis':'Japanese','reviewer':'test'}
    e.source_review.record_review(42,c['id'],review);e.source_review.choose(42,s['family'],[])
    old=db.one('SELECT * FROM episodes');old_path=Path(old['path'])
    db.execute("INSERT INTO watches(show_id,episode,finished,position,method,updated) VALUES(42,1,1,1400,'manual',1)")
    # A distinct reviewed sample, with its own complete downloader acknowledgement.
    new=setup[3]('[Raw] Example S01E01 [1080p][CHS]',url='https://mikanani.me/raw.torrent')
    e.sources.data=encode({b'info':{b'name':b'Example',b'piece length':16384,b'pieces':b'y'*20,b'files':[{b'length':100,b'path':[b'Example S01E01 Raw.mkv']}]}})
    e.source_review.start(42,[new['id']]);nt=db.one('SELECT * FROM tasks WHERE hash<>?',(t['hash'],))
    job=e.thunder.claim();e.thunder.preflight(job['hash'],job['lease']);accept(r,nt,8)
    f=json.loads(nt['selection'])['selected']['1'];p=Path(nt['staging_path'])/f['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'z'*100)
    e.reconcile();ns=db.one('SELECT * FROM source_samples WHERE candidate_id=?',(new['id'],))
    nfp=e.source_review.sub.fingerprint({'path':ns['path'],'size':100})
    db.execute("UPDATE source_samples SET state='ready',evidence=? WHERE candidate_id=?",(json.dumps({'fingerprint':nfp}),new['id']))
    e.source_review.record_review(42,new['id'],review)
    e.source_review.choose(42,ns['family'],[])
    assert db.one('SELECT * FROM episodes')==old
    with pytest.raises(ValueError,match='已变化'):e.source_review.choose(42,ns['family'],[],replace_first_hash='wrong')
    e.source_review.choose(42,ns['family'],[],replace_first_hash=t['hash'])
    updated=db.one('SELECT * FROM episodes')
    assert updated['hash']==nt['hash'] and updated['first_completed']==old['first_completed']
    assert old_path.exists() and Path(updated['path']).samefile(p)
    assert db.one('SELECT * FROM watches')['finished']==1 and db.show(42)['state']=='watching'
    assert db.one('SELECT purpose FROM tasks WHERE hash=?',(t['hash'],))['purpose']=='source_sample'
    assert db.rows("SELECT key FROM kv WHERE key LIKE 'source_replacement_%'") and q.adds==0
