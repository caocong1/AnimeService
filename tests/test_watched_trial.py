import pytest
from unittest.mock import Mock
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path/'test.db')


def add(db,sid=1,state='wish',selected=1,quarter='2026-10',total=12):
    db.execute('INSERT INTO shows(id,title,state,selected,quarter,total) VALUES(?,?,?,?,?,?)',
               (sid,'Test',state,selected,quarter,total))


@pytest.mark.parametrize('method',['manual','dandan','manual-dandan'])
def test_wish_promoted_by_shared_finished_record(db,method):
    add(db)
    db.execute('INSERT INTO watches(show_id,episode,finished,position,method) VALUES(1,1,0,100,?)',(method,))
    assert db.show(1)['state']=='wish'
    db.execute('UPDATE watches SET finished=1 WHERE show_id=1')
    assert db.show(1)['state']=='trial'
    # User choices stick across repeat history sync and unwatch.
    db.execute("UPDATE shows SET state='wish' WHERE id=1")
    db.execute('UPDATE watches SET finished=1 WHERE show_id=1')
    assert db.show(1)['state']=='wish'
    db.execute('UPDATE watches SET finished=0 WHERE show_id=1')
    assert db.show(1)['state']=='wish'


@pytest.mark.parametrize('state,selected',[('watching',1),('paused',1),('dropped',1),('completed',1),('wish',0)])
def test_existing_choices_and_unselected_are_preserved(db,state,selected):
    add(db,state=state,selected=selected)
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(1,1,1)')
    assert db.show(1)['state']==state


def test_backfill_only_target_quarter_including_continuing(db):
    for sid,quarter in [(1,'2026-10'),(2,'2026-07'),(3,'2026-07'),(4,'2026-10')]:
        add(db,sid,quarter=quarter)
        if sid!=4:db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(?,1,1)',(sid,))
    db.execute("UPDATE shows SET state='wish'")  # Existing records before deployment.
    db.execute("INSERT INTO quarter_members VALUES(2,'2026-10','continuing','test')")
    assert {r['id'] for r in db.promote_watched_wishes('2026-10')}=={1,2}
    assert db.show(3)['state']==db.show(4)['state']=='wish'
    assert db.promote_watched_wishes('2026-10')==[]


def test_api_returns_trial_and_completion_keeps_priority(db):
    add(db)
    engine=Mock()
    engine.state.side_effect=lambda sid,state:db.execute('UPDATE shows SET state=? WHERE id=?',(state,sid))
    client=TestClient(create_app(db,engine,start_worker=False),base_url='http://127.0.0.1:4871')
    client.headers['X-Anime-Token']=client.get('/api/bootstrap').json()['token']
    assert client.post('/api/shows/1/watch',json={'episode':1,'finished':True}).json()['state']=='trial'
    assert db.show(1)['state']=='trial'
    db.execute('UPDATE shows SET total=2 WHERE id=1')
    assert client.post('/api/shows/1/watch',json={'episode':2,'finished':True}).json()['state']=='completed'
    assert db.show(1)['state']=='completed'


def test_list_exposes_season_membership(db):
    add(db,quarter='2026-07')
    db.execute("INSERT INTO quarter_members VALUES(1,'2026-10','continuing','test')")
    client=TestClient(create_app(db,Mock(),start_worker=False),base_url='http://127.0.0.1:4871')
    row=client.get('/api/shows?scope=history').json()[0]
    assert row['quarters']==['2026-07','2026-10']
