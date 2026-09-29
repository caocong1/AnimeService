import json,time
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from anime.db import Store
from anime.app import create_app
from anime.releases import validate_title,Rejected

def test_compact_search_preserves_total_and_detail_fields(tmp_path):
    db=Store(tmp_path/'a.db')
    db.upsert_subject({'id':1,'name':'Example','total_episodes':24,'infobox':[{'key':'别名','value':[{'v':'Alias'}]}]},'2026-10')
    db.execute("UPDATE shows SET state='dropped',cleanup_hold=1 WHERE id=1")
    db.upsert_subject({'id':1,'name':'Example','eps':0},'2026-10')
    s=db.show(1)
    assert s['total']==24 and json.loads(s['metadata'])['infobox']
    assert s['state']=='dropped' and s['cleanup_hold']==1

def test_list_download_completion_order_not_metadata_or_poll_order(tmp_path):
    db=Store(tmp_path/'a.db')
    for sid,air in [(1,'2026-10-10'),(2,'2026-10-01'),(3,'2026-10-07'),(4,'')]:
        db.upsert_subject({'id':sid,'name':f'Example{sid}','date':air})
        db.execute('UPDATE shows SET selected=1 WHERE id=?',(sid,))
    for sid,completed in [(1,100),(3,200)]:
        db.execute("INSERT INTO episodes(show_id,episode,status,first_completed) VALUES(?,1,'complete',?)",(sid,completed))
    db.upsert_subject({'id':1,'name':'Fresh metadata'})
    client=TestClient(create_app(db,Mock(),False),base_url='http://127.0.0.1:4871')
    rows=client.get('/api/shows?scope=home').json()
    assert [s['id'] for s in rows]==[3,1,2,4]

def test_dmhy_fallback_keeps_local_episode_and_rejects_cumulative_number(tmp_path):
    db=Store(tmp_path/'a.db');db.upsert_subject({'id':1,'name':'Example 第三季','total_episodes':12})
    db.set('subject_details_1',time.time())
    eng=Mock();eng.catalog.mikan_matches.return_value=[];eng.settings.return_value={};eng.inventory_matches.return_value=[]
    titles=['Example 第三季 - 01 [1080p][CHS]','Example 第三季 - 25 [1080p][CHS]','Example 第二季 - 01 [1080p][CHS]']
    eng.sources.web.fetch.return_value.content=('<rss version="2.0"><channel>'+''.join(f'<item><title>{t}</title><link>https://share.dmhy.org/{i}</link></item>' for i,t in enumerate(titles))+'</channel></rss>').encode()
    client=TestClient(create_app(db,eng,False),base_url='http://127.0.0.1:4871')
    token=client.get('/api/bootstrap').json()['token']
    result=client.post('/api/shows/1/prepare',headers={'X-Anime-Token':token},json={})
    assert result.status_code==200
    r=result.json();assert r['preview']==[titles[0]] and not r['warning']
    assert r['excluded_numbering']==1 and r['mapping']['require_season']
    assert r['mapping']['dmhy_keyword']=='Example 第三季'
    assert not db.rows('SELECT * FROM tasks')

def test_search_sequel_without_season_is_rejected():
    with pytest.raises(Rejected):
        validate_title('Example - 01 [1080p][CHS]',{'aliases':['Example'],'season':3,'require_season':True},{})

def test_quarter_fetch_enriches_search_with_subject_details(tmp_path):
    from anime.sources import Catalog
    db=Store(tmp_path/'a.db');web=Mock()
    web.fetch.side_effect=[Mock(json=lambda:{'total':1,'data':[{'id':9,'name':'アニメ','platform':'TV','eps':0}]}),
                           Mock(json=lambda:{'id':9,'name':'アニメ','platform':'TV','total_episodes':13})]
    assert Catalog(db,web).sync_quarter(2026,10)==1
    assert db.show(9)['total']==13 and db.show(9)['quarter']=='2026-10'
