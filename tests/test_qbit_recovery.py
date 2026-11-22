import json
import os
from pathlib import Path
from unittest.mock import Mock
import concurrent.futures

import psutil
import pytest

from anime import qbit_recovery as recovery
from test_reliability import setup


@pytest.fixture
def original(tmp_path,monkeypatch):
    exe=tmp_path/'qbittorrent.exe';exe.write_bytes(b'existing')
    profile=tmp_path/'profile';config=profile/'qBittorrent/config/qBittorrent.ini'
    config.parent.mkdir(parents=True);config.write_bytes(b'original configuration')
    monkeypatch.setattr(recovery,'EXE',exe);monkeypatch.setattr(recovery,'PROFILE',profile)
    monkeypatch.setattr(recovery,'absence',Mock(return_value='absent'))
    monkeypatch.setattr(recovery,'launch',Mock(return_value=Mock(pid=123)))
    return tmp_path/'state',config


def test_restore_persists_intent_before_launch_and_cooldown_survives_new_call(original):
    directory,config=original
    def launch():
        assert json.loads((directory/'qbit-restore.json').read_text())[-1]['state']=='launching'
        return Mock(pid=123)
    recovery.launch.side_effect=launch
    assert recovery.restore_original(directory)['state']=='launched'
    assert recovery.restore_original(directory)['state']=='cooldown'
    recovery.launch.assert_called_once()
    assert config.read_bytes()==b'original configuration'


@pytest.mark.parametrize('state',['process_present','process_unknown','port_unknown','port_occupied'])
def test_existing_foreign_or_unconfirmed_process_and_port_never_launch(original,state):
    recovery.absence.return_value=state
    assert recovery.restore_original(original[0])['state']==state
    recovery.launch.assert_not_called()


def test_process_appearing_between_checks_prevents_launch(original):
    recovery.absence.side_effect=['absent','process_present']
    assert recovery.restore_original(original[0])['state']=='process_present'
    recovery.launch.assert_not_called()


@pytest.mark.parametrize('prior',['launching','unknown'])
def test_unknown_launch_is_never_replayed_after_restart_or_cooldown(original,prior):
    directory,_=original;directory.mkdir()
    (directory/'qbit-restore.json').write_text(json.dumps([{'state':prior,'next_attempt':0}]))
    assert recovery.restore_original(directory)['state']=='launch_unknown'
    recovery.launch.assert_not_called()


def test_parallel_calls_launch_only_once(original):
    directory,_=original
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda _:recovery.restore_original(directory),range(4)))
    assert sum(row['state']=='launched' for row in rows)==1
    recovery.launch.assert_called_once()


def test_known_exit_after_cooldown_can_restore_again(original,monkeypatch):
    directory,_=original
    monkeypatch.setattr(recovery.time,'time',lambda:1000)
    recovery.restore_original(directory)
    monkeypatch.setattr(recovery.time,'time',lambda:1061)
    assert recovery.restore_original(directory)['state']=='launched'
    assert recovery.launch.call_count==2


def test_unknown_launch_exception_preserves_receipt(original):
    recovery.launch.side_effect=RuntimeError('ambiguous')
    assert recovery.restore_original(original[0])['state']=='unknown'
    assert recovery.restore_original(original[0])['state']=='launch_unknown'
    recovery.launch.assert_called_once()


def test_missing_original_config_does_not_create_new_profile(original):
    original[1].unlink()
    assert recovery.restore_original(original[0])['state']=='config_missing'
    recovery.launch.assert_not_called()


@pytest.mark.parametrize('case',['disabled','thunder','stop','stop_file'])
def test_maintenance_respects_opt_in_preference_and_service_stop(setup,monkeypatch,case):
    db,q,engine,c=setup
    db.set('qbit_auto_restore_enabled',case!='disabled')
    if case=='thunder':db.set('downloader_preference','thunder_first')
    if case=='stop':engine.stop_event.set()
    if case=='stop_file':(db.path.parent/'stop.request').write_text('stopping')
    restore=Mock();monkeypatch.setattr(recovery,'restore_original',restore)
    recovery.maintain_original(engine)
    restore.assert_not_called()


def test_maintenance_restores_process_only_without_posting_downloads(setup,monkeypatch):
    db,q,engine,c=setup;db.set('qbit_auto_restore_enabled',True)
    restore=Mock(return_value={'state':'launched','pid':123})
    monkeypatch.setattr(recovery,'restore_original',restore)
    recovery.maintain_original(engine)
    assert db.get('qbit_restore_status')['state']=='launched'
    assert not q.adds and not q.starts and not db.rows('SELECT * FROM tasks')


@pytest.mark.parametrize('error',[psutil.AccessDenied(123),OSError('uncertain')])
def test_process_enumeration_failure_is_not_absence(monkeypatch,error):
    monkeypatch.setattr(recovery.psutil,'process_iter',Mock(side_effect=error))
    assert recovery.absence()=='process_unknown'


def test_occupied_loopback_and_socket_timeout_are_not_absence(monkeypatch):
    monkeypatch.setattr(recovery.psutil,'process_iter',lambda:iter(()))
    monkeypatch.setattr(recovery.socket,'create_connection',Mock(side_effect=TimeoutError()))
    assert recovery.absence()=='port_unknown'


def test_windows_delayed_connection_refusal_is_distinct_from_timeout(monkeypatch):
    monkeypatch.setattr(recovery.psutil,'process_iter',lambda:iter(()))
    def delayed_refusal(address,timeout):
        assert address==('127.0.0.1',4870)
        if timeout<=2.1:raise TimeoutError()
        raise ConnectionRefusedError()
    monkeypatch.setattr(recovery.socket,'create_connection',delayed_refusal)
    assert recovery.absence()=='absent'


@pytest.mark.skipif(os.name != 'nt', reason='Windows process creation flags')
def test_hidden_launch_uses_only_original_program_profile_and_port(monkeypatch):
    popen=Mock(return_value=Mock(pid=123));startup=Mock()
    monkeypatch.setattr(recovery.subprocess,'Popen',popen)
    monkeypatch.setattr(recovery.subprocess,'STARTUPINFO',lambda:startup)
    startup.dwFlags=0
    recovery.launch()
    assert popen.call_args.args[0]==[str(recovery.EXE),'--profile='+str(recovery.PROFILE),'--webui-port=4870']
    assert popen.call_args.kwargs['close_fds'] is True and startup.wShowWindow==0


def test_settings_enable_does_not_reset_rejected_candidates(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db,q,engine,c=setup;candidate=c();db.execute("UPDATE candidates SET status='rejected'")
    with TestClient(create_app(db,engine,False),base_url='http://127.0.0.1:4871') as client:
        token=client.get('/api/bootstrap').json()['token']
        response=client.post('/api/settings',json={'qbit_auto_restore_enabled':True},headers={'X-Anime-Token':token})
        assert response.status_code==200 and db.get('qbit_auto_restore_enabled') is True
        assert db.one('SELECT status FROM candidates')['status']=='rejected'
        assert not q.adds


def test_original_loop_checks_recovery_even_when_maintenance_is_busy(setup,monkeypatch):
    db,q,engine,c=setup;db.set('qbit_auto_restore_enabled',True)
    restore=Mock(return_value={'state':'process_present'})
    monkeypatch.setattr(recovery,'restore_original',restore)
    monkeypatch.setattr(engine.stop_event,'wait',Mock(side_effect=[False,True]))
    engine.maintenance_lock.acquire()
    try:engine.maintain_loop()
    finally:engine.maintenance_lock.release()
    restore.assert_called_once()
    assert db.get('qbit_restore_status')['state']=='process_present' and q.adds==0
