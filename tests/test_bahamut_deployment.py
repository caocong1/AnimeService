"""Capability absence is reported; this helper must never reload a service."""
import json
from types import SimpleNamespace
import pytest
from scripts import deploy_bahamut_alignment as checker


@pytest.mark.parametrize('status,capability,expected',[(200,'bounded-hls-v1',0),(200,'older',2),(404,None,2)])
def test_alignment_helper_only_reads_capability(tmp_path,monkeypatch,status,capability,expected):
    calls=[]
    class Session:
        trust_env=True
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            assert self.trust_env is False
            assert url=='http://127.0.0.1:4871/api/web/alignment-capabilities'
            assert kwargs=={'timeout':5,'allow_redirects':False}
            calls.append(url)
            return SimpleNamespace(status_code=status,raise_for_status=lambda:None,json=lambda:{'bahamut_reference':capability})
    monkeypatch.setattr('sys.argv',['deploy_bahamut_alignment.py','--output',str(tmp_path)])
    monkeypatch.setattr(checker.requests,'Session',Session)
    assert checker.main()==expected
    assert len(calls)==1
    proof=json.loads((tmp_path/'deployment.json').read_text())
    assert proof['restart_attempted'] is False and proof['browser_started'] is False
    assert proof['state']==('capability_verified' if expected==0 else 'owner_reload_required')
