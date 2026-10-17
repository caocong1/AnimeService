"""Original fixtures; no production DB, downloader, audio or acceptance claims."""
import json
from pathlib import Path

import pytest
from test_reliability import setup
from test_subtitle_studio import ready
from test_subtitle_delivery import Readiness
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_studio import archive_for, lab_core


@pytest.mark.requires_lab
def test_delivery_http_refresh_requires_session_and_ignores_claimed_ready(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db, q, e, _, _ = ready(setup)
    with TestClient(create_app(db, e, False), base_url='http://127.0.0.1:4871') as client:
        assert client.get('/api/subtitle-delivery').json()['jobs'] == []
        assert client.post('/api/subtitle-delivery/refresh').status_code == 403
        token = client.get('/api/bootstrap').json()['token']
        result = client.post('/api/subtitle-delivery/refresh', headers={'X-Anime-Token': token},
                             json={'ready': True, 'actual_listening': True}).json()
        assert result['jobs'] and all(j['state'] != 'ready' for j in result['jobs'])
        assert all('lease_token' not in j and 'dispatch_token' not in j for j in result['jobs'])
    assert q.adds == 0 and not db.rows('SELECT * FROM watches')


@pytest.mark.parametrize('dispatch', ['acknowledged', 'unknown', 'blocked-stopped'])
@pytest.mark.requires_lab
def test_inventory_refresh_preserves_owned_editorial_input(setup, dispatch):
    db, q, e, _, _ = ready(setup)
    archive = archive_for(db)
    video = db.path.parent / 'original.mkv'; video.write_bytes(b'original fixture media')
    target = {'path': str(video), 'size': video.stat().st_size, 'fingerprint': lab_core().fingerprint(video)}
    source = db.path.parent / 'original.vtt'
    source.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n明天再见。\n', encoding='utf-8')
    base = archive.add_base(42, 1, 2, source, {'language': 'chi', 'source_key': 'original-fixture',
                                            'media_fingerprint': target['fingerprint']})
    work = archive.workbook(42, 1, 2, base, target)
    payload = {'season': 1, 'workbook': str(work), 'target': target, 'draft_ids': [base]}
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(42,2,?,'complete',?)",
               (str(video), target['size']))
    db.execute("INSERT INTO subtitle_studio_jobs VALUES(42,2,'waiting_review',?,'',0)", (json.dumps(payload),))
    assessor = Readiness(); scheduler = SubtitleDelivery(db, assessor); scheduler.refresh()
    e.subtitle_studio.delivery = scheduler
    db.set('subtitle_delivery_policy', {'enabled': True})
    job = scheduler.get('episode:42:2')
    lease = scheduler.reserve(job['id'], 'fixture owner', job['revision'])
    intent = scheduler.begin_dispatch(job['id'], 'fixture owner', lease['lease_token'], lease['revision'])
    if dispatch == 'unknown':
        scheduler.reconcile_dispatch(job['id'], intent['dispatch_token'])
    else:
        scheduler.reconcile_dispatch(job['id'], intent['dispatch_token'], thread_id='fixture-only-chat')
        if dispatch == 'blocked-stopped':
            scheduler.record_blocked(job['id'], 'fixture-only-chat', ['synthetic blocker'], owner_stopped=True)
    before = work.read_bytes()
    old_payload = db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=42 AND episode=2')['payload']
    later = db.path.parent / 'later.vtt'
    later.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n明日再会。\n', encoding='utf-8')
    second = archive.add_base(42, 1, 2, later, {'language': 'chi', 'source_key': 'new-fixture',
                                             'media_fingerprint': target['fingerprint']})
    e.subtitle_studio.refresh_job(42, 2)
    assert work.read_bytes() == before
    assert db.one('SELECT payload FROM subtitle_studio_jobs WHERE show_id=42 AND episode=2')['payload'] == old_payload
    assert second in db.get('subtitle_delivery_pending_inventory_42_2')['draft_ids']
    assert q.adds == 0 and not db.rows('SELECT * FROM watches')


def test_cli_public_output_never_exports_dispatch_or_lease_tokens():
    from scripts.subtitle_delivery_queue import public
    result = public({'lease_token': 'private fixture', 'payload': {'dispatch_token': 'private fixture'},
                     'thread_id': 'public fixture id'})
    assert result == {'payload': {}, 'thread_id': 'public fixture id'}


