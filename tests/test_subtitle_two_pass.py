"""Synthetic release files and temporary databases only; no real downloader."""
import hashlib
import json
import shutil

import pytest
from test_reliability import setup
from test_subtitle_delivery_integration import prepared_candidate
from test_subtitle_delivery_first import release_fixture
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_refinement import SECTIONS, enqueue_refinement, validate_review
from anime.subtitle_delivery_view import report_progress


def start(db):
    db.set('subtitle_delivery_policy', {'first_release_mode': 'finished', 'editions': ['v0','v1']})
    s=SubtitleDelivery(db); j=s.record_preparation(42,2)
    lease=s.reserve(j['id'],'dispatcher',j['revision'])
    intent=s.begin_dispatch(j['id'],'dispatcher',lease['lease_token'],lease['revision'])
    s.reconcile_dispatch(j['id'],intent['dispatch_token'],thread_id='writer')
    return s,j['id']


def mark_edition(db, root, edition, baseline=None):
    r=json.loads((root/'release.json').read_text());r['edition']=edition
    if baseline:r['baseline_release_id']=baseline
    (root/'release.json').write_text(json.dumps(r))
    index=db.path.parent.parent/'static/finished-media/index.json'
    data=json.loads(index.read_text())
    next(x for x in data['releases'] if x['id']==r['id'])['edition']=edition
    index.write_text(json.dumps(data))
    return r


