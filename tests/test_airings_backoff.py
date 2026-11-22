"""Air-date refresh keeps failure memory: one probe per backoff during a Bangumi outage, sparse events."""
import time
from unittest.mock import Mock
import pytest
from anime.db import Store
from anime.engine import Engine
from anime.net import NetworkError
from anime.sources import Catalog


@pytest.fixture
def tracked(tmp_path,monkeypatch):
    db=Store(tmp_path/'test.db')
    for sid in (1,2,3,4):
        db.upsert_subject({'id':sid,'name':f'Example{sid}'});db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=?",(sid,))
    engine=Engine(db,qbit=Mock(),sources=Mock(),library=tmp_path/'library')
    clock={'now':time.time()};monkeypatch.setattr(time,'time',lambda:clock['now'])
    return db,engine,clock


def airings_events(db):return db.rows("SELECT message FROM events WHERE scope='airings'")


def bangumi(web,down):
    """A fake Bangumi API: subjects and one page of episodes, or a transport failure while down."""
    def fetch(url,**kw):
        if down['on']:raise NetworkError('SSLError：请求失败，请检查网络或本机接口（net.fetch:74）')
        if '/subjects/' in url:return Mock(json=lambda:{'id':int(url.rsplit('/',1)[1]),'name':'Example'})
        return Mock(json=lambda:{'total':1,'offset':0,'data':[{'id':int(url.split('subject_id=')[1].split('&')[0])*100,'sort':1,'ep':1,'airdate':'2000-01-01'}]})
    web.fetch.side_effect=fetch


def test_outage_is_probed_once_per_backoff_and_reported_once(tracked):
    db,engine,clock=tracked;web=Mock();down={'on':True};bangumi(web,down);engine.catalog=Catalog(db,web)
    engine.refresh_airings()
    assert web.fetch.call_count==1 and len(airings_events(db))==1
    assert db.get('airings_backoff')['failures']==1
    for _ in range(3):engine.refresh_airings()  # still inside the backoff
    assert web.fetch.call_count==1
    clock['now']+=61;engine.refresh_airings()
    assert web.fetch.call_count==2 and db.get('airings_backoff')['failures']==2 and len(airings_events(db))==1
    down['on']=False;clock['now']+=121;engine.refresh_airings()
    assert all(db.get(f'details_{sid}') for sid in (1,2,3,4))
    assert db.get('airings_backoff') is None and len(airings_events(db))==1


def test_one_show_failure_backs_off_only_that_show(tracked):
    db,engine,clock=tracked;engine.catalog=Mock()
    def details(sid):
        if sid==2:raise NetworkError('api.bgm.tv HTTP 404',404)
        db.set(f'details_{sid}',time.time())
    engine.catalog.details.side_effect=details
    engine.refresh_airings()
    assert [c.args[0] for c in engine.catalog.details.call_args_list]==[1,2,3,4]
    assert db.get('details_retry_2')['failures']==1 and db.get('airings_backoff') is None
    engine.refresh_airings()
    assert engine.catalog.details.call_count==4  # show 2 waits for its own retry time
    clock['now']+=301;engine.refresh_airings()
    assert engine.catalog.details.call_count==5 and len(airings_events(db))==1


def test_bad_pagination_does_not_stop_other_shows(tracked):
    db,engine,clock=tracked;web=Mock();down={'on':False};bangumi(web,down);engine.catalog=Catalog(db,web)
    ok=web.fetch.side_effect
    def fetch(url,**kw):
        if 'subject_id=1&' in url:return Mock(json=lambda:{'total':'x','data':None})
        return ok(url,**kw)
    web.fetch.side_effect=fetch
    engine.refresh_airings()
    assert db.get('details_retry_1') and db.get('airings_backoff') is None
    assert all(db.get(f'details_{sid}') for sid in (2,3,4))


def test_retry_after_is_respected_and_heartbeat_names_the_phase(tracked):
    db,engine,clock=tracked;engine.catalog=Mock();seen=[]
    def details(sid):
        seen.append(db.get('heartbeat')['phase']);raise NetworkError('api.bgm.tv HTTP 429',429,retry_after=7200)
    engine.catalog.details.side_effect=details
    engine.refresh_airings()
    assert seen==['airings'] and db.get('airings_backoff')['next']>=clock['now']+7200


def test_manual_refresh_success_ends_the_recorded_outage(tracked):
    db,engine,clock=tracked;engine.catalog=Mock()
    engine.catalog.details.side_effect=NetworkError('x')
    engine.refresh_airings();assert len(airings_events(db))==1
    clock['now']+=10;db.set('details_4',time.time())  # e.g. POST /api/shows/4/refresh succeeded
    clock['now']+=4000;engine.catalog.details.side_effect=NetworkError('x')
    engine.refresh_airings()
    assert len(airings_events(db))==2 and db.get('airings_backoff')['failures']==1
