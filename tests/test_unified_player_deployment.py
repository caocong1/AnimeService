"""The legacy deployment entry point must run only the bounded metadata checker."""
import json
from types import SimpleNamespace
import pytest
from scripts import deploy_unified_player as deployment


@pytest.mark.parametrize('exit_code',[0,7])
def test_metadata_wrapper_is_bounded_and_preserves_checker_failure(tmp_path,monkeypatch,exit_code):
    output=tmp_path/'receipt';calls=[]
    monkeypatch.setattr('sys.argv',['deploy_unified_player.py','--output',str(output)])
    monkeypatch.setenv('PLAYER_QA_BASE','http://untrusted.invalid')
    monkeypatch.setattr(deployment.shutil,'which',lambda name:'node-test' if name=='node' else None)
    def run(command,**kwargs):
        calls.append(command)
        assert command==['node-test','scripts/verify_unified_player_live.cjs']
        assert kwargs['timeout']==60
        assert kwargs['env']['PLAYER_QA_BASE']=='http://127.0.0.1:4871'
        assert kwargs['env']['PLAYER_QA_OUT']==str(output.resolve())
        return SimpleNamespace(returncode=exit_code)
    monkeypatch.setattr(deployment.subprocess,'run',run)
    assert deployment.main()==exit_code
    assert len(calls)==1
    proof=json.loads((output/'deployment.json').read_text())
    assert proof['state']==('metadata_verified' if exit_code==0 else 'metadata_check_failed')
    assert proof['metadata_exit_code']==exit_code
    assert proof['restart_attempted'] is False
    assert proof['browser_started'] is False


def test_missing_checker_runtime_fails_without_deployment(tmp_path,monkeypatch):
    monkeypatch.setattr('sys.argv',['deploy_unified_player.py','--output',str(tmp_path)])
    monkeypatch.setattr(deployment.shutil,'which',lambda name:None)
    def forbidden(*args,**kwargs):raise AssertionError('Must not run a fallback command')
    monkeypatch.setattr(deployment.subprocess,'run',forbidden)
    with pytest.raises(RuntimeError,match='Node.js is required'):deployment.main()
    assert not (tmp_path/'deployment.json').exists()
