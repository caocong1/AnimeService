"""Decode the bound episode once per target, sharing the result across preparation rounds."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.subtitle_evidence import _guard, completed_selection, current_target
from anime.subtitle_lab import lab_core
from anime.subtitles import Subtitles
from anime.viewing_workflow import unreleased_date
from scripts.subtitle_delivery_queue import ReadStore
from scripts.subtitle_output_lock import OutputBusyError, output_lock


def bound_media(db_path, sid, ep, owner=None, expected_input=None):
    if (db_path.parent / 'stop.request').exists():
        raise ValueError('Service stopped')
    db = ReadStore(db_path)
    try:
        job = db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (f'episode:{sid}:{ep}',))
        if owner is None and expected_input is None:
            _guard(db, sid, ep, job)
        else:
            from anime.subtitle_delivery import _finished, _scope
            from anime.subtitle_dispatch_fence import check_output
            from anime.viewing_workflow import lane
            check_output(db, sid, ep)
            if (not owner or not expected_input or not job or job['state'] != 'production'
                    or job['dispatch_state'] != 'acknowledged' or job['thread_id'] != owner
                    or job['input_key'] != expected_input or lane(db, sid) != 'watching'
                    or _scope(db, sid, ep) or _finished(db, sid, ep)):
                raise ValueError('Current unwatched production owner and exact input required')
            payload = json.loads(job['payload'])
            if payload.get('user_stopped') or payload.get('owner_stopped_writing'):
                raise ValueError('Stopped episode is protected')
        if unreleased_date(db, sid, ep, time.time()):
            raise ValueError('Episode not released')
        target = current_target(db, sid, ep)
        fingerprint = target.get('fingerprint', '')
        if owner and payload['assessment'].get('target_fingerprint') != fingerprint:
            raise ValueError('Owner target fingerprint changed')
        if not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise ValueError('Episode has no bound target')
        path = Path(target['path']).resolve()
        stat = path.stat()
        if (not completed_selection(db, sid, ep, path)[0] or stat.st_size != target['size']
                or lab_core().fingerprint(path) != fingerprint):
            raise ValueError('Completed target changed')
        sub = Subtitles(db)
        return (str(path), fingerprint, stat.st_size, stat.st_mtime_ns), sub.tool('ffmpeg'), sub.tool('ffprobe')
    finally:
        db.con.close()


def probe(db_path, sid, ep, hardware):
    media, ffmpeg, ffprobe = bound_media(db_path, sid, ep)
    path, fingerprint, size, mtime_ns = media
    folder = db_path.parent / 'subtitle-studio' / 'decode-checks' / f'{sid}-E{ep}' / fingerprint
    def recheck():
        if bound_media(db_path, sid, ep)[0] != media:
            raise ValueError('Episode target changed during preparation')

    with output_lock(folder / '.decode.lock', 'episode preparation decode', timeout=0) as lock:
        recheck()
        # A changed file under the same path/fingerprint needs a separate receipt.
        receipt = folder / f'{size}-{mtime_ns}.json'
        if receipt.is_file():
            data = json.loads(receipt.read_text(encoding='utf8'))
            if (data['target'] != list(media) or data['show_id'] != sid or data['episode'] != ep
                    or data['exit_code'] != 0 or not re.fullmatch('[0-9a-f]{64}', data['media_sha256'])):
                raise ValueError('Existing decode receipt has inconsistent evidence')
            evidence = Path(data['evidence_dir']).resolve()
            age = time.time() - data['checked_at']
            if (0 <= age < 86400 and evidence.parent == folder.resolve()
                    and (evidence / 'decode.progress').is_file() and (evidence / 'probe.json').is_file()
                    and (evidence / 'decode.progress').read_text().strip().endswith('progress=end')):
                return receipt
        run = folder / str(time.time_ns())
        run.mkdir()
        description = subprocess.check_output([ffprobe, '-v', 'error', '-show_format', '-show_streams',
                                               '-of', 'json', path], timeout=60)
        (run / 'probe.json').write_bytes(description)
        duration = float(json.loads(description)['format']['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Target has no finite duration')
        progress = run / 'decode.progress'
        acceleration = (['-threads', '2', '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
                        if hardware == 'cuda' else ['-threads', '6'])
        cmd = [ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-stats_period', '10', '-progress',
               str(progress), *acceleration, '-i', path, '-map', '0:v:0', '-map', '0:a:0', '-sn', '-dn', '-f', 'null', '-']
        deadline = time.monotonic() + max(1800, 4 * duration)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        deployment = db_path.parent / 'subtitle-render-worker.json'
        route = {}
        if deployment.is_file():
            import shutil
            from scripts.subtitle_remote_media import preparation
            host = json.loads(deployment.read_text(encoding='utf-8-sig'))['host']
            with Path(path).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            with preparation(host, Path(path), digest, db_path.parent, {'mode': 'decode'}, recheck) as (file, route):
                shutil.copyfile(file, progress)
            cmd = ['videotoolbox', 'full video and audio decode', host]
        else:
            with (run / 'decode.log').open('xb') as log, lock.child_options() as options:
                with subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=flags, **options) as proc:
                    try:
                        while proc.poll() is None:
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                recheck()
                                if time.monotonic() >= deadline:
                                    raise TimeoutError('Episode decode timed out')
                    except BaseException:
                        proc.kill()
                        proc.wait()
                        raise
            if proc.returncode:
                raise ValueError(f'Decode exited {proc.returncode}. See {run}')
            with Path(path).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        progress_text = progress.read_text().strip()
        end_times = re.findall(r'(?m)^out_time_us=(\d+)$', progress_text)
        if not progress_text.endswith('progress=end') or not end_times:
            raise ValueError(f'Missing decode end position. See {run}')
        seconds = int(end_times[-1]) / 1_000_000
        if abs(seconds - duration) > 1:
            raise ValueError(f'Decoded {seconds:.3f} of {duration:.3f} seconds. See {run}')
        recheck()
        result = dict(show_id=sid, episode=ep, target=list(media), media_sha256=digest,
                      duration_seconds=duration, decoded_seconds=seconds, exit_code=0,
                      evidence_dir=str(run), command=cmd, worker=route, checked_at=time.time())
        temp = run / 'receipt.tmp'
        raw = json.dumps(result, ensure_ascii=False, indent=2)
        (run / 'receipt.json').write_text(raw, encoding='utf8')
        temp.write_text(raw, encoding='utf8')
        os.replace(temp, receipt)
        return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('show_id', type=int)
    parser.add_argument('episode', type=int)
    parser.add_argument('--hardware', choices=('cpu', 'cuda'), default='cpu')
    args = parser.parse_args()
    try:
        print(probe(ROOT / 'data/anime.db', args.show_id, args.episode, args.hardware))
    except OutputBusyError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
