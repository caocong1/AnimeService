"""Delivery-first scheduling fixtures use only synthetic local files/fake qB."""
import hashlib
import json
import time

import pytest
from test_reliability import setup
from test_subtitle_delivery_integration import prepared_candidate
from test_subtitle_targets import evidence
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_delivery_materials import readiness


@pytest.mark.parametrize('failure', [None, 'policy_missing', 'optin_missing', 'wrong_target', 'expired', 'changed_report'])
@pytest.mark.requires_lab
def test_first_release_uses_chinese_and_real_output_preparation_without_fake_audio(setup, monkeypatch, failure):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    before=(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'))
    record.pop('japanese_dialogue_report');record.pop('capability_report')
    report={'role':'finished_v1_technical_preparation','show_id':42,'episode':2,
            'reviewer':'synthetic fixture only','target_fingerprint':target['fingerprint'],
            'technical_decode_passed':True,'output_tools_verified':True,'local_viewer_available':True,
            'actual_audio_received_and_listened':False,'evidence':'synthetic decode fixture',
            'expires_at':time.time()+3600}
    if failure=='wrong_target':report['target_fingerprint']='stale'
    if failure=='expired':report['expires_at']=0
    ref=evidence(archive.root,'first-release-tools.json',report)
    record.update(dispatch_standard='finished_v1',delivery_capability_report=ref)
    if failure=='optin_missing':record.pop('dispatch_standard')
    if failure=='changed_report':__import__('pathlib').Path(ref['path']).write_text('{}')
    db.set('subtitle_delivery_readiness_42_2',record)
    if failure!='policy_missing':db.set('subtitle_delivery_policy',{'first_release_mode':'finished'})
    result=readiness(db,42,2,{})
    assert result['accepted_verified'] is False
    scheduler=SubtitleDelivery(db);job=scheduler.record_preparation(42,2)
    assert (job['state']=='ready') is (failure is None)
    if failure is None:
        assert result['review_notes'] and not result['reasons']
        lease=scheduler.reserve(job['id'],'fixture-owner',job['revision'])
        scheduler.begin_dispatch(job['id'],'fixture-owner',lease['lease_token'],lease['revision'])
    assert before==(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches')) and q.adds==0


@pytest.mark.requires_lab
def test_already_delivered_episode_cannot_resume_or_repeat_first_release(setup,monkeypatch):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    scheduler=SubtitleDelivery(db);job=scheduler.record_preparation(42,2)
    db.set('subtitle_delivery_finished_42_2',{'delivery_status':'finished','release_id':'fixture-v1',
                                           'manifest_sha256':'a'*64,'accepted':False})
    assert next(j for j in scheduler.snapshot()['jobs'] if j['id']==job['id'])['delivery_finished']
    with pytest.raises(ValueError,match='finished viewing release'):
        scheduler.reserve(job['id'],'fixture owner',job['revision'])
    assert q.adds==0 and not db.rows('SELECT * FROM watches')


def release_fixture(db, target):
    project=db.path.parent.parent;root=project/'data/subtitle-studio/releases/42/S01E002/v1'
    root.mkdir(parents=True)
    qa={'decode_passed':True,'source_stat_unchanged':True,'duration_seconds':3,
        'source_audio_packet_sha256':'fixture','output_audio_packet_sha256':'fixture'}
    for name in ('bilingual.ass','static.ass','subtitles.zip','版本说明.txt','episode.mp4'):
        (root/name).write_bytes(b'original synthetic fixture '+name.encode())
    (root/'technical-qa.json').write_text(json.dumps(qa))
    files={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.iterdir()}
    r={'id':'fixture-e2-v1','show_id':42,'episode':2,'status':'finished','duration_seconds':3,
       'formal_review_complete':False,'files':files}
    source_hash=hashlib.sha256(__import__('pathlib').Path(target['path']).read_bytes()).hexdigest()
    config=root/'input-config.json';config.write_text(json.dumps({'media':target['path'],'media_sha256':source_hash}))
    r.update(source_media_sha256=source_hash,input_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest())
    (root/'release.json').write_text(json.dumps(r))
    static=project/'static/finished-media';(static/r['id']).mkdir(parents=True)
    (static/r['id']/'episode.mp4').write_bytes((root/'episode.mp4').read_bytes())
    entry={k:r[k] for k in ('id','show_id','episode','status')};entry['video_sha256']=files['episode.mp4']['sha256']
    (static/'index.json').write_text(json.dumps({'releases':[entry]}))
    proof=root/'player.json';proof.write_text(json.dumps({'role':'finished_episode_player_check',
        'release_id':r['id'],'video_sha256':entry['video_sha256'],'reviewer':'fixture only',
        'evidence':'synthetic fixture, no real viewing claim','errors':[],
        'play_pause_passed':True,'seek_passed':True,'download_passed':True,'complete_chinese_coverage_checked':True}))
    return root,proof


@pytest.mark.parametrize('failure',[None,'unknown','wrong_owner','bad_file','wrong_episode','bad_player','worker_active','not_published'])
@pytest.mark.requires_lab
def test_finished_receipt_verifies_files_and_releases_only_confirmed_owner(setup,monkeypatch,failure):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    before=(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'))
    scheduler=SubtitleDelivery(db);job=scheduler.record_preparation(42,2)
    lease=scheduler.reserve(job['id'],'fixture owner',job['revision'])
    intent=scheduler.begin_dispatch(job['id'],'fixture owner',lease['lease_token'],lease['revision'])
    scheduler.reconcile_dispatch(job['id'],intent['dispatch_token'],None if failure=='unknown' else 'fixture-chat')
    root,proof=release_fixture(db,target)
    if failure=='bad_file':(root/'episode.mp4').write_bytes(b'changed')
    if failure=='wrong_episode':
        r=json.loads((root/'release.json').read_text());r['episode']=3;(root/'release.json').write_text(json.dumps(r))
    if failure=='bad_player':
        p=json.loads(proof.read_text());p['seek_passed']=False;proof.write_text(json.dumps(p))
    if failure=='worker_active':(root/'worker.lock.json').write_text('{}')
    if failure=='not_published':(db.path.parent.parent/'static/finished-media/index.json').write_text('{"releases":[]}')
    if failure:
        with pytest.raises(ValueError):scheduler.record_finished(job['id'],'other' if failure=='wrong_owner' else 'fixture-chat',root,proof)
        assert scheduler.get(job['id'])['state']!='finished'
    else:
        result=scheduler.record_finished(job['id'],'fixture-chat',root,proof)
        assert result['state']=='finished' and result['dispatch_state']=='completed'
        assert result['accepted_build_id'] is None and not scheduler.has_active(42,2)
        assert scheduler.record_finished(job['id'],'fixture-chat',root,proof)['duplicate']
        scheduler.refresh();assert scheduler.get(job['id'])['state']=='finished'
        # Older already-loaded background refreshers may rewrite only the job;
        # the independently verified finished ledger must still prevent replay.
        db.execute("UPDATE subtitle_delivery_jobs SET state='ready' WHERE id=?",(job['id'],))
        assert next(j for j in scheduler.snapshot()['jobs'] if j['id']==job['id'])['state']=='finished'
        with pytest.raises(ValueError,match='finished viewing release'):
            scheduler.reserve(job['id'],'other writer',scheduler.get(job['id'])['revision'])
    assert before==(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches')) and q.adds==0
