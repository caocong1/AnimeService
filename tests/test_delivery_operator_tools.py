"""Operator exits for the delivery queue: a lease that never dispatched can be handed back; verified absence is recorded with its evidence."""
import importlib.util,json,sys,os,base64
from pathlib import Path
import pytest
from test_subtitle_delivery import delivery,begin

spec=importlib.util.spec_from_file_location('subtitle_delivery_queue',Path(__file__).resolve().parents[1]/'scripts/subtitle_delivery_queue.py')
queue_cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue_cli)


def events(db,identity,kind):
    return [json.loads(r['payload']) for r in db.rows('SELECT payload FROM subtitle_delivery_events WHERE job_id=? AND kind=? ORDER BY id',(identity,kind))]


def test_undispatched_lease_can_be_released_but_a_started_dispatch_cannot(delivery):
    db,scheduler,ready,now=delivery
    identity=scheduler.episode_id(42,1);row=scheduler.get(identity)
    lease=scheduler.reserve(identity,'owner-a',row['revision'])
    with pytest.raises(ValueError):scheduler.release(identity,'owner-b',lease['revision'])
    with pytest.raises(ValueError):scheduler.release(identity,'owner-a',lease['revision']-1)
    released=scheduler.release(identity,'owner-a',lease['revision'],lease['lease_token'],reason='test')
    assert released['state']=='ready' and released['lease_owner']=='' and released['lease_expires']==0
    assert events(db,identity,'lease-released')==[{'owner':'owner-a','reason':'test'}]
    other=scheduler.reserve(scheduler.episode_id(42,2),'owner-c',scheduler.get(scheduler.episode_id(42,2))['revision'])  # the single slot is free again
    assert other['lease_owner']=='owner-c'
    scheduler.release(scheduler.episode_id(42,2),'owner-c',other['revision'])
    identity2,intent=begin(scheduler,42,1,owner='owner-d')
    with pytest.raises(ValueError,match='reconcile'):scheduler.release(identity2,'owner-d',intent['revision'])


def test_begin_cli_hands_the_lease_back_when_dispatch_cannot_start(delivery,monkeypatch,capsys):
    db,scheduler,ready,now=delivery
    identity=scheduler.episode_id(42,1);row=scheduler.get(identity)
    monkeypatch.setattr(queue_cli,'ControlStore',lambda path:db)
    monkeypatch.setattr(queue_cli,'SubtitleDelivery',lambda store,initialize=True:scheduler)
    monkeypatch.setattr(scheduler,'begin_dispatch',lambda *a,**k:(_ for _ in ()).throw(ValueError('Exact ready inputs changed')))
    monkeypatch.setattr(sys,'argv',['queue','--db',str(db.path),'begin',identity,'--owner','cli-owner','--revision',str(row['revision'])])
    with pytest.raises(SystemExit) as stop:queue_cli.main()
    payload=json.loads(str(stop.value))
    assert payload['lease_released'] is True and payload['state']=='ready' and 'Exact ready inputs changed' in payload['error']
    assert scheduler.get(identity)['lease_owner']=='' and events(db,identity,'lease-released')[0]['reason'].startswith('begin failed')


def test_begin_acl_failure_does_not_claim_or_commit(delivery,monkeypatch):
    db,scheduler,ready,now=delivery
    identity=scheduler.episode_id(42,1);before=scheduler.get(identity)
    monkeypatch.setattr(queue_cli,'ControlStore',lambda path:db)
    monkeypatch.setattr(queue_cli,'SubtitleDelivery',lambda store,initialize=True:scheduler)
    def unavailable(path):
        raise ValueError('cannot secure the ticket directory')
    monkeypatch.setattr(queue_cli,'protect_ticket_directory',unavailable)
    monkeypatch.setattr(sys,'argv',['queue','--db',str(db.path),'begin',identity,'--owner','cli-owner','--revision',str(before['revision'])])
    with pytest.raises(ValueError,match='cannot secure'):
        queue_cli.main()
    assert scheduler.get(identity)==before
    assert events(db,identity,'reserved')==[]
    assert events(db,identity,'dispatch-started')==[]


@pytest.mark.skipif(os.name!='nt',reason='Windows ticket ACL integration')
def test_windows_ticket_acl_ignores_parent_powershell_modules(tmp_path,monkeypatch):
    # Windows PowerShell 5 can inherit PowerShell 7's incompatible module path.
    # Ticket protection/readback must work without loading Security/Utility.
    monkeypatch.setenv('PSModulePath',str(tmp_path/'no-modules'))
    ticket_dir=tmp_path/'tickets';ticket_dir.mkdir()
    queue_cli.protect_ticket_directory(ticket_dir)
    encoded_path=base64.b64encode(str(ticket_dir).encode()).decode()
    script=(
        "$ErrorActionPreference='Stop'; "
        f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}')); "
        "$a=[IO.Directory]::GetAccessControl($p); if(-not $a.AreAccessRulesProtected) {exit 1}; "
        "$r=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); "
        "$u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "if($r.Count -lt 1) {exit 3}; "
        "foreach($x in $r) {if($x.IdentityReference.Value -notin @($u,'S-1-5-18') -or "
        "$x.AccessControlType -ne 'Allow' -or $x.FileSystemRights -ne 'FullControl') {exit 2}}"
    )
    result=queue_cli.subprocess.run([
        str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),
        '-NoProfile','-NonInteractive','-EncodedCommand',
        base64.b64encode(script.encode('utf-16le')).decode()],capture_output=True)
    assert result.returncode==0,result.stderr


def test_not_created_requires_evidence_and_keeps_it_with_the_receipt(delivery,tmp_path,monkeypatch,capsys):
    db,scheduler,ready,now=delivery
    identity,intent=begin(scheduler,42,1)
    scheduler.reconcile_dispatch(identity,intent['dispatch_token'])  # response lost: unknown
    assert scheduler.get(identity)['dispatch_state']=='unknown'
    monkeypatch.setattr(queue_cli,'ControlStore',lambda path:db)
    monkeypatch.setattr(queue_cli,'SubtitleDelivery',lambda store,initialize=True:scheduler)
    bad=tmp_path/'bad.json';bad.write_text(json.dumps({'checked_at':'2026-10-18'}),encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['queue','--db',str(db.path),'not-created',identity,'--evidence',str(bad)])
    with pytest.raises(ValueError,match='checked_at, method and checked_by'):queue_cli.main()
    assert scheduler.get(identity)['dispatch_state']=='unknown'
    good=tmp_path/'absence.json';good.write_text(json.dumps({'checked_at':'2026-10-18T02:00:00+08:00','method':'listed all chats of the dispatcher, none for this job','checked_by':'operator','job_id':identity,'dispatch_token':intent['dispatch_token']}),encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['queue','--db',str(db.path),'not-created',identity,'--evidence',str(good)])
    queue_cli.main();out=json.loads(capsys.readouterr().out)
    assert out['dispatch_state']=='not-created' and out['state']=='ready' and 'dispatch_token' not in out
    receipt=events(db,identity,'dispatch-reconciled')[-1]
    assert receipt['result']=='not-created' and receipt['absence_evidence']['sha256'] and receipt['absence_evidence']['method'].startswith('listed')
    assert scheduler.reserve(identity,'next-owner',scheduler.get(identity)['revision'])['lease_owner']=='next-owner'
