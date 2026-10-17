"""Bounded idle loading and existing-backend acquisition for enrolled subtitles.

Never opens a player, instantiates Engine, or submits directly to a downloader.
Stops on changed code or an unknown acquisition response instead of replaying it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CODE = ('anime/viewing_workflow.py', 'anime/subtitle_delivery.py', 'anime/subtitle_studio.py',
        'anime/subtitle_enrollment.py', 'anime/season.py',
        'anime/native_indexes.py', 'anime/research_tasks.py', 'anime/source_review.py',
        'scripts/deploy_subtitle_enrollment.py')
BASE = 'http://127.0.0.1:4871'


def binding():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in CODE}


def idle(c, now=None):
    now = time.time() if now is None else now
    if c.execute('SELECT 1 FROM subtitle_viewing_leases WHERE expires>?', (now,)).fetchone(): return False
    if c.execute('SELECT 1 FROM web_progress WHERE last_played>?', (now-180,)).fetchone(): return False
    if c.execute("SELECT 1 FROM thunder_queue WHERE lease!='' AND expires>?", (now,)).fetchone(): return False
    row = c.execute("SELECT value FROM kv WHERE key='heartbeat'").fetchone()
    if not row or json.loads(row[0]).get('phase') != 'idle': return False
    return not any((p.info['name'] or '').lower() in ('dandanplay.exe','mpv.exe','vlc.exe')
                   for p in psutil.process_iter(['name']))


def backend_ready(session):
    response = session.get(BASE+'/api/subtitle-delivery', timeout=15)
    response.raise_for_status()
    features = response.json().get('preparation_features', {})
    return (features.get('all_available_unwatched') is True
            and features.get('watching_auto_enrollment') is True
            and features.get('watched_acquisition_guard') is True
            and 'subtitle_only' in features.get('research_modes', []))


def acquire(session, c, manifests):
    """One request per episode, through the running host's guarded queue."""
    results = []
    for manifest in manifests:
        if manifest.get('error'): continue
        ep, identity = manifest['episode'], manifest['hash']
        if c.execute('SELECT 1 FROM watches WHERE show_id=511177 AND episode=? AND finished=1', (ep,)).fetchone():
            results.append({'episode':ep, 'state':'watched_preserved'}); continue
        task = c.execute('SELECT status FROM tasks WHERE hash=?', (identity,)).fetchone()
        if task:
            results.append({'episode':ep, 'hash':identity, 'state':'existing_task_preserved', 'task_status':task[0]}); continue
        # The token is process-local and never included in receipts/logs.
        boot = session.get(BASE+'/api/bootstrap', timeout=15); boot.raise_for_status()
        headers = {'X-Anime-Token':boot.json()['token'], 'Origin':BASE}
        try:
            response = session.post(BASE+f'/api/subtitle-studio/511177/{ep}/acquire',
                headers=headers, json={'candidate_ids':[manifest['candidate_id']], 'role':'clean_base'}, timeout=90)
            response.raise_for_status()
        except requests.RequestException:
            task = c.execute('SELECT status FROM tasks WHERE hash=?', (identity,)).fetchone()
            results.append({'episode':ep, 'hash':identity, 'state':'acquisition_needs_reconcile',
                            'task_status':task[0] if task else None})
            # Do not retry an uncertain external outcome, even after timeout.
            return results
        task = c.execute('SELECT status FROM tasks WHERE hash=?', (identity,)).fetchone()
        results.append({'episode':ep, 'hash':identity, 'state':'backend_request_completed',
                        'task_status':task[0] if task else None})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=14400)
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    lock = out/'deployment.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, 'w') as stream: stream.write(str(os.getpid()))
    expected = binding()
    def save(state, **values):
        data = {'state':state, 'time':time.time(), 'pid':os.getpid(), 'code':expected, **values}
        temp = out/'deployment.tmp'
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(out/'deployment.json')
    try:
        candidates = json.loads((out/'candidate-manifests.json').read_text(encoding='utf-8'))
        # Start with one Chinese soft-subtitle candidate for each requested
        # episode. Other retained candidates remain available for comparison.
        manifests = [m for m in candidates if m['candidate_id'] in (6037,7323)]
        if len(manifests) != 2 or any(m.get('error') for m in manifests):
            raise ValueError('Both verified primary manifests are required')
        from anime.releases import bdecode
        for manifest in manifests:
            if bdecode((ROOT/manifest['torrent']).read_bytes())[0] != manifest['hash']:
                raise ValueError('Immutable candidate torrent changed')
        session = requests.Session(); session.trust_env = False
        c = sqlite3.connect((ROOT/'data/anime.db').resolve().as_uri()+'?mode=ro', uri=True)
        deadline = time.monotonic()+args.timeout_seconds
        reloaded = False
        save('waiting_for_playback_idle')
        while time.monotonic() < deadline:
            if binding() != expected: save('stopped_code_changed'); return
            try:
                if backend_ready(session):
                    results = acquire(session, c, manifests)
                    # Refresh the existing public projection after queueing;
                    # this does not start another acquisition or writer.
                    from scripts.subtitle_delivery_queue import ControlStore
                    from anime.subtitle_delivery import SubtitleDelivery
                    from anime.subtitle_delivery_view import export_view
                    db = ControlStore(ROOT/'data/anime.db')
                    SubtitleDelivery(db).refresh()
                    export_view(db, ROOT)
                    unknown = any(x['state']=='acquisition_needs_reconcile' for x in results)
                    save('acquisition_needs_reconcile' if unknown else 'loaded_and_materials_queued',
                         reloaded=reloaded, acquisition=results)
                    return
            except requests.RequestException:
                # During the one allowed supervised reload, wait for the new
                # listener; never reload again or interpret outage as absence.
                time.sleep(15); continue
            if reloaded:
                save('backend_feature_verification_failed_after_one_reload'); return
            if not idle(c): time.sleep(15); continue
            supervisor = psutil.Process(int((ROOT/'data/supervisor.pid').read_text()))
            if (Path(supervisor.cwd()).resolve() != ROOT
                    or not any(Path(a).name=='supervisor.py' for a in supervisor.cmdline())):
                raise ValueError('Supervisor identity changed')
            listeners = {x.pid for x in psutil.net_connections('tcp')
                         if x.status=='LISTEN' and x.laddr.port==4871}
            if len(listeners)!=1: raise ValueError('Expected one media backend listener')
            worker = psutil.Process(listeners.pop())
            if Path(worker.cwd()).resolve()!=ROOT or 'anime.run' not in worker.cmdline():
                raise ValueError('Backend process identity changed')
            time.sleep(2)
            if binding()!=expected: save('stopped_code_changed'); return
            if not idle(c): continue
            save('reloading_idle_backend', old_pid=worker.pid)
            worker.terminate(); reloaded = True
            time.sleep(3)
        save('expired_without_interrupting_playback')
    except Exception as exc:
        save('stopped_error', error_type=type(exc).__name__)
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
