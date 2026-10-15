import json
from unittest.mock import Mock
import pytest
from test_reliability import setup
from anime.source_recovery import select_verified_release
from anime.releases import Rejected


@pytest.mark.parametrize('title,filename,season',[
    ('[Group] Example / 战车椅子-TANK CHAIR- - 01 [CHS]','TANK CHAIR - 01.mkv',1),
    ('[Group] Example / Alternate Name Season 2 - 01 [CHS]','Alternate Name S02 - 01.mkv',2),
    ('[Group] Example / Alternate Name S01E01 [CHS]','Alternate Name S01E01.mkv',1),
])
def test_declared_alias_formats_retain_actual_file_season_and_episode_validation(setup,title,filename,season):
    db,q,e,c=setup;m=db.mapping(42);m.update(mikan_id='123',season=season)
    db.execute('UPDATE shows SET mapping=?',(json.dumps(m),))
    db.execute("INSERT INTO sources(id,show_id,kind,url) VALUES(1,42,'mikan','https://mikanani.me/RSS/Bangumi?bangumiId=123')")
    e.catalog.mikan_mapping=Mock(return_value={'verified':True})
    candidate=c(title);files=[{'index':0,'name':filename,'size':100}]
    assert select_verified_release(e,candidate,files,[1],[1])[1]==[0]
    with pytest.raises(ValueError):select_verified_release(e,candidate,[{**files[0],'name':'Unrelated S01E01.mkv'}],[1],[1])


@pytest.mark.parametrize('block',['','identity','wrong_season','unselected','other_show','missing_episode'])
def test_declared_alias_recovery_keeps_identity_season_scope_and_episode_gates(setup,block):
    db,q,e,c=setup;m=db.mapping(42);m['mikan_id']='123';db.execute('UPDATE shows SET mapping=?',(json.dumps(m),))
    db.execute("INSERT INTO sources(id,show_id,kind,url) VALUES(1,42,'mikan','https://mikanani.me/RSS/Bangumi?bangumiId=123')")
    candidate=c('[Group] Example / Alternate Name - 01 [1080p][CHS]')
    files=[{'index':0,'name':'Alternate Name S01E01.mkv','size':100}]
    e.catalog.mikan_mapping=Mock(return_value={'verified':True})
    if block=='identity':e.catalog.mikan_mapping.side_effect=ValueError('wrong subject')
    if block=='wrong_season':files[0]['name']='Alternate Name S09E01.mkv'
    if block=='unselected':db.execute('UPDATE shows SET selected=0')
    if block=='other_show':files[0]['name']='Unrelated S01E01.mkv'
    if block=='missing_episode':files[0]['name']='Alternate Name S01E02.mkv'
    if block:
        with pytest.raises(ValueError):select_verified_release(e,candidate,files,[1],[1])
        assert db.mapping(42)==m
    else:
        result,ids=select_verified_release(e,candidate,files,[1],[1])
        assert ids==[0] and result[1]==files[0]
        assert db.mapping(42)['aliases']==['Example','Alternate Name']
        assert {k:v for k,v in db.mapping(42).items() if k!='aliases'}=={k:v for k,v in m.items() if k!='aliases'}


@pytest.mark.parametrize('block',['','wrong_subject','stale','mixed'])
def test_part_numbering_uses_independent_local_episode_anchor(setup,block):
    import time
    from anime.source_recovery import verified_part_numbering
    from anime.releases import validate_title
    db,q,e,c=setup;db.execute("UPDATE shows SET original='千歳くんはラムネ瓶のなか 第2クール' WHERE id=42")
    mapping={**db.mapping(42),'end':13,'mikan_id':'4105'}
    rows=[{'subject_id':42,'ep':1,'sort':14},{'subject_id':42,'ep':2,'sort':15}]
    if block=='wrong_subject':rows[0]['subject_id']=99
    if block=='mixed':rows[1]['sort']=16
    db.set('bangumi_episodes_42',{'time':time.time()-(22000 if block=='stale' else 0),'items':rows})
    result=verified_part_numbering(db,42,mapping)
    if block:assert result==mapping
    else:
        assert result['airing_offset']==13 and result['source_seasons']==[1,2]
        assert validate_title('Example 第二季 - 14 [CHS]',result,{},False)==[1]
        for bad in ['Example 第九季 - 14 [CHS]','Example - 13.5 [CHS]']:
            with pytest.raises(Rejected):validate_title(bad,result,{},False)


@pytest.mark.parametrize('block',['','changed_file','wrong_source','same_subject','unproved'])
def test_preparation_inventory_only_excludes_unchanged_audited_other_subject(setup,block,tmp_path):
    from anime.source_recovery import preparation_inventory
    db,q,e,c=setup
    path=tmp_path/'Example - 11.mp4';path.write_bytes(b'old')
    item={'path':str(path),'size':3};e.inventory_matches=Mock(return_value=[item])
    proof={**item,'mtime_ns':path.stat().st_mtime_ns,'other_subject_id':41,
           'all_pieces_match':True,'source_identity_verified':True}
    audit={'show_id':42,'mikan_id':'123','files':[proof]}
    if block=='changed_file':path.write_bytes(b'changed')
    if block=='wrong_source':audit['mikan_id']='999'
    if block=='same_subject':proof['other_subject_id']=42
    if block=='unproved':proof['all_pieces_match']=False
    db.set('inventory_other_subject_42',audit)
    assert preparation_inventory(e,42,{'mikan_id':'123'})==([item] if block else [])
