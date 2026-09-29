import datetime,time
from pathlib import Path
from anime.db import Store
from anime.dandan_history import DandanHistory


def sample(tmp_path):
    db=Store(tmp_path/'test.db');db.upsert_subject({'id':42,'name':'Test','total_episodes':12})
    db.execute("UPDATE shows SET selected=1,state='dropped',authorized=0,cleanup_hold=1 WHERE id=42")
    h=DandanHistory(db);p=tmp_path/'ep.mp4';p.write_bytes(b'video')
    media={'a':{'id':'a','show_id':42,'episode':2,'path':str(p),'size':5,'name':p.name,'folder':'Test'}}
    played=time.time()-200*86400
    iso=datetime.datetime.fromtimestamp(played,datetime.timezone.utc).isoformat()
    data={'recent':[{'path':str(p),'played':iso,'seconds':1200,'fraction':1}],
          'files':[{'path':str(p),'size':5,'duration':1200,'episode_id':'9010002'}],
          'seen':[{'episode_id':'9010002','watched':iso}]}
    return db,h,media,data,played


def test_player_watched_flag_updates_count_without_changing_subscription(tmp_path):
    db,h,media,data,t=sample(tmp_path);r=h.merge(data,media)
    assert r['seen_files']==1 and r['resume_files']==1
    assert db.one('SELECT * FROM watches')['method']=='dandan'
    assert db.one('SELECT * FROM watches')['finished']==1
    assert db.show(42)['watched']==1 and r['synced_watched']==1
    assert abs(db.one('SELECT * FROM watches')['updated']-t)<0.01
    s=db.show(42);assert s['state']=='dropped' and s['cleanup_hold']==1 and s['authorized']==0
    p=db.one('SELECT * FROM web_progress');assert abs(p['last_played']-t)<0.01
    assert p['position']==1200 and abs(p['updated']-t)<0.01


def test_new_web_resume_and_manual_correction_win_repeated_import(tmp_path):
    db,h,media,data,t=sample(tmp_path);h.merge(data,media);h.confirm(['a'])
    db.execute("UPDATE watches SET finished=0,method='manual'")
    db.execute('UPDATE web_progress SET position=100,updated=?',(time.time(),))
    h.merge(data,media);h.merge(data,media)
    assert db.one('SELECT * FROM web_progress')['position']==100
    assert db.one('SELECT * FROM watches')['finished']==0
    assert db.show(42)['watched']==0
    assert db.one('SELECT count(*) n FROM dandan_history')['n']==1


def test_batch_confirmation_is_explicit_and_does_not_reset_activity_age(tmp_path):
    db,h,media,data,t=sample(tmp_path);h.merge(data,media);h.confirm(['a'])
    assert db.show(42)['watched']==1 and db.show(42)['state']=='dropped'
    w=db.one('SELECT * FROM watches');assert abs(w['updated']-t)<0.01
    assert w['method']=='manual-dandan'


def test_wrong_path_size_and_deleted_files_are_not_matched(tmp_path):
    db,h,media,data,t=sample(tmp_path)
    data['files'][0]['size']=6;assert h.merge(data,media)['matched_files']==0
    data['files'][0]['size']=5;data['files'][0]['path']+='wrong';assert h.merge(data,media)['matched_files']==0
    data['files'][0]['path']=media['a']['path'];Path(media['a']['path']).unlink()
    assert h.merge(data,media)['matched_files']==0


def test_open_only_and_invalid_position_never_become_actual_progress(tmp_path):
    db,h,media,data,t=sample(tmp_path);data['recent'][0]['seconds']=0
    data['seen']=[]
    h.merge(data,media);assert not db.rows('SELECT * FROM web_progress')
    assert not db.rows('SELECT * FROM watches')
    data['recent'][0]['seconds']=float('nan');h.merge(data,media)
    assert not db.rows('SELECT * FROM web_progress')


def test_api_cards_details_and_live_counts_agree_after_sync(tmp_path):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    from unittest.mock import Mock
    db,h,media,data,t=sample(tmp_path);h.merge(data,media)
    engine=Mock();engine.gaps.return_value={}
    app=create_app(db=db,engine=engine,start_worker=False)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    assert client.get('/api/shows?scope=history').json()[0]['watched']==1
    detail=client.get('/api/shows/42').json()
    assert detail['show']['watched']==1 and detail['watches'][0]['method']=='dandan'
    assert client.get('/api/status').json()['watch_counts'][0]['watched']==1


def test_episode_link_removed_cannot_confirm_stale_season(tmp_path):
    import pytest
    db,h,media,data,t=sample(tmp_path);h.merge(data,media)
    h.merge(data,{})
    with pytest.raises(ValueError):h.confirm(['a'])
    # Previously verified watched history survives removal of the local file/link.
    assert db.show(42)['watched']==1


def test_failed_extraction_preserves_history_and_exposes_error(tmp_path):
    db,h,media,data,t=sample(tmp_path);h.merge(data,media)
    def fail():raise OSError('arbitrary secret not suitable for logs')
    h.extract=fail;result=h.sync()
    assert not result['ok'] and 'secret' not in result['error']
    assert db.one('SELECT count(*) n FROM dandan_history')['n']==1


def test_removed_or_ambiguous_account_flags_do_not_linger(tmp_path):
    db,h,media,data,t=sample(tmp_path);h.merge(data,media)
    data['seen']=[];data['account_scope']='ambiguous';h.merge(data,media)
    assert db.one('SELECT * FROM dandan_history')['source_watched'] is None
    assert db.one('SELECT * FROM dandan_history')['position']==1200


def test_end_position_without_player_watched_flag_is_not_watched(tmp_path):
    db,h,media,data,t=sample(tmp_path);data['seen']=[]
    h.merge(data,media)
    assert not db.rows('SELECT * FROM watches') and db.show(42)['watched']==0


def test_same_episode_multiple_versions_count_once_and_restart_is_idempotent(tmp_path):
    db,h,media,data,t=sample(tmp_path)
    media['b']={**media['a'],'id':'b'}
    h.merge(data,media);DandanHistory(db).merge(data,media)
    assert db.one('SELECT count(*) n FROM watches')['n']==1
    assert db.show(42)['watched']==1


def test_player_flag_without_resume_is_imported_but_unlinked_history_is_not(tmp_path):
    db,h,media,data,t=sample(tmp_path);data['recent']=[]
    del media['a']['show_id'];del media['a']['episode']
    h.merge(data,media);assert not db.rows('SELECT * FROM watches')
    media['a'].update(show_id=42,episode=2)
    h.merge(data,media);assert db.show(42)['watched']==1
    assert not db.rows('SELECT * FROM web_progress')


def test_history_api_requires_local_token_for_sync_and_confirm(tmp_path):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    from unittest.mock import Mock
    db,h,media,data,t=sample(tmp_path);h.merge(data,media)
    app=create_app(db=db,engine=Mock(),start_worker=False)
    client=TestClient(app,base_url='http://127.0.0.1:4871')
    assert client.get('/api/history/dandan').json()['total']==1
    assert client.post('/api/history/dandan/sync',json={}).status_code==403
    assert client.post('/api/history/dandan/confirm',json={'media_ids':['a']}).status_code==403
    token=client.get('/api/bootstrap').json()['token']
    assert client.post('/api/history/dandan/confirm',headers={'x-anime-token':token},json={'media_ids':['a']}).json()['confirmed']==1
