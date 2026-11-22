"""Reopen the installed desktop app while the user session exists. Never unlock Windows."""
import json,logging,subprocess,time
from pathlib import Path
import psutil
from .db import ROOT

def maintain_desktop(stop,db):
    while not stop.is_set():
        try:
            config=ROOT/'data/desktop-autostart.json'
            settings=json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
            if settings.get('enabled'):
                # Use the installed package identity; a codex CLI process is not the desktop app.
                alive=False
                for p in psutil.process_iter(['name','exe']):
                    exe=(p.info['exe'] or '').replace('\\','/').lower()
                    if '/windowsapps/openai.codex_' in exe and exe.endswith(('/codex.exe','/chatgpt.exe')):alive=True;break
                if not alive:
                    appid=settings.get('app_id')
                    if appid!='OpenAI.Codex_2p2nqsd0c76g0!App':raise ValueError('Desktop app identity not verified')
                    subprocess.Popen(['explorer.exe','shell:AppsFolder\\'+appid],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                db.set('desktop_health',{'time':time.time(),'was_running':alive,'autostart':True,'unlock':'manual'})
        except Exception as e:
            try:db.set('desktop_health',{'time':time.time(),'error':type(e).__name__})
            except Exception as recording:logging.getLogger(__name__).warning('desktop_health_unrecorded error=%s recording=%s',type(e).__name__,type(recording).__name__)
        stop.wait(120)