def make_review(project, root, r, baseline):
    evidence=project/'data/subtitle-studio/scene-evidence.json'
    evidence.write_text('synthetic evidence fixture, no actual viewing claim')
    ref={'path':str(evidence),'sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}
    report={'role':'episode_v1_refinement_review','show_id':42,'episode':2,
            'source_media_sha256':r['source_media_sha256'],'baseline_release_id':baseline,
            'reviewer':'synthetic fixture only','sections':{k:{'status':'done','work_done':'synthetic scene/lyrics check fixture','evidence_refs':[ref]} for k in SECTIONS}}
    path=root/'refinement-review.json';path.write_text(json.dumps(report))
    return path,report


def test_v0_keeps_single_writer_until_evidenced_v1(setup,monkeypatch):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    before=(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches'))
    s,identity=start(db);root,proof=release_fixture(db,target);v0=mark_edition(db,root,'v0')
    first=s.record_finished(identity,'writer',root,proof)
    assert first['continue_refinement'] and first['state']=='production'
    assert first['dispatch_state']=='acknowledged' and s.has_active(42,2)
    assert s.record_finished(identity,'writer',root,proof)['duplicate']
    report_progress(db,identity,'writer','lyrics')
    s.refresh();assert s.get(identity)['state']=='production'
    assert not enqueue_refinement(s,identity)  # never create another writer for active v1
    s.record_blocked(identity,'writer',['synthetic tool interruption'],owner_stopped=True)
    saved=s.get(identity);lease=s.resume(identity,'writer','dispatcher',saved['revision'])
    intent=s.begin_dispatch(identity,'dispatcher',lease['lease_token'],lease['revision'])
    s.reconcile_dispatch(identity,intent['dispatch_token'],thread_id='writer')
    v1root=root.with_name('v2');shutil.copytree(root,v1root)
    r=json.loads((v1root/'release.json').read_text());r['id']='fixture-refined-v1'
    r['edition']='v1';r['baseline_release_id']=v0['id']
    reportpath,report=make_review(db.path.parent.parent,v1root,r,v0['id'])
    r['files']['refinement-review.json']={'bytes':reportpath.stat().st_size,'sha256':hashlib.sha256(reportpath.read_bytes()).hexdigest()}
    (v1root/'release.json').write_text(json.dumps(r))
    p=json.loads(proof.read_text());p['release_id']=r['id'];v1proof=v1root/'player.json';v1proof.write_text(json.dumps(p))
    static=db.path.parent.parent/'static/finished-media';(static/r['id']).mkdir()
    shutil.copy2(v1root/'episode.mp4',static/r['id']/'episode.mp4')
    index=json.loads((static/'index.json').read_text());index['releases'].append({'id':r['id'],'show_id':42,'episode':2,'status':'finished','edition':'v1','video_sha256':p['video_sha256']});(static/'index.json').write_text(json.dumps(index))
    final=s.record_finished(identity,'writer',v1root,v1proof)
    assert final['state']=='finished' and final['dispatch_state']=='completed' and not final['continue_refinement']
    assert not s.has_active(42,2) and not final['accepted_build_id']
    assert db.get('subtitle_delivery_v0_42_2')['release_id']==v0['id']
    assert db.get('subtitle_delivery_finished_42_2')['edition']=='v1'
    assert before==(db.rows('SELECT * FROM episodes'),db.rows('SELECT * FROM watches')) and q.adds==0


@pytest.mark.parametrize('failure',['missing_section','missing_evidence','changed_evidence','missing_issue','wrong_baseline'])
def test_v1_rejects_unperformed_or_unbound_work(setup,monkeypatch,failure):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    root,proof=release_fixture(db,target);r=json.loads((root/'release.json').read_text())
    path,report=make_review(db.path.parent.parent,root,r,'v0-fixture')
    if failure=='missing_section':del report['sections']['effects']
    if failure=='missing_evidence':report['sections']['op']['evidence_refs']=[]
    if failure=='changed_evidence':report['sections']['op']['evidence_refs'][0]['sha256']='wrong'
    if failure=='missing_issue':report['sections']['ed']['status']='unresolved'
    if failure=='wrong_baseline':report['baseline_release_id']='wrong'
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):validate_review(db.path.parent.parent,path,42,2,r['source_media_sha256'],'v0-fixture')
    assert q.adds==0


@pytest.mark.parametrize('legacy_state',['finished','blocked','ready'])
def test_existing_unwatched_basic_gets_one_same_chat_refinement_and_watched_is_frozen(setup,monkeypatch,legacy_state):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    s=SubtitleDelivery(db);j=s.record_preparation(42,2)
    lease=s.reserve(j['id'],'d',j['revision']);intent=s.begin_dispatch(j['id'],'d',lease['lease_token'],lease['revision'])
    s.reconcile_dispatch(j['id'],intent['dispatch_token'],thread_id='old-writer')
    root,proof=release_fixture(db,target);s.record_finished(j['id'],'old-writer',root,proof)
    db.set('subtitle_delivery_policy',{'editions':['v0','v1']})
    db.execute('UPDATE subtitle_delivery_jobs SET state=? WHERE id=?',(legacy_state,j['id']))
    refine=enqueue_refinement(s,j['id'])
    assert refine['kind']=='refinement' and refine['thread_id']=='old-writer'
    assert enqueue_refinement(s,j['id'])['id']==refine['id']
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    assert enqueue_refinement(s,j['id']) is None
    with pytest.raises(ValueError):s.reserve(refine['id'],'d',refine['revision'],allow_experience=True)
    assert len(db.rows("SELECT * FROM subtitle_delivery_jobs WHERE kind='refinement'"))==1 and q.adds==0


def test_explicit_stopped_owner_is_preserved_and_private_refinement_gets_no_old_chat(setup,monkeypatch):
    db,q,e,archive,old,new,freeze,target,record=prepared_candidate(setup,monkeypatch)
    s=SubtitleDelivery(db);j=s.record_preparation(42,2)
    lease=s.reserve(j['id'],'d',j['revision']);intent=s.begin_dispatch(j['id'],'d',lease['lease_token'],lease['revision'])
    s.reconcile_dispatch(j['id'],intent['dispatch_token'],thread_id='stopped-big-chat')
    root,proof=release_fixture(db,target);s.record_finished(j['id'],'stopped-big-chat',root,proof)
    db.execute("UPDATE subtitle_delivery_jobs SET state='blocked',dispatch_state='blocked-stopped' WHERE id=?",(j['id'],))
    original=s.get(j['id'])
    db.set('subtitle_delivery_policy',{'editions':['v0','v1'],'stopped_owner_refinement':'isolated_private','isolated_refinement_stopped_threads':['stopped-big-chat']})
    refine=enqueue_refinement(s,j['id'])
    assert refine['thread_id']=='' and refine['payload']['isolated_owner_required'] is True
    assert s.get(j['id'])==original
    db.execute("UPDATE subtitle_delivery_jobs SET dispatch_state='unknown' WHERE id=?",(j['id'],))
    assert enqueue_refinement(s,j['id']) is None
    assert q.adds==0
