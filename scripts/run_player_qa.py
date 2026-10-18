"""Create synthetic media + isolated DB, run browser checks, stop only our fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import requests

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--vendor-root',type=Path);args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    qa_root=output/('anime-player-qa-'+secrets.token_hex(8));qa_root.mkdir()
    token=secrets.token_urlsafe(32)
    (qa_root/'.fixture-token-sha256').write_text(hashlib.sha256(token.encode()).hexdigest(),encoding='ascii')
    env={**os.environ,'ANIMESERVICE_START_WORKER':'0','ANIMESERVICE_DB':str(qa_root/'qa.db'),
         'ANIMESERVICE_DEPLOYMENT':str(qa_root/'deployment.json'),'PLAYER_QA_ROOT':str(qa_root),
         'PLAYER_QA_TOKEN':token,'PLAYER_QA_BASE':'http://127.0.0.1:4879','PLAYER_QA_OUT':str(qa_root/'proof')}
    if args.vendor_root:env['ANIMESERVICE_QA_VENDOR_ROOT']=str(args.vendor_root.resolve())
    sys.path.insert(0,str(ROOT))
    from anime.subtitles import Subtitles
    from anime.db import DEFAULTS
    from types import SimpleNamespace
    ffmpeg=Subtitles(SimpleNamespace(path=qa_root/'qa.db',get=lambda key:DEFAULTS[key])).tool('ffmpeg')
    catalog=qa_root/'static/finished-media';first=catalog/'qa-s01e02-v0';first.mkdir(parents=True)
    media=first/'episode.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-f','lavfi','-i','testsrc2=size=320x180:rate=2',
        '-f','lavfi','-i','anullsrc=channel_layout=mono:sample_rate=48000','-t','1421','-c:v','libx264','-preset','ultrafast',
        '-crf','35','-pix_fmt','yuv420p','-c:a','aac','-b:a','32k','-movflags','+faststart',str(media)],check=True,timeout=120)
    sha=hashlib.sha256(media.read_bytes()).hexdigest();releases=[]
    for version in (0,1):
        identity=f'qa-s01e02-v{version}';folder=catalog/identity;folder.mkdir(exist_ok=True)
        if version:shutil.copyfile(media,folder/'episode.mp4')
        releases.append({'id':identity,'show_id':42,'season':1,'episode':2,'edition':f'v{version}',
            'version':version,'status':'finished','title':'合成验收素材','video_sha256':sha,'duration_seconds':1421})
    (catalog/'index.json').write_text(json.dumps({'releases':releases},ensure_ascii=False),encoding='utf-8')
    with socket.socket() as check:check.bind(('127.0.0.1',4879))
    with (qa_root/'server.log').open('w',encoding='utf-8') as log:
        server=subprocess.Popen([sys.executable,'-m','uvicorn','scripts.unified_player_fixture:app','--host','127.0.0.1','--port','4879','--no-access-log'],
            cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            session=requests.Session();session.trust_env=False
            for _ in range(100):
                if server.poll() is not None:raise RuntimeError('Fixture stopped before readiness; inspect its isolated server log')
                try:
                    proof=session.get(env['PLAYER_QA_BASE']+'/api/testing/fixture',timeout=1).json()
                    if proof.get('token_sha256')==hashlib.sha256(token.encode()).hexdigest():break
                except (requests.RequestException,ValueError):pass
                time.sleep(.2)
            else:raise RuntimeError('Fixture did not become ready')
            node=shutil.which('node')
            if not node:raise RuntimeError('Node.js is required')
            subprocess.run([node,'scripts/verify_unified_player.cjs'],cwd=ROOT,env=env,check=True,timeout=240)
            import sqlite3
            with sqlite3.connect(qa_root/'qa.db') as con:
                con.execute('DELETE FROM web_progress');con.execute('DELETE FROM finished_playback_receipts')
                con.execute('DELETE FROM watches WHERE episode>1');con.execute('UPDATE shows SET watched=1')
            subprocess.run([node,'scripts/verify_unified_player.cjs'],cwd=ROOT,env={**env,'PLAYER_QA_LEGACY':'1'},check=True,timeout=240)
            print(json.dumps({'isolated':True,'synthetic_media':True,'worker_enabled':False,'checks':['current','legacy'],'proof_dir':str(qa_root/'proof')},ensure_ascii=False))
        finally:
            # Windows venv launchers can own a child Python process. Stop only
            # this Popen tree, never a process selected by a shared port.
            import psutil
            try:
                parent=psutil.Process(server.pid);owned=parent.children(recursive=True)+[parent]
                for process in owned:
                    try:process.terminate()
                    except psutil.NoSuchProcess:pass
                _,alive=psutil.wait_procs(owned,timeout=5)
                for process in alive:
                    try:process.kill()
                    except psutil.NoSuchProcess:pass
                _,alive=psutil.wait_procs(alive,timeout=5)
                if alive:raise RuntimeError('Owned fixture processes did not stop')
            except psutil.NoSuchProcess:pass

if __name__=='__main__':main()
