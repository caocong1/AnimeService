"""Synthetic intake, keys and external CAS only; never touches production media."""
import copy
import hashlib
import json
import os
import base64
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from anime.ordinary_attestation import Keyring,TimedKey,SourceAttestor,TimedOrdinaryIssuer,create_private_directory,local_plain
from anime.ordinary_anchor import AnchoredLedger
from anime.ordinary_issuance import canonical,digest

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows share/ACL contract')


@pytest.fixture
def env(tmp_path):
    now=[2000]
    ring=Keyring([TimedKey('s1','source',1000,3000,b's'*32),TimedKey('s2','source',1500,5000,b't'*32),
                  TimedKey('p1','publication',1000,3000,b'p'*32),TimedKey('p2','publication',1500,5000,b'q'*32)])
    source=tmp_path/'synthetic.bin';source.write_bytes(b'A'*8192)
    row={'intake_id':'1'*32,'approved':True,'mode':'owner-reviewed-import/v1','authorized':True,'path':str(source),
         'id':digest(str(source).lower().encode())[:32],'size':8192,'association_digest':'a'*64,'input_signature':'b'*64}
    root=create_private_directory(tmp_path/'private')
    issuer=TimedOrdinaryIssuer(root,ring,'p1',lambda key:copy.deepcopy(row),wall_clock=lambda:now[0],reserve_bytes=0)
    attest=SourceAttestor(ring,'s1',lambda intake:copy.deepcopy(row),wall_clock=lambda:now[0],lifetime=60)
    return {'now':now,'ring':ring,'row':row,'source':source,'root':root,'issuer':issuer,'attest':attest}


def test_independent_intake_hash_verifies_and_publishes_without_db(env):
    evidence=env['attest'].attest('1'*32)
    assert evidence['payload']['sha256']==hashlib.sha256(b'A'*8192).hexdigest()
    assert env['issuer'].source(evidence)['input_signature']=='b'*64
    with env['issuer'].publish(evidence) as lease:assert lease.read()==b'A'*8192
    assert not list(env['root'].rglob('*.db'))


@pytest.mark.parametrize('change',[{'approved':False},{'mode':'current-path-hash'},{'authorized':False},{'intake_id':'2'*32}])
def test_intake_is_not_a_path_or_download_state_authorization(env,change):
    env['row'].update(change)
    with pytest.raises(ValueError):env['attest'].attest('1'*32)


@pytest.mark.parametrize('field',['mac','key_id','input_signature'])
def test_forged_attestation_is_rejected(env,field):
    evidence=env['attest'].attest('1'*32)
    if field=='input_signature':evidence['payload'][field]='c'*64
    else:evidence[field]='0'*64
    with pytest.raises(ValueError):env['issuer'].source(evidence)


def test_rotation_keeps_old_signatures_only_inside_key_interval(env):
    evidence=env['attest'].attest('1'*32)
    old=env['issuer'].seal({'schema':'test/v1','x':1})
    new=TimedOrdinaryIssuer(env['root'],env['ring'],'p2',lambda key:env['row'],wall_clock=lambda:env['now'][0])
    assert new.unseal(old,'test/v1')['x']==1 and new.source(evidence)['size']==8192
    env['now'][0]=3000
    with pytest.raises(ValueError,match='interval'):new.unseal(old,'test/v1')
    # Historical source verification still cannot revive an expired verification key.
    with pytest.raises(ValueError,match='interval'):new.source(evidence,at=2000)
    assert new.seal({'schema':'test/v1'})['key_id']=='p2'


def test_key_purpose_window_and_duplicate_material_are_rejected(env):
    ring=env['ring']
    for args in [('s1','publication',2000),('s1','source',999),('s1','source',3000),('missing','source',2000)]:
        with pytest.raises(ValueError):ring.key(*args)
    with pytest.raises(ValueError):ring.key('s1','source',2000,2000,3001)
    with pytest.raises(ValueError):Keyring([TimedKey('x','source',1,4,b'x'*32),TimedKey('y','publication',1,4,b'x'*32)])
    assert 'ssss' not in repr(next(iter(ring._keys.values())))