@pytest.mark.parametrize('classification', [None, 'songs_only', 'dialogue', 'stale_hash'])
@pytest.mark.requires_lab
def test_japanese_track_needs_real_dialogue_qualification_and_provisional_target(setup, classification):
    import hashlib
    from anime.subtitle_delivery_materials import readiness
    db, q, e, _, _ = ready(setup); archive = archive_for(db)
    video=db.path.parent/'prepared.mkv';video.write_bytes(b'synthetic prepared media')
    target={'path':str(video),'size':video.stat().st_size,'fingerprint':lab_core().fingerprint(video)}
    selection={'selected':{'2':{'name':video.name,'size':target['size'],'index':0}}}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose) VALUES(?,42,'[Group] Example S01E02','complete',?,?,'episode')",
               ('a'*40,json.dumps(selection),str(video.parent)))
    files=[]
    for name,lang,text in [('cn','chi','明天再见。'),('jp','jpn','また明日。')]:
        source=db.path.parent/(name+'.vtt')
        source.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n'+text+'\n',encoding='utf-8')
        files.append(archive.add_base(42,1,2,source,{'language':lang,'source_key':name,
                       'origin':'embedded','media_fingerprint':target['fingerprint']}))
    record={'target':target}
    if classification:
        path=archive.root/'original-fixture-qualification.json'
        value={'status':'verified','role':'dialogue' if classification!='songs_only' else 'songs_only',
               'coverage':'whole_episode_dialogue','show_id':42,'episode':2,'reviewer':'synthetic test only',
               'evidence':'original synthetic classification fixture, not real QA',
               'source_raw_hashes':[archive.draft(files[1])['raw_hash']]}
        path.write_text(json.dumps(value),encoding='utf-8')
        record['japanese_dialogue_report']={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        if classification=='stale_hash':path.write_text('{}')
    db.set('subtitle_delivery_readiness_42_2',record)
    result=readiness(db,42,2,{})
    assert result['target_fingerprint']==target['fingerprint']
    assert result['material_ready'] is (classification=='dialogue')
    assert result['capability_ready'] is False and result['accepted_verified'] is False
    assert q.adds==0 and not db.rows('SELECT * FROM watches')


def synthetic_png(width=32,height=18):
    """A complete original PNG, no third-party media or acceptance evidence."""
    import struct,zlib
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress((b'\x00'+bytes(width*3))*height))+chunk(b'IEND',b''))


def frame_refs(root):
    import hashlib
    refs=[]
    for n in range(3):
        path=root/f'original-frame-{n}.png';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(synthetic_png())
        refs.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                     'source_pts':n*1000,'scope':'synthetic state-machine fixture only'})
    return refs


def test_sampled_frame_inputs_need_three_distinct_files_but_allow_identical_hold_pixels(tmp_path):
    from anime.subtitle_delivery_materials import sampled_frames_valid
    root=tmp_path/'private';refs=frame_refs(root)
    assert len({x['sha256'] for x in refs})==1
    assert sampled_frames_valid(root,refs)
    assert sampled_frames_valid(root,refs+[refs[0]])
    assert not sampled_frames_valid(root,[refs[0]]*3)
    assert not sampled_frames_valid(root,refs[:2])


@pytest.mark.parametrize('failure',['stale_hash','wrong_hash','external_path','missing','not_png','bad_ihdr','zero_width','oversized_height'])
def test_sampled_frame_hash_owner_and_png_dimensions_reject_invalid_inputs(tmp_path,failure):
    import hashlib
    from anime.subtitle_delivery_materials import sampled_frames_valid
    root=tmp_path/'private';refs=frame_refs(root);path=Path(refs[0]['path'])
    if failure=='stale_hash':path.write_bytes(synthetic_png(40,18))
    elif failure=='wrong_hash':refs[0]['sha256']='0'*64
    elif failure=='external_path':
        outside=tmp_path/'outside.png';outside.write_bytes(path.read_bytes());refs[0]['path']=str(outside)
    elif failure=='missing':path.unlink()
    else:
        raw=bytearray(path.read_bytes())
        if failure=='not_png':raw[0:8]=b'notimage'
        elif failure=='bad_ihdr':raw[12:16]=b'IDAT'
        elif failure=='zero_width':raw[16:20]=bytes(4)
        else:raw[20:24]=(16385).to_bytes(4,'big')
        path.write_bytes(raw);refs[0]['sha256']=hashlib.sha256(raw).hexdigest()
    assert not sampled_frames_valid(root,refs)


