import json
from pathlib import Path
import pytest
from test_reliability import setup
from test_source_review import ready_review,finish_sample


def prepared(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);t,s,fp=finish_sample(e,r)
    review={'visual':80,'translation':84,'chinese_verified':True,'confidence':'medium','reviewer':'test',
        'evidence':'sampled actual comparison','original_basis':'Japanese dialogue'}
    return db,q,e,db.one('SELECT * FROM source_samples WHERE candidate_id=?',(c['id'],)),c,review


def test_history_preserves_corrections_reverts_and_idempotent_backfill(setup):
    db,q,e,s,c,review=prepared(setup)
    e.source_review.record_review(42,c['id'],review)
    e.source_review.record_review(42,c['id'],{**review,'translation':60})
    e.source_review.record_review(42,c['id'],review)
    assert len(e.source_review.history.report(latest=False)['records'])==3
    assert e.source_review.history.report()['records'][0]['translation_score']==84
    assert e.source_review.history.archive_existing()['inserted']==0
    assert e.source_review.history.archive_existing()['inserted']==0
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches') and q.adds==0


def test_legacy_review_is_archived_before_overwrite(setup):
    db,q,e,s,c,review=prepared(setup)
    db.execute('UPDATE source_samples SET review=? WHERE candidate_id=?',(json.dumps({**review,'fingerprint':json.loads(s['evidence'])['fingerprint']}),c['id']))
    e.source_review.record_review(42,c['id'],{**review,'translation':85})
    records=e.source_review.history.report(latest=False)['records']
    assert [r['translation_score'] for r in records]==[85,84]


def test_review_and_history_are_atomic(setup,monkeypatch):
    db,q,e,s,c,review=prepared(setup)
    def failure(*args):raise RuntimeError('archive unavailable')
    monkeypatch.setattr(e.source_review.history,'append',failure)
    with pytest.raises(RuntimeError):e.source_review.record_review(42,c['id'],review)
    assert json.loads(db.one('SELECT review FROM source_samples')['review'])=={}
    assert not db.rows('SELECT * FROM subtitle_review_history')


def test_chinese_text_identity_ignores_timing_and_never_uses_japanese_or_ocr(setup):
    db,q,e,s,c,review=prepared(setup)
    directory=e.source_review.root/'evidence'/str(c['id']);directory.mkdir(parents=True,exist_ok=True)
    (directory/'track-2.vtt').write_text('WEBVTT\n\n00:01.000 --> 00:02.000\n你好\n',encoding='utf-8')
    (directory/'track-3.vtt').write_text('WEBVTT\n\n00:05.000 --> 00:06.000\n你好\n',encoding='utf-8')
    evidence=json.loads(s['evidence']);evidence['tracks']=[{'index':2,'file':'track-2.vtt','language':'chi'},
        {'index':3,'file':'track-3.vtt','language':'jpn'}]
    s['evidence']=json.dumps(evidence);review['fingerprint']=evidence['fingerprint']
    a=e.source_review.history.snapshot(s,review);assert len(a['chinese_text_tracks'])==1
    evidence['tracks'][0]['file']='track-3.vtt';s['evidence']=json.dumps(evidence)
    b=e.source_review.history.snapshot(s,review)
    assert a['chinese_text_tracks'][0]['text_sha256']==b['chinese_text_tracks'][0]['text_sha256']
    review['fingerprint']='old-file';unknown=e.source_review.history.snapshot(s,review)
    assert unknown['chinese_text_tracks'][0]['text_sha256'] is None and not unknown['text_identity_bound_to_review']


def test_shared_transcript_is_linked_only_for_same_show_and_episode(setup):
    db,q,e,s,c,review=prepared(setup)
    payload=e.source_review.history.snapshot(s,review)
    payload['chinese_text_tracks']=[{'text_sha256':'same','cues':10}]
    with db.connect() as con:
        e.source_review.history.append(con,payload)
        e.source_review.history.append(con,{**payload,'candidate_id':99,'publisher':'Other'})
        e.source_review.history.append(con,{**payload,'candidate_id':100,'episode':2})
    records=e.source_review.history.report()['records']
    original=next(r for r in records if r['candidate_id']==c['id'])
    assert [x['candidate_id'] for x in original['shared_text_with']]==[99]
    assert not next(r for r in records if r['episode']==2)['shared_text_with']


def test_exclusion_and_unstructured_findings_are_unknown_not_zero(setup):
    db,q,e,s,c,review=prepared(setup)
    e.source_review.record_review(42,c['id'],{**review,'decision':'excluded','evidence':'Japanese only'})
    record=e.source_review.history.report()['records'][0]
    assert record['translation_score'] is None and record['findings_coverage']=='not_structured'
    assert 'url' not in record and 'selection' not in record
    assert not db.rows('SELECT * FROM watches') and q.adds==0


def test_bad_findings_do_not_write_anything(setup):
    db,q,e,s,c,review=prepared(setup)
    with pytest.raises(ValueError):e.source_review.record_review(42,c['id'],{**review,'subtitle_findings':[{'seconds':-1}]})
    assert not db.rows('SELECT * FROM subtitle_review_history')


def test_history_api_export_and_same_origin_write_gate(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db,q,e,s,c,review=prepared(setup);e.source_review.record_review(42,c['id'],review)
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        assert client.get('/subtitle-reviews').status_code==200
        data=client.get('/api/source-review/subtitles/history?show_id=42').json()
        assert data['records'][0]['translation_score']==84
        assert client.get('/api/source-review/subtitles/history?show_id=999').json()['records']==[]
        exported=client.get('/api/source-review/subtitles/export').json()
        assert exported['latest_only'] is False and len(exported['records'])==1
        assert client.post('/api/source-review/subtitles/archive').status_code==403
        token=client.get('/api/bootstrap').json()['token']
        assert client.post('/api/source-review/subtitles/archive',headers={'X-Anime-Token':token}).json()['inserted']==0
    assert q.adds==0