def test_private_key_file_is_exclusive_roundtrippable_and_no_secret_output(env,capsys):
    path=env['root']/'keys.json';env['ring'].save_new(path)
    ring=Keyring.read_private(path);assert ring.key('s1','source',2000)==b's'*32
    with pytest.raises(FileExistsError):env['ring'].save_new(path)
    assert capsys.readouterr().out==''


def test_new_key_generation_and_existing_directory_are_explicit(tmp_path):
    first=TimedKey.generate('source',1,10);second=TimedKey.generate('publication',1,10)
    assert first.key_id!=second.key_id and first.secret!=second.secret and len(first.secret)==32
    root=create_private_directory(tmp_path/'new')
    with pytest.raises(FileExistsError):create_private_directory(root)


def test_remote_path_is_rejected_before_any_filesystem_lookup(monkeypatch):
    from anime import ordinary_attestation as module
    monkeypatch.setattr(module,'plain',lambda _:pytest.fail('filesystem lookup before drive check'))
    with pytest.raises(ValueError,match='fixed drive'):local_plain(r'\\untrusted.invalid\share\media.bin')


def test_key_file_with_broad_acl_is_refused(env):
    path=env['root']/'keys.json';env['ring'].save_new(path)
    script=r'''$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PATH'))
$a=[Security.AccessControl.FileSecurity]::new();$a.SetAccessRuleProtection($true,$false)
$s=[Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($s,'FullControl','Allow'))
[IO.File]::SetAccessControl($p,$a)
'''.replace('PATH',base64.b64encode(str(path).encode()).decode())
    shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    r=subprocess.run([str(shell),'-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode()],capture_output=True,timeout=10)
    assert r.returncode==0
    with pytest.raises(ValueError,match='ACL'):Keyring.read_private(path)


def test_attestation_time_and_byte_budgets_fail_without_evidence(env):
    a=SourceAttestor(env['ring'],'s1',lambda _:env['row'],max_bytes=1,wall_clock=lambda:2000)
    with pytest.raises(ValueError,match='byte budget'):a.attest('1'*32)
    clock=[0]
    def tick():clock[0]+=1;return clock[0]
    a=SourceAttestor(env['ring'],'s1',lambda _:env['row'],time_limit=.5,clock=tick,wall_clock=lambda:2000,lifetime=60)
    with pytest.raises(ValueError,match='time budget'):a.attest('1'*32)


def test_concurrency_is_fail_fast_and_slot_is_released(env):
    entered=Event();release=Event()
    def approve(_):
        entered.set();assert release.wait(5);return env['row']
    a=SourceAttestor(env['ring'],'s1',approve,wall_clock=lambda:2000,lifetime=60)
    with ThreadPoolExecutor(max_workers=1) as pool:
        f=pool.submit(a.attest,'1'*32);assert entered.wait(3)
        with pytest.raises(ValueError,match='concurrency'):a.attest('1'*32)
        release.set();assert f.result()['payload']['size']==8192
    assert a.attest('1'*32)['payload']['size']==8192


def test_changed_approval_and_existing_writer_refuse_evidence(env):
    n=[0]
    def approve(_):
        n[0]+=1;r=copy.deepcopy(env['row'])
        if n[0]>1:r['input_signature']='f'*64
        return r
    a=SourceAttestor(env['ring'],'s1',approve,wall_clock=lambda:2000,lifetime=60)
    with pytest.raises(ValueError,match='moved'):a.attest('1'*32)
    with env['source'].open('r+b'):
        with pytest.raises(ValueError,match='sharing conflict'):env['attest'].attest('1'*32)


class Anchor:
    # Synthetic trusted service, deliberately NOT a local file persistence provider.
    identity='a'*32
    def __init__(self):self.value=(0,'0'*64);self.fail=None
    def read(self):return self.value
    def compare_exchange(self,expected,wanted):
        if self.fail=='before':self.fail=None;raise RuntimeError('synthetic crash')
        if self.value!=expected:return False
        self.value=wanted
        if self.fail=='after':self.fail=None;raise RuntimeError('synthetic lost reply')
        return True


@pytest.fixture
def ledger(env,tmp_path):
    root=create_private_directory(tmp_path/'ledger');anchor=Anchor()
    return AnchoredLedger(root,env['issuer'],anchor),anchor


@pytest.mark.parametrize('window',['before','after'])
def test_anchor_crash_before_or_after_cas_retries_idempotently(ledger,window):
    log,anchor=ledger;anchor.fail=window
    with pytest.raises(RuntimeError):log.bind('1'*32,'b'*64)
    head=log.bind('1'*32,'b'*64)
    assert head[0]==1 and log.snapshot()[1]=={'1'*32:'b'*64}


@pytest.mark.parametrize('change',['forged','replay','downgrade','missing'])
def test_old_manifest_or_chain_cannot_replace_current_anchor(ledger,change):
    log,anchor=ledger;log.bind('1'*32,'b'*64);first=(log.root/'1.json').read_bytes();log.bind('2'*32,'c'*64)
    second=log.root/'2.json'
    if change=='missing':second.unlink()
    elif change=='replay':second.write_bytes(first)
    else:
        record=json.loads(second.read_text());record['body']['schema']='ordinary-ledger/v0' if change=='downgrade' else 'ordinary-ledger/v1';record['body']['manifest_sha256']='d'*64
        second.write_bytes(canonical(record))
    with pytest.raises((ValueError,FileNotFoundError)):log.snapshot()


def test_alias_rebind_and_missing_anchor_fail_closed(ledger):
    log,anchor=ledger;log.bind('1'*32,'b'*64)
    with pytest.raises(ValueError,match='rebind'):log.bind('1'*32,'c'*64)
    anchor.value=None
    with pytest.raises(ValueError,match='anchor'):log.snapshot()


def test_different_pending_fact_never_overwrites_an_interrupted_commit(ledger):
    log,anchor=ledger;anchor.fail='before'
    with pytest.raises(RuntimeError):log.bind('1'*32,'b'*64)
    before=(log.root/'1.json').read_bytes()
    with pytest.raises(ValueError,match='pending'):log.bind('2'*32,'c'*64)
    assert (log.root/'1.json').read_bytes()==before and anchor.value[0]==0
    assert log.bind('1'*32,'b'*64)[0]==1


def test_ledger_budget_and_rotated_publication_verifier(ledger,env):
    log,anchor=ledger;log.bind('1'*32,'b'*64)
    rotated=TimedOrdinaryIssuer(env['root'],env['ring'],'p2',lambda _:env['row'],wall_clock=lambda:env['now'][0])
    other=AnchoredLedger(log.root,rotated,anchor)
    assert other.snapshot()[1]=={'1'*32:'b'*64}
    tick=[0]
    def clock():tick[0]+=1;return tick[0]
    short=AnchoredLedger(log.root,rotated,anchor,time_limit=.1,clock=clock)
    with pytest.raises(ValueError,match='time budget'):short.snapshot()


def test_concurrent_same_alias_never_rebinds(ledger):
    log,anchor=ledger;entered=Event();release=Event();original=anchor.compare_exchange
    def cas(*args):entered.set();assert release.wait(5);return original(*args)
    anchor.compare_exchange=cas
    with ThreadPoolExecutor(max_workers=1) as pool:
        f=pool.submit(log.bind,'1'*32,'b'*64);assert entered.wait(3)
        with pytest.raises(ValueError,match='sharing conflict'):log.bind('1'*32,'c'*64)
        release.set();assert f.result()[0]==1
    assert log.bind('1'*32,'b'*64)[0]==1
