import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from anime.app import create_app
from anime.subtitle_studio import archive_for,lab_core
from anime.subtitles import Subtitles
from test_reliability import setup
from test_subtitle_studio import ready
import pytest


def draft_job(setup):
    db,q,engine,reader,candidate=ready(setup)
    video=db.path.parent/'original.mkv';video.write_bytes(b'fixture')
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(42,2,?,7,'complete')",(str(video),))
    archive=archive_for(db);target={'path':str(video),'size':7,'fingerprint':lab_core().fingerprint(video)}
    base=db.path.parent/'cn.vtt';base.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n明天再见。\n',encoding='utf-8')
    archive.add_base(42,1,2,base,{'source_key':'fixture','language':'chi','origin':'embedded','media_fingerprint':target['fingerprint'],'release':'[Group] Example S01E02'})
    engine.subtitle_studio.refresh_job(42,2)
    return db,q,engine,video,target


@pytest.mark.requires_lab
def test_library_draft_progress_and_history_are_read_only(setup):
    db,q,engine,video,target=draft_job(setup)
    payload=json.loads(db.one('SELECT payload FROM subtitle_studio_jobs')['payload'])
    path=Path(payload['workbook']);book=json.loads(path.read_text(encoding='utf-8'))
    book['cues'][0]['status']='translation_reviewed';path.write_text(json.dumps(book),encoding='utf-8')
    before=(db.show(42),db.rows('SELECT * FROM episodes'),path.read_bytes())
    result=engine.subtitle_studio.library();row=result['entries'][0]
    assert row['progress']=={'total':1,'reviewed':1} and not row['build_id']
    assert row['target_matches_current']
    assert row['video_file']=='original.mkv' and row['versions']==[]
    assert str(db.path.parent) not in json.dumps(result)
    db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    assert engine.subtitle_studio.overview()['jobs']==[]
    assert len(engine.subtitle_studio.library()['entries'])==1
    assert not engine.subtitle_studio.library()['entries'][0]['editable']
    db.execute("UPDATE shows SET state=? WHERE id=42",(before[0]['state'],))
    assert (db.show(42),db.rows('SELECT * FROM episodes'),path.read_bytes())==before
    assert not db.rows('SELECT * FROM watches') and q.adds==0


@pytest.mark.requires_lab
def test_library_selected_version_requires_current_accessible_video(setup,monkeypatch):
    db,q,engine,video,target=draft_job(setup)
    selected={'id':'version-fixture','files':{'bilingual.ass':{'path':str(db.path.parent/'bilingual.ass')}}}
    monkeypatch.setattr('anime.subtitle_studio.authored',lambda db,item:selected)
    result=engine.subtitle_studio.library();row=result['entries'][0]
    assert row['build_id']=='version-fixture' and row['subtitle_file']=='bilingual.ass'
    assert result['summary']['accepted']==1
    video.write_bytes(b'changed file')
    result=engine.subtitle_studio.library()
    assert result['summary']['accepted']==0 and result['entries'][0]['build_id'] is None
    assert not result['entries'][0]['target_matches_current']
    assert result['entries'][0]['state']!='accepted'


@pytest.mark.requires_lab
def test_player_reports_embedded_and_authored_files_without_private_paths(setup,monkeypatch):
    db,q,engine,video,target=draft_job(setup)
    app=create_app(db,engine,start_worker=False)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    monkeypatch.setattr(Subtitles,'tracks',lambda self,item:[{'index':3,'codec':'subrip','language':'chi','label':'中文','supported':True}])
    key=hashlib.sha256(str(video).lower().encode()).hexdigest()[:32]
    tracks=client.get(f'/api/web/media/{key}/subtitles').json()
    assert tracks['tracks'][0]['origin']=='embedded' and tracks['tracks'][0]['file_name']=='original.mkv'
    assert tracks['default']==3
    selected={'id':'version-fixture','files':{'bilingual.ass':{'path':str(db.path.parent/'bilingual.ass')}}}
    monkeypatch.setattr('anime.subtitle_studio.authored',lambda db,item:selected)
    tracks=client.get(f'/api/web/media/{key}/subtitles').json()
    assert tracks['default']==-1000 and tracks['tracks'][0]['origin']=='authored'
    assert tracks['tracks'][0]['build_id']=='version-fixture' and tracks['tracks'][0]['file_name']=='bilingual.ass'
    assert str(db.path.parent) not in json.dumps(tracks)
    assert client.get('/subtitle-library').status_code==200
    assert client.get('/api/subtitle-library').json()['summary']['accepted']==1
    assert not db.rows('SELECT * FROM watches') and q.adds==0