def prepared_candidate(setup,monkeypatch):
    import time
    from test_subtitle_targets import fixture,evidence
    db,q,e,archive,old,new,freeze,target=fixture(setup,monkeypatch)
    jp=db.path.parent/'original-japanese.vtt'
    jp.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nまた明日。\n',encoding='utf-8')
    native=archive.add_base(42,1,2,jp,{'language':'jpn','source_key':'original-japanese-fixture',
        'origin':'embedded','media_fingerprint':target['fingerprint']})
    qualification=evidence(archive.root,'candidate-dialogue-fixture.json',{
        'status':'verified','role':'dialogue','coverage':'whole_episode_dialogue','show_id':42,'episode':2,
        'reviewer':'Original synthetic fixture only','evidence':'No real media qualification claim',
        'source_raw_hashes':[archive.draft(native)['raw_hash']]})
    review=json.loads(Path(freeze['base_review']['path']).read_bytes())
    review.update(role='production_candidate',scope='sample_only')
    review['clean']={'status':'sampled_clear','method':'soft_disabled_sampled',
        'evidence':'Original synthetic sample-candidate fixture, not whole acceptance',
        'inspected_source_frames':frame_refs(archive.root/'candidate-frames')}
    review['content_audio']={'status':'pending','actual_listening':False,'evidence':''}
    candidate=evidence(archive.root,'candidate-review-fixture.json',review)
    capability=evidence(archive.root,'candidate-capability-fixture.json',{
        'reviewer':'Original synthetic fixture only','target_fingerprint':target['fingerprint'],
        'actual_audio_received_and_listened':True,'continuous_picture_observed':True,
        'non_default_ass_player_verified':True,'evidence':'Synthetic scheduling test, not real QA',
        'expires_at':time.time()+3600})
    record={'target':target,'base_review':candidate,'capability_report':capability,
            'japanese_dialogue_report':qualification}
    db.set('subtitle_delivery_readiness_42_2',record)
    payload={'target':target,'workbook':None,'draft_ids':[freeze['base_id'],native]}
    db.execute("INSERT INTO subtitle_studio_jobs VALUES(42,2,'waiting_review',?,'',0)",(json.dumps(payload),))
    return db,q,e,archive,old,new,{**freeze,'base_review':candidate},target,record


@pytest.mark.requires_lab
def test_sample_only_candidate_dispatches_without_claiming_whole_acceptance(setup,monkeypatch):
    from anime.subtitle_delivery_materials import readiness
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    before=(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'))
    result=readiness(db,42,2,{})
    assert result['material_ready'] and result['clean_review_ready'] and result['capability_ready']
    assert result['accepted_verified'] is False
    scheduler=SubtitleDelivery(db);job=scheduler.record_preparation(42,2)
    assert job['state']=='ready'
    lease=scheduler.reserve(job['id'],'original-fixture-owner',job['revision'])
    intent=scheduler.begin_dispatch(job['id'],'original-fixture-owner',lease['lease_token'],lease['revision'])
    assert intent['action']=='create-thread'
    assert (db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'))==before
    assert not db.rows('SELECT * FROM subtitle_targets') and not db.rows('SELECT * FROM subtitle_publications')
    assert q.adds==0


@pytest.mark.requires_lab
def test_sample_only_review_remains_ineligible_for_formal_mother_freeze(setup,monkeypatch):
    from anime.subtitle_delivery_materials import readiness
    from anime.subtitle_targets import head
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    assert readiness(db,42,2,{})['clean_review_ready']
    before=db.rows('SELECT * FROM episodes')
    with pytest.raises(ValueError,match='clean'):
        e.subtitle_studio.production_targets.freeze(42,2,freeze)
    assert head(db,42,2) is None and db.rows('SELECT * FROM episodes')==before
    assert old.exists() and new.exists() and not db.rows('SELECT * FROM watches') and q.adds==0


@pytest.mark.parametrize('failure',['few','same_path','stale_sha','wrong_target','wrong_role','wrong_scope'])
@pytest.mark.requires_lab
def test_sample_candidate_review_cannot_claim_ready_with_invalid_frame_or_role_proofs(setup,monkeypatch,failure):
    from test_subtitle_targets import evidence
    from anime.subtitle_delivery_materials import readiness
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    ref=record['base_review'];path=Path(ref['path']);review=json.loads(path.read_bytes())
    frames=review['clean']['inspected_source_frames']
    if failure=='few':review['clean']['inspected_source_frames']=frames[:2]
    elif failure=='same_path':review['clean']['inspected_source_frames']=[frames[0]]*3
    elif failure=='stale_sha':Path(frames[0]['path']).write_bytes(synthetic_png(40,18))
    elif failure=='wrong_target':review['target_fingerprint']='other-fixture-target'
    elif failure=='wrong_role':review['role']='accepted_mother'
    else:review['scope']='whole_episode'
    record['base_review']=evidence(archive.root,'changed-candidate-review-fixture.json',review)
    db.set('subtitle_delivery_readiness_42_2',record)
    result=readiness(db,42,2,{})
    assert result['clean_review_ready'] is False and result['accepted_verified'] is False
    scheduler=SubtitleDelivery(db);job=scheduler.record_preparation(42,2)
    assert job['state']!='ready'
    with pytest.raises(ValueError):scheduler.reserve(job['id'],'fixture-owner',job['revision'])
    assert not db.rows('SELECT * FROM subtitle_targets') and q.adds==0
