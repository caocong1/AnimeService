"""Only a credential problem asks for a new Bangumi login; an unset airing offset stays unset; numbering can be diffed read-only."""
import importlib.util,json,time,threading
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from test_bangumi_sync import setup,FakeBangumi,MemoryVault
from anime.app import create_app
from anime.bangumi_sync import BangumiSync
from anime.db import Store

spec=importlib.util.spec_from_file_location('airing_numbering_diff',Path(__file__).resolve().parents[1]/'scripts/airing_numbering_diff.py')
diff_tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(diff_tool)


def test_transient_failure_backs_off_without_demanding_a_new_login(setup):
    db,remote,sync=setup
    sync.client_factory=Mock(side_effect=RuntimeError('database is locked'))
    sync.tick();health=db.get('bangumi_sync_health')
    assert health['ok'] is False and health['auth_required'] is False and health['failures']==1
    assert 55<=health['next_attempt']-time.time()<=61 and 'locked' not in health['error'] and health['error'].startswith('RuntimeError：')
    db.set('bangumi_sync_health',{**health,'next_attempt':0});sync.tick()
    assert db.get('bangumi_sync_health')['failures']==2 and 115<=db.get('bangumi_sync_health')['next_attempt']-time.time()<=121
    assert db.rows("SELECT * FROM bangumi_sync_queue")  # the queue is kept
    sync.client_factory=lambda token:remote;db.set('bangumi_sync_health',{'ok':False,'failures':2,'next_attempt':0})
    sync.tick();assert db.get('bangumi_sync_health')['ok'] is True


def test_missing_or_mismatched_credentials_still_require_a_new_login(setup):
    db,remote,sync=setup
    sync.vault.value={'token':'t','id':99,'username':'other'}
    sync.tick();health=db.get('bangumi_sync_health')
    assert health['auth_required'] is True and '不匹配' in health['error']
    db.set('bangumi_sync_health',{});sync.vault.read=Mock(side_effect=ValueError('Bangumi 凭据读取失败，请在本机重新连接账号'))
    sync.tick();assert db.get('bangumi_sync_health')['auth_required'] is True


@pytest.mark.parametrize('health_write_fails',[False,True])
def test_sync_thread_survives_database_errors_before_tick_guard(setup,health_write_fails):
    db,remote,sync=setup
    stop=threading.Event()
    sync.options=Mock(side_effect=RuntimeError('private database detail'))
    original_tick=sync.tick
    def recover():
        sync.options=Mock(return_value={'enabled':False})
        original_tick()
        stop.set()
    sync.tick=Mock(side_effect=[original_tick,recover])
    # Mock side_effect functions must be called, not merely returned.
    actions=iter((original_tick,recover))
    sync.tick.side_effect=lambda:next(actions)()
    if health_write_fails:db.set=Mock(side_effect=RuntimeError('database unavailable'))
    sync.wake=Mock()
    sync.run(stop)
    assert sync.tick.call_count==2
    assert sync.wake.wait.call_args_list[0].args==(60,)
    if not health_write_fails:
        health=db.get('bangumi_sync_health')
        assert health['auth_required'] is False and 'private' not in health['error']


def test_mapping_save_keeps_an_unset_airing_offset_unset(tmp_path,monkeypatch):
    monkeypatch.setattr('anime.app.load_deployment',lambda:{'lan_network':'192.168.1.0/24'})
    db=Store(tmp_path/'data/test.db');db.upsert_subject({'id':42,'name':'Example','total_episodes':12})
    app=create_app(db,Mock(),start_worker=False);client=TestClient(app,base_url='http://localhost:4871')
    token=client.get('/api/bootstrap').json()['token'];headers={'X-Anime-Token':token,'Origin':'http://localhost:4871'}
    base={'aliases':['Example'],'season':1,'confirmed':True,'inventory_checked':True,'dmhy_keyword':'Example'}
    assert client.post('/api/shows/42/mapping',json=base,headers=headers).status_code==200
    assert 'airing_offset' not in db.mapping(42) and db.mapping(42)['offset']==0
    assert client.post('/api/shows/42/mapping',json={**base,'airing_offset':0},headers=headers).status_code==200
    assert db.mapping(42)['airing_offset']==0  # explicit zero is kept as the user's statement
    assert client.post('/api/shows/42/mapping',json={**base,'airing_offset':12},headers=headers).status_code==200
    assert client.post('/api/shows/42/mapping',json=base,headers=headers).status_code==200
    assert db.mapping(42)['airing_offset']==12  # a later save without the field keeps the saved value


def test_numbering_diff_reports_board_and_sync_disagreement_read_only(tmp_path,capsys):
    db=Store(tmp_path/'data/test.db')
    chapters=[{'id':101,'type':0,'sort':13,'ep':1,'subject_id':1},{'id':102,'type':0,'sort':14,'ep':2,'subject_id':1}]
    for sid,mapping in ((1,{'aliases':['A'],'airing_offset':0}),(2,{'aliases':['B']})):
        db.upsert_subject({'id':sid,'name':'X','total_episodes':2});db.execute('UPDATE shows SET selected=1,mapping=? WHERE id=?',(json.dumps(mapping),sid))
        for sort in (13,14):db.execute('INSERT INTO airings(show_id,episode,airdate) VALUES(?,?,?)',(sid,sort,'2026-01-01'))
        db.set('airing_sort_offset_'+str(sid),12);db.set('bangumi_episodes_'+str(sid),{'time':time.time(),'items':[{**c,'subject_id':sid} for c in chapters]})
    before=(tmp_path/'data/test.db').read_bytes()
    assert diff_tool.main([str(tmp_path/'data/test.db')])==0
    lines=[json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[-1]=={'shows':2,'differing':1}
    explicit_zero,absent=lines[0],lines[1]
    assert explicit_zero['airing_offset']==0 and explicit_zero['board']==[1,2] and explicit_zero['sync']==[13,14] and explicit_zero['differs']
    assert absent['airing_offset']=='absent' and absent['board']==[1,2] and absent['sync']==[1,2] and not absent['differs']
    assert (tmp_path/'data/test.db').read_bytes()==before
