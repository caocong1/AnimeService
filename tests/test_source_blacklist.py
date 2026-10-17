import json
from pathlib import Path
import pytest
from test_reliability import setup
from test_source_review import ready_review,finish_sample
from test_thunder_bridge import ready,accept
from anime.source_blacklist import KEY,source_blocker
from anime.source_review import family,SourceReview
from anime.webplayer import WebPlayer
from anime.db import Store


def ban(db):
    db.set(KEY,[{'publisher':'Skymoon-Raws','aliases':['Skymoon-Raws','Skymoon'],
                 'reason':'正片中插入博彩广告','evidence':[{'candidate_id':1,'seconds':309}]}])


def test_persistent_exact_publisher_aliases_and_selected_files(setup):
    db,q,e,c=setup;ban(db);db=Store(db.path)
    assert source_blocker(db,'[sKyMoOn-RaWs] Example S01E01')
    assert source_blocker(db,'【Skymoon】 Example')
    for title in ('[Skymoon-Raws2] Example','[Other] Skymoon-Raws Example','[Other] Example [Viu]'):
        assert not source_blocker(db,title)
    selection={'ids':[0],'files':[{'index':0,'name':'Example S01E01.mkv'},
                                {'index':1,'name':'folder/[Skymoon] Example S01E01.mp4'}]}
    assert not source_blocker(db,'[Other] Example',selection)
    selection['ids']=[1];assert source_blocker(db,'[Other] Example',selection)
    candidate=c('[Skymoon-Raws] Example S01E01 [CHS]');e.ingest(candidate)
    assert db.one('SELECT status FROM candidates')['status']=='rejected'
    assert not db.rows('SELECT * FROM tasks') and q.adds==0


def test_blocked_samples_never_offered_chosen_or_promoted(setup):
    db,q,e,r,c=ready_review(setup)
    db.execute('UPDATE candidates SET title=?',('[Skymoon-Raws] Example S01E01 [1080p][CHS]',))
    e.source_review.start(42);t,s,fp=finish_sample(e,r)
    e.source_review.record_review(42,c['id'],{'visual':99,'translation':99,'chinese_verified':True,
        'confidence':'medium','evidence':'fixture','original_basis':'fixture','reviewer':'test'})
    before=db.rows('SELECT * FROM source_samples');ban(db)
    status=e.source_review.status(42)
    assert status['candidates']==[] and status['recommendation']==[]
    assert status['samples'][0]['source_blocker']
    with pytest.raises(ValueError):e.source_review.choose(42,s['family'],[])
    with pytest.raises(ValueError):e.source_review.queue(db.one('SELECT * FROM candidates'))
    with pytest.raises(ValueError):e.source_review.promote(s,provisional=True)
    db.set('play_completed_premiere_enabled',True);e.source_review.publish_available()
    assert not db.rows('SELECT * FROM episodes') and q.adds==0
    assert db.rows('SELECT * FROM source_samples')==before


def test_blacklist_added_after_claim_blocks_preflight_and_preserves_uncertainty(setup):
    db,q,e,r,t=ready(setup);db.execute('UPDATE tasks SET title=?',('[Skymoon-Raws] Example S01E01',))
    job=e.thunder.claim();ban(db)
    with pytest.raises(ValueError,match='黑名单'):e.thunder.preflight(job['hash'],job['lease'])
    assert db.one('SELECT committing FROM thunder_queue')['committing']==0
    db.set(KEY,[]);e.thunder.preflight(job['hash'],job['lease']);ban(db)
    e.thunder.report(job['hash'],job['lease'],'Timeout after click');e.reconcile()
    assert db.one('SELECT state,committing FROM thunder_queue')=={'state':'review','committing':1}
    assert db.one('SELECT hash FROM episodes')['hash']==t['hash'] and q.adds==0


