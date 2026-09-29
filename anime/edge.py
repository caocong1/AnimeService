"""Isolated HTTPS listener. Never reloads or edits the user's shared nginx."""
import json,os,subprocess,time,psutil
from .db import ROOT
from .deployment import load as load_deployment

def maintain_https(stop):
    certificate_path=load_deployment().get('certificate')
    if not certificate_path:return
    config=ROOT/'data/edge/nginx.conf';certificate=ROOT/certificate_path
    binary=ROOT/'data/tools/nginx.exe'
    if not config.exists() or not certificate.exists() or not binary.exists():return
    proc=None;last_renew=0;last_map=0
    args=[str(binary),'-p',str(config.parent)+'/', '-c','nginx.conf','-g','daemon off;']
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
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
                try:subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/Map-AnimeHttps.ps1')],capture_output=True,creationflags=flags,timeout=90)
                except subprocess.TimeoutExpired:pass
            if now-last_renew>86400:
                last_renew=now
                before=certificate.stat().st_mtime
                try:
                    result=subprocess.run([str(ROOT/'.venv/Scripts/python.exe'),str(ROOT/'scripts/renew_https.py')],capture_output=True,creationflags=flags,timeout=2400)
                    ok=result.returncode==0
                except subprocess.TimeoutExpired:ok=False
                (ROOT/'data/https-health.json').write_text(json.dumps({'checked':now,'renew_ok':ok}))
                if ok and certificate.stat().st_mtime!=before:
                    subprocess.run([str(binary),'-p',str(config.parent)+'/', '-c','nginx.conf','-s','reload'],capture_output=True,creationflags=flags)
            stop.wait(20)
    finally:
        if proc and proc.poll() is None:
            subprocess.run([str(binary),'-p',str(config.parent)+'/', '-c','nginx.conf','-s','quit'],capture_output=True,creationflags=flags)
