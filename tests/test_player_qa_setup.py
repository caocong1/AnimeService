"""Unsafe QA setup fails before importing the application or opening a database."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fault',['root','nonce','worker','existing_db'])
def test_fixture_refuses_unsafe_setup_before_application_import(tmp_path,fault):
    root=tmp_path/('unsafe-root' if fault=='root' else 'anime-player-qa-synthetic')
    root.mkdir();token='synthetic-fixture-token-at-least-24'
    (root/'.fixture-token-sha256').write_text(hashlib.sha256(token.encode()).hexdigest())
    sentinel=tmp_path/'production-sentinel.db';sentinel.write_bytes(b'never open or change this synthetic sentinel')
    if fault=='existing_db':(root/'qa.db').write_bytes(b'existing protected database')
    env={**os.environ,'PLAYER_QA_ROOT':str(root),'PLAYER_QA_TOKEN':'wrong' if fault=='nonce' else token,
        'ANIMESERVICE_START_WORKER':'1' if fault=='worker' else '0','ANIMESERVICE_DB':str(sentinel)}
    code="""import json,runpy,sys
try:runpy.run_path('scripts/unified_player_fixture.py')
except RuntimeError:print(json.dumps({'refused':True,'app_imported':'anime.app' in sys.modules}))
else:raise AssertionError('Unsafe setup was accepted')
"""
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=10,check=True)
    assert json.loads(result.stdout)=={'refused':True,'app_imported':False}
    assert sentinel.read_bytes()==b'never open or change this synthetic sentinel'
    if fault=='existing_db':assert (root/'qa.db').read_bytes()==b'existing protected database'
    else:assert not (root/'qa.db').exists()
