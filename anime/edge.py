"""Isolated HTTPS listener. Never reloads or edits the user's shared nginx."""
import json,os,subprocess,time,psutil
from pathlib import Path
from .db import ROOT
from .deployment import load as load_deployment

# The generic maintenance scripts print one fixed outcome token and nothing private; the
# listener records only a token whose exit status matches and never stores script output.
MAPPING={'mapping_ready':0,'mapping_conflict':3,'mapping_setup_failed':1}
RENEWAL={'ok':0,'dns_wait_failed':1,'certificate_request_failed':1,'certificate_request_timeout':1,'certificate_setup_failed':1}
MAPPING_TIMEOUT=90
RENEWAL_TIMEOUT=3600+600+60  # the renewal script bounds lego by propagation_timeout (at most 3600 s) plus 600 s
POWERSHELL=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe')

def outcome(args,timeout,known,flags):
    try:result=subprocess.run(args,capture_output=True,creationflags=flags,timeout=timeout)
    except subprocess.TimeoutExpired:return 'timeout'
    tokens=result.stdout.decode(errors='replace').split()
    token=tokens[-1] if tokens else ''
    return token if known.get(token)==result.returncode else 'unexpected'

def maintain_https(stop):
    certificate_path=load_deployment().get('certificate')
    if not certificate_path:return
    config=ROOT/'data/edge/nginx.conf';certificate=ROOT/certificate_path
    binary=ROOT/'data/tools/nginx.exe';private=ROOT/'data/https.json'
    if not config.exists() or not certificate.exists() or not binary.exists():return
    proc=None;last_renew=0;last_map=0;health={}
    args=[str(binary),'-p',str(config.parent)+'/', '-c','nginx.conf','-g','daemon off;']
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    def record(**fields):
        health.update(fields);(ROOT/'data/https-health.json').write_text(json.dumps(health))
    try:
        while not stop.is_set() and not (ROOT/'data/stop.request').exists():
            alive=bool(proc and proc.poll() is None)
            if not alive:
                try:
                    existing=psutil.Process(int((ROOT/'data/edge.pid').read_text()))
                    alive=existing.is_running() and existing.exe().lower()==str(binary).lower() and str(config.parent).lower() in existing.cwd().lower()
                except (OSError,ValueError,psutil.Error):alive=False
            if not alive:
                with (ROOT/'logs/https-process.log').open('ab') as log:
                    proc=subprocess.Popen(args,cwd=config.parent,stdout=log,stderr=log,creationflags=flags)
                (ROOT/'data/edge.pid').write_text(str(proc.pid))
            now=time.time()
            if now-last_map>1800:
                last_map=now
                mapping='config_missing' if not private.exists() else outcome(
                    [POWERSHELL,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/https_upnp.ps1'),'-ConfigPath',str(private)],
                    MAPPING_TIMEOUT,MAPPING,flags)
                record(mapped=now,mapping=mapping)
            if now-last_renew>86400:
                last_renew=now
                before=certificate.stat().st_mtime
                renewal='config_missing' if not private.exists() else outcome(
                    [str(ROOT/'.venv/Scripts/python.exe'),str(ROOT/'scripts/https_renew.py')],RENEWAL_TIMEOUT,RENEWAL,flags)
                ok=renewal=='ok'
                record(checked=now,renew_ok=ok,renewal=renewal)
                if ok and certificate.stat().st_mtime!=before:
                    subprocess.run([str(binary),'-p',str(config.parent)+'/', '-c','nginx.conf','-s','reload'],capture_output=True,creationflags=flags)
            stop.wait(20)
    finally:
        if proc and proc.poll() is None:
            subprocess.run([str(binary),'-p',str(config.parent)+'/', '-c','nginx.conf','-s','quit'],capture_output=True,creationflags=flags)
