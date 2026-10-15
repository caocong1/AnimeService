import json
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store
from anime.sources import Catalog
from anime.discovery import discover,import_subject

def subject(sid,platform='TV',date='2026-10-01',**extra):
    return {'id':sid,'type':2,'name':'アニメ'+str(sid),'name_cn':'测试'+str(sid),
            'platform':platform,'date':date,'tags':[{'name':'日本'}],**extra}

@pytest.fixture
def setup(tmp_path):
    db=Store(tmp_path/'discovery.db');web=Mock();eng=Mock();eng.catalog=Catalog(db,web)
    client=TestClient(create_app(db,eng,False),base_url='http://127.0.0.1:4871')
    return db,web,eng,client

def test_cross_year_types_and_verified_continuing_without_mutation(setup):
    db,web,eng,client=setup
    for item in [subject(1),subject(2,'剧场版','2025-03-01'),subject(3,'OVA','2026-07-01'),subject(4,'WEB'),subject(5,'Movie'),subject(6,'OAD')]:db.upsert_subject(item)
    db.execute("UPDATE shows SET selected=1,state='dropped',cleanup_hold=1 WHERE id=2")
    db.execute("INSERT INTO quarter_members VALUES(3,'2026-10','continuing','verified')")
    before=db.rows('SELECT * FROM shows')
    assert {s['id'] for s in client.get('/api/shows?kind=all').json()}=={1,2,3,4,5,6}
    assert {s['id'] for s in client.get('/api/shows?kind=movie').json()}=={2,5}
    assert {s['id'] for s in client.get('/api/shows?kind=ova').json()}=={3,6}
    assert {s['id'] for s in client.get('/api/shows?kind=series').json()}=={1,4}
    assert [s['id'] for s in client.get('/api/shows?kind=all&year=2025').json()]==[2]
    assert [s['id'] for s in client.get('/api/shows?kind=continuing').json()]==[3]
    assert client.get('/api/shows?kind=continuing&quarter=2026-07').json()==[]
    assert client.get('/api/shows?kind=all&quarter=2026-10').json()[0]['quarter_kind'] in ('new','continuing')
    assert client.get('/api/shows?kind=invalid').status_code==400
    assert client.get('/api/shows?year=1').status_code==400
    assert 2025 in client.get('/api/bootstrap').json()['catalog_years']
    assert db.rows('SELECT * FROM shows')==before
    web.fetch.assert_not_called();eng.state.assert_not_called()

def test_discovery_search_reads_only_and_does_not_apply_quarter(setup):
    db,web,eng,client=setup
    db.upsert_subject(subject(2,'剧场版','2025-03-01'));db.execute("UPDATE shows SET selected=1,state='completed',cleanup_hold=1 WHERE id=2")
    before=db.rows('SELECT * FROM shows')
    web.fetch.return_value=Mock(json=lambda:{'total':5,'offset':0,'data':[subject(2,'Movie','2025-03-01'),subject(3,'OVA'),subject(4,'Movie',tags=[{'name':'欧美'}],name='Example'),subject(5,'Movie',nsfw=True)]})
    r=client.get('/api/catalog/discover?q=测试&kind=movie').json()
    assert [x['id'] for x in r['items']]==[2] and r['items'][0]['state']=='completed'
    assert r['next_offset']==4 and r['page_filtered']
    args=web.fetch.call_args.kwargs['json'];assert args['filter']=={'type':[2],'nsfw':False}
    assert db.rows('SELECT * FROM shows')==before and not db.rows('SELECT * FROM tasks')
    eng.state.assert_not_called();eng.catalog.db is db

@pytest.mark.parametrize('kind,cat',[('movie','3'),('ova','2'),('tv','1'),('web','5')])
def test_browse_source_category_and_actual_pagination(setup,kind,cat):
    from urllib.parse import urlparse,parse_qs
    db,web,_,client=setup
    web.fetch.return_value=Mock(json=lambda:{'total':9,'offset':2,'data':[subject(10,'Movie')]})
    r=client.get(f'/api/catalog/discover?kind={kind}&year=2025&offset=2')
    assert r.status_code==200 and r.json()['next_offset']==3
    args=parse_qs(urlparse(web.fetch.call_args.args[0]).query)
    assert args['cat']==[cat] and args['year']==['2025'] and args['offset']==['2']
    assert db.rows('SELECT * FROM shows')==[]

def test_explicit_import_metadata_preserves_all_tracking(setup):
    db,web,eng,client=setup
    db.upsert_subject(subject(2));db.execute("UPDATE shows SET selected=1,state='paused',authorized=0,cleanup_hold=1,mapping='{}' WHERE id=2")
    before=db.show(2)
    web.fetch.return_value=Mock(json=lambda:subject(2,'OVA',name_cn='资料更新'))
    assert client.post('/api/catalog/import/2',json={}).status_code==403
    headers={'X-Anime-Token':client.get('/api/bootstrap').json()['token']}
    assert client.post('/api/catalog/import/2',json={},headers=headers).status_code==200
    after=db.show(2)
    assert after['title']=='资料更新'
    for key in ('state','selected','authorized','auto_download','cleanup_hold','mapping','watched'):assert after[key]==before[key]
    web.fetch.return_value=Mock(json=lambda:subject(9,'Movie','2024-01-01'))
    assert client.post('/api/catalog/import/9',json={},headers=headers).status_code==200
    s=db.show(9);assert s['selected']==s['authorized']==s['auto_download']==0 and s['quarter']==''
    assert not db.rows('SELECT * FROM sources') and not db.rows('SELECT * FROM tasks')
    eng.state.assert_not_called()

@pytest.mark.parametrize('item',[subject(9,type=1),subject(9,nsfw=True),subject(10)])
def test_invalid_import_does_not_write(setup,item):
    db,web,_,_=setup;web.fetch.return_value=Mock(json=lambda:item)
    with pytest.raises(ValueError):import_subject(Catalog(db,web),db,9)
    assert not db.rows('SELECT * FROM shows')

def test_invalid_discovery_does_not_call_network(setup):
    db,web,_,client=setup
    for query in ['offset=-1','offset=2001','year=2','kind=continuing','q='+'a'*101]:assert client.get('/api/catalog/discover?'+query).status_code==400
    web.fetch.assert_not_called()

def test_page_filter_unknown_and_error(setup):
    from anime.net import NetworkError
    db,web,_,_=setup;catalog=Catalog(db,web)
    web.fetch.return_value=Mock(json=lambda:{'total':1,'data':[subject(2,'OVA',name='English',tags=[])]})
    assert discover(catalog,db,kind='ova')['items']==[]
    assert len(discover(catalog,db,kind='ova',include_unknown=True)['items'])==1
    web.fetch.return_value=Mock(json=lambda:{'total':2,'offset':9,'data':[]})
    with pytest.raises(NetworkError):discover(catalog,db)