def test_ad_copy_removed_from_media_but_files_episode_and_watches_remain(setup):
    db,q,e,c=setup;e.ingest(c('[Skymoon-Raws] Example S01E01 [1080p][CHS]'))
    t=db.one('SELECT * FROM tasks');f=json.loads(t['selection'])['selected']['1']
    p=Path(t['save_path'])/f['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*100)
    q.fs[t['hash']][0]['progress']=1;e.reconcile()
    db.execute("INSERT INTO watches VALUES(42,1,0,309,'manual',1)")
    player=WebPlayer(db);key=next(iter(player.media()))
    before=db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'),db.show(42)
    ban(db);assert key not in player.media()
    assert before==(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'),db.show(42))
    assert p.is_file() and q.adds==1
    from fastapi.testclient import TestClient
    from anime.app import create_app
    exe=db.path.parent/'dandanplay.exe';exe.write_bytes(b'fixture');db.set('dandan_exe',str(exe))
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        token=client.get('/api/bootstrap').json()['token']
        assert client.get('/api/web/media?show_id=42').json()==[]
        assert client.get(f'/api/web/media/{key}/picture').status_code==404
        assert not client.get('/api/home').json()['next']
        detail=client.get('/api/shows/42').json()
        assert detail['episodes']==[]
        assert detail['candidates']==[]
        assert detail['board']['next'] is None
        assert client.get('/api/shows?scope=home').json()[0]['next'] is None
        assert detail['board']['eps'][0]['s'] in ('future','missing','aired')
        for scope in ('home','history','catalog'):
            row=client.get('/api/shows?scope='+scope+'&include_unknown=true&kind=all').json()[0]
            assert row['downloaded']==0 and row['unwatched']==0
        response=client.post('/api/player/open',json={'show_id':42,'episode':1},headers={'X-Anime-Token':token})
        assert response.status_code==400 and '黑名单' in response.text


@pytest.mark.parametrize('held',['','uncertain','unselected','dropped'])
def test_clean_premiere_reuses_completed_sample_without_blacklisted_reservation(setup,held):
    from anime.download_recovery import recover_downloads
    from test_download_recovery import pending
    from test_reliability import encode
    db,q,e,r,c=pending(setup)
    # The original complete episode remains on disk and in downloader history.
    recover_downloads(e);old=db.one('SELECT * FROM tasks')
    f=json.loads(old['selection'])['selected']['1'];path=Path(old['save_path'])/f['name']
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'x'*f['size'])
    q.fs[old['hash']][0]['progress']=1;e.reconcile()
    db.set('play_completed_premiere_enabled',True);e.source_review.publish_available()
    original=db.one('SELECT * FROM episodes')
    db.execute('UPDATE tasks SET title=? WHERE hash=?',('[Skymoon-Raws] Example S01E01 [CHS]',old['hash']))
    db.execute('UPDATE candidates SET title=? WHERE id=?',('[Skymoon-Raws] Example S01E01 [CHS]',c['id']))
    db.execute("INSERT INTO watches VALUES(42,1,1,123,'manual',1)")
    watches=db.rows('SELECT * FROM watches')
    candidate=setup[3]('[Clean] Example S01E01 [1080p][CHS]',url='https://example.invalid/clean')
    e.sources.data=encode({b'info':{b'name':b'Example',b'piece length':16384,b'pieces':b'z'*20,
        b'files':[{b'length':100,b'path':[b'Example S01E01 Clean.mkv']}]}})
    e.source_review.start(42,[candidate['id']]);recover_downloads(e)
    new=db.one('SELECT * FROM tasks WHERE hash<>?',(old['hash'],))
    f=json.loads(new['selection'])['selected']['1'];clean=Path(new['save_path'])/f['name']
    clean.parent.mkdir(parents=True,exist_ok=True);clean.write_bytes(b'z'*f['size'])
    q.fs[new['hash']][0]['progress']=1;e.reconcile();ban(db)
    if held=='uncertain':db.execute("UPDATE tasks SET status='review' WHERE hash=?",(old['hash'],))
    if held=='unselected':db.execute('UPDATE shows SET selected=0 WHERE id=42')
    if held=='dropped':db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    e.source_review.publish_available()
    if held:assert db.one('SELECT * FROM episodes')==original
    else:
        assert db.one('SELECT hash FROM episodes')['hash']==new['hash']
        assert db.one('SELECT first_completed FROM episodes')['first_completed']==original['first_completed']
        assert db.rows("SELECT key FROM kv WHERE key LIKE 'source_excluded_premiere_%'")
        assert len(WebPlayer(db).media())==1
        assert e.source_review.policy(42)['playback_family']==family(candidate['title'])['key']
    assert db.rows('SELECT * FROM watches')==watches
    assert path.is_file() and clean.is_file() and q.adds==2


def test_qbit_ui_fault_fallback_cannot_submit_blacklisted_publisher(setup):
    from test_download_recovery import pending
    from anime.download_recovery import recover_downloads
    db,q,e,r,c=pending(setup)
    db.execute('UPDATE tasks SET title=?',('[Skymoon-Raws] Example S01E01 [1080p][CHS]',))
    before=db.one('SELECT * FROM tasks');ban(db);recover_downloads(e)
    assert db.one('SELECT * FROM tasks')==before
    assert q.adds==0 and not q.starts and not q.stops
    assert not db.get('qbit_fallback_'+before['hash'])
    from anime.download_policy import require_submission_ready
    db.set('qbit_fallback_'+before['hash'],{'hash':before['hash'],'show_id':42,'reason':'fixture','thunder_absent':True})
    with pytest.raises(ValueError,match='黑名单'):
        require_submission_ready(db,{**before,'provider':'qbit'})


def test_video_ineligible_can_keep_subtitle_review_without_becoming_recommendation(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    review={'visual':72,'translation':85,'chinese_verified':True,'confidence':'medium','evidence':'ad frame',
            'original_basis':'fixture','reviewer':'test','recommendation_eligible':False,
            'fidelity_qualification':{'eligible':False,'reason':'embedded advertisement'}}
    e.source_review.record_review(42,c['id'],review)
    status=e.source_review.status(42)
    assert status['samples'][0]['review']['translation']==85 and status['recommendation']==[]
    with pytest.raises(ValueError):e.source_review.choose(42,s['family'],[])


def test_media_keys_are_hashed_once_per_inventory_version_and_follow_file_changes(setup,tmp_path):
    db,q,e,c=setup
    inventory=db.path.parent/'inventory.json';inventory.write_text(json.dumps([{'path':str(tmp_path/'a.mp4'),'size':1},{'path':str(tmp_path/'b.mp4'),'size':2}]),encoding='utf-8')
    player=WebPlayer(db)
    first=player.media();assert len(first)==2 and player.inventory_rows() is player.inventory_rows()
    assert player.media()==first
    import os,time
    inventory.write_text(json.dumps([{'path':str(tmp_path/'c.mp4'),'size':3}]),encoding='utf-8');os.utime(inventory,None)
    changed=player.media();assert len(changed)==1 and next(iter(changed.values()))['name']=='c.mp4'
