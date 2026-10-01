import hashlib
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from anime.db import Store
from anime.webplayer import WebPlayer


@pytest.fixture
def fixture(tmp_path):
    db=Store(tmp_path/'test.db')
    path=tmp_path/'episode.mkv';path.write_bytes(b'local fixture')
    db.execute('INSERT INTO shows(id,title) VALUES(1,?)',('Example Season 2',))
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(1,1,?,'complete',?)",(str(path),path.stat().st_size))
    player=WebPlayer(db)
    key=hashlib.sha256(str(path).lower().encode()).hexdigest()[:32]
    source=player.bind_episodes({'bangumi':{'episodes':[{'url':'https://www.bilibili.com/video/BV1EVap6yE6B?p=1'}]}})['bangumi']['episodes'][0]
    player.alignment=Mock()
    player.alignment.start.return_value={'job_id':'job','status':'queued','message':'waiting'}
    player.alignment.status.return_value={'job_id':'job','status':'matched','offset':20}
    return db,player,key,source,path


def test_registered_source_requires_verified_pair_and_uses_db_url(fixture):
    db,player,key,source,path=fixture
    result=player.start_alignment(key,{'source_id':source['source_key'],'url':'https://evil.test/private'})
    assert result['status']=='unavailable';player.alignment.start.assert_not_called()
    result=player.start_alignment(key,{'source_id':source['source_key'],'confirmed':True})
    assert result['status']=='queued'
    args=player.alignment.start.call_args
    assert args.args[1]=='https://www.bilibili.com/video/BV1EVap6yE6B?p=1'
    assert args.args[2]==source['source_identity']
    assert player.alignment_status(key,'job')['offset']==20
    player.alignment_status(key,'job',cancel=True);player.alignment.cancel.assert_called_once_with('job')


def test_unverified_media_and_unregistered_source_never_start(fixture):
    db,player,key,source,path=fixture
    with pytest.raises(ValueError):player.start_alignment(key,{'source_id':'not-registered','confirmed':True})
    db.execute("UPDATE episodes SET status='legacy_unverified'")
    player.inventory=[{'path':str(path),'size':path.stat().st_size}]
    assert player.start_alignment(key,{'source_id':source['source_key'],'confirmed':True})['status']=='unavailable'
    player.alignment.start.assert_not_called()


def test_auto_match_records_pair_without_changing_episode_or_watches(fixture,monkeypatch):
    db,player,key,source,path=fixture
    monkeypatch.setattr('anime.webplayer.match_danmu',lambda *_:{'status':'matched','selected':[{'id':source['source_key']}]})
    player.auto_danmu(key)
    assert db.one('SELECT 1 FROM web_danmu_matches WHERE media_id=? AND source_key=?',(key,source['source_key']))
    player.start_alignment(key,{'source_id':source['source_key']})
    player.alignment.start.assert_called_once()
    assert db.one('SELECT episode,status FROM episodes')=={'episode':1,'status':'complete'}
    assert not db.rows('SELECT * FROM watches')


def test_poll_requires_media_binding_and_current_file(fixture):
    db,player,key,source,path=fixture
    with pytest.raises(HTTPException):player.alignment_status(key,'another-job')
    player.start_alignment(key,{'source_id':source['source_key'],'confirmed':True})
    path.write_bytes(b'changed')
    with pytest.raises(ValueError):player.alignment_status(key,'job')


def test_episode_reassignment_invalidates_previous_pair_proof(fixture):
    db,player,key,source,path=fixture
    player.start_alignment(key,{'source_id':source['source_key'],'confirmed':True})
    db.execute('UPDATE episodes SET episode=2')
    assert player.start_alignment(key,{'source_id':source['source_key']})['status']=='unavailable'
    with pytest.raises(HTTPException):player.alignment_status(key,'job')
