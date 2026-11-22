"""Build one complete local subtitled release from frozen episode inputs.

No scheduler, model, downloader, Engine, database or acceptance writes. Review
notes travel with the release and do not prevent a technically playable output.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def write(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


def run(args, cwd, timeout=120):
    return subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)


def check_unwatched(c):
    if c.get('delivery_job_id') or (PROJECT/'data/anime.db').is_file():
        from scripts.subtitle_delivery_queue import ReadStore
        db = ReadStore(PROJECT / 'data/anime.db')
        try:
            from anime.subtitle_dispatch_fence import check_output
            check_output(db,c['show_id'],c['episode'])
            if c.get('delivery_job_id') and db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (c['show_id'], c['episode'])):
                raise ValueError('Episode already watched; preserve existing subtitles')
        finally:
            db.con.rollback(); db.con.close()


def mapped_duration_check(ffprobe, source, movie, cwd, src_probe, probe):
    """MKV's format duration can include a longer unselected subtitle/dub track."""
    container_delta = abs(float(probe['format']['duration']) - float(src_probe['format']['duration']))
    if container_delta < .06:
        return {'container_delta_seconds': container_delta, 'mapped_video_checked': False}
    def extent(path):
        packets = json.loads(run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                                 '-show_entries', 'packet=pts_time,duration_time', '-of', 'json', str(path)], cwd).stdout)['packets']
        return {'count': len(packets), 'first': min(float(p['pts_time']) for p in packets),
                'end': max(float(p['pts_time']) + float(p.get('duration_time', 0)) for p in packets)}
    original, encoded = extent(source), extent(movie)
    assert original['count'] == encoded['count'], 'Video frame count changed'
    assert abs(original['first'] - encoded['first']) < .06 and abs(original['end'] - encoded['end']) < .06, 'Mapped video duration changed'
    return {'container_delta_seconds': container_delta, 'mapped_video_checked': True,
            'source': original, 'output': encoded}


def encode_release(args, root, render_lease, outcome):
    """Own exactly one child, observe real progress, and retain its final facts."""
    from scripts.subtitle_render_watchdog import wait_for_render
    process = None
    started = time.monotonic()
    try:
        with (root / 'render.log').open('xb') as log:
            with render_lease.child_options() as child_options:
                process = subprocess.Popen(args, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                           **child_options)
            write(root / 'process.json', {'controller_pid': os.getpid(), 'worker_pid': process.pid})
            print(json.dumps({'render_started': True, 'pid': process.pid, 'root': str(root)}), flush=True)
            progress = root / 'render-progress.txt'
            if outcome.get('remote_job'):
                # Preserve the usual progress path; ssh transports ffmpeg progress and logs together.
                os.link(root / 'render.log', progress)
            code = wait_for_render(process, progress, outcome)
            assert code == 0, 'Render failed; preserve output and log'
    except BaseException as error:
        outcome['error'] = {'type': type(error).__name__, 'message': str(error)[:500]}
        # Only this Popen handle is controlled. Never rediscover/kill a PID tree
        # or release somebody else's encoding slot after an uncertain stop.
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=20)
            except Exception as stop_error:
                outcome['stop_error'] = {'type': type(stop_error).__name__, 'message': str(stop_error)[:500]}
        raise
    finally:
        outcome.update(spawned=process is not None, stopped=process is None or process.poll() is not None,
                       exit_code=process.returncode if process is not None else None,
                       seconds=time.monotonic() - started)
        write(root / 'render-outcome.json', outcome)


def build(config):
    c = json.loads(Path(config).read_text(encoding='utf-8-sig'))
    import sys
    sys.path.insert(0, str(PROJECT))
    if c.get('edition') is not None and c['edition'] not in ('v0', 'v1'):
        raise ValueError('Unknown viewing edition')
    check_unwatched(c)
    def stage(name):
        if not c.get('delivery_job_id') or not c.get('delivery_thread_id'):
            return
        # Optional metadata only; progress view failure cannot discard a movie.
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from scripts.subtitle_delivery_queue import ControlStore, ROOT
            from anime.subtitle_delivery_view import report_progress, export_view
            db = ControlStore(ROOT / 'data/anime.db')
            report_progress(db, c['delivery_job_id'], c['delivery_thread_id'], name)
            export_view(db, ROOT)
        except (ValueError, OSError, KeyError, TypeError):
            pass
    root = Path(c['output']).resolve()
    if (root / 'release.json').is_file():
        old = json.loads((root / 'release.json').read_text(encoding='utf-8'))
        if old['input_config_sha256'] != digest(config):
            raise ValueError('Existing release uses different inputs; create a new version.')
        for name, record in old['files'].items():
            if digest(root / name) != record['sha256']:
                raise ValueError('Existing release changed; do not overwrite it.')
        print(json.dumps({'status': 'existing_completed_release_reused', 'root': str(root)}))
        return
    if c.get('edition') == 'v1':
        from scripts.merge_subtitle_issues import merge_for_build
        merge_for_build(PROJECT, c)
        from anime.subtitle_refinement import validate_review
        validate_review(Path(__file__).resolve().parents[1], c['refinement_review'], c['show_id'], c['episode'],
                        c['media_sha256'], c.get('baseline_release_id'))
    from anime.subtitle_coverage import validate as validate_coverage
    from anime.subtitle_display_rows import validate as validate_display_rows
    validate_coverage(Path(__file__).resolve().parents[1], c.get('chinese_coverage_review', ''),
                      c['ass'], c['show_id'], c['episode'], c['media_sha256'])
    validate_display_rows(c['ass'])
    if c.get('static_ass'):
        validate_display_rows(c['static_ass'])
    from scripts.subtitle_output_lock import output_lock
    # Queue before creating an immutable release directory. A busy slot leaves
    # no partial output and never changes a job to blocked/stopped.
    with output_lock(PROJECT / 'data/subtitle-studio/.render.process.lock',
                     'subtitle_encoding', timeout=5400) as render_lease:
        check_unwatched(c)
        validate_coverage(PROJECT, c.get('chinese_coverage_review', ''),
                          c['ass'], c['show_id'], c['episode'], c['media_sha256'])
        if c.get('edition') == 'v1':
            validate_review(PROJECT, c['refinement_review'], c['show_id'], c['episode'],
                            c['media_sha256'], c.get('baseline_release_id'))
        return build_release(config, c, root, stage, validate_coverage, render_lease, configured_render_host(c))


def configured_render_host(c):
    remote_host = None
    deployment = PROJECT / 'data/subtitle-render-worker.json'
    if deployment.exists():
        from scripts.subtitle_remote_render import available
        host = json.loads(deployment.read_text(encoding='utf-8-sig'))['host']
        deadline = time.monotonic() + 600
        while True:
            try:
                if available(host):
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                from scripts.subtitle_output_lock import OutputBusyError
                raise OutputBusyError(f'Mac render worker {host} offline before dispatch; inputs retained for retry')
            print(json.dumps({'status': 'waiting_for_render_host', 'host': host}), flush=True)
            time.sleep(min(20, remaining))
            check_unwatched(c)
        remote_host = host
    return remote_host


def build_release(config, c, root, stage, validate_coverage, render_lease, remote_host=None):
    if remote_host is None and c.get('delivery_job_id'):
        remote_host = configured_render_host(c)
    root.mkdir(parents=True, exist_ok=False)
    source = Path(c['media'])
    ass_source = Path(c['ass'])
    assert digest(source) == c['media_sha256'], 'Media changed'
    assert digest(ass_source) == c['ass_sha256'], 'ASS changed'
    assert shutil.disk_usage(root).free > source.stat().st_size * 2 + 512 * 1024**2
    source_stat = (source.stat().st_size, source.stat().st_mtime_ns)
    ffmpeg, ffprobe = c['ffmpeg'], c['ffprobe']
    width = int(c.get('width', 1920))
    assert 640 <= width <= 3840 and width % 2 == 0
    original = ass_source.read_text(encoding='utf-8-sig').splitlines()
    omit = c.get('omit_label_style')
    removed = []
    final = []
    for line in original:
        if line.startswith('Title:'):
            line = 'Title: ' + c['title']
        if omit and line.startswith('Dialogue:') and line.split(',', 4)[3] == omit:
            removed.append(line)
            continue
        final.append(line)
    if omit:
        assert len(removed) == 1, 'Unexpected label count'
    (root / 'bilingual.ass').write_text('\n'.join(final) + '\n', encoding='utf-8-sig')
    shutil.copy2(c['chinese_coverage_review'], root / 'chinese-coverage-review.json')
    # Title/label processing must not silently remove reviewed dialogue.
    validate_coverage(Path(__file__).resolve().parents[1], root / 'chinese-coverage-review.json',
                      root / 'bilingual.ass', c['show_id'], c['episode'], c['media_sha256'], c['ass_sha256'])
    if c.get('edition') == 'v1':
        shutil.copy2(c['refinement_review'], root / 'refinement-review.json')
    if c.get('static_ass'):
        assert digest(c['static_ass']) == c['static_ass_sha256'], 'Static ASS changed'
        shutil.copy2(c['static_ass'], root / 'static.ass')
    else:
        shutil.copy2(root / 'bilingual.ass', root / 'static.ass')
    from anime.subtitle_display_rows import validate as validate_display_rows
    for subtitle in ('bilingual.ass', 'static.ass'):
        validate_display_rows(root / subtitle)
    (root / 'fonts').mkdir()
    for font in c['fonts']:
        assert digest(font['path']) == font['sha256'], 'Font changed'
        shutil.copy2(font['path'], root / 'fonts' / Path(font['path']).name)
    licenses = []
    for notice in c.get('font_licenses', []):
        assert digest(notice['path']) == notice['sha256'], 'Font license changed'
        (root / 'font-licenses').mkdir(exist_ok=True)
        target = root / 'font-licenses' / Path(notice['path']).name
        shutil.copy2(notice['path'], target)
        licenses.append(target)
    for ext in ('srt', 'vtt'):
        if c.get(ext):
            shutil.copy2(c[ext], root / ('bilingual.' + ext))
    shutil.copy2(config, root / 'input-config.json')
    movie = root / c['filename']
    assert movie.parent == root and movie.suffix == '.mp4'
    lock = root / 'worker.lock.json'
    write(lock, {'owner_pid': os.getpid(), 'started': time.time(), 'purpose': 'one bounded release render'})
    args = [ffmpeg, '-nostdin', '-n', '-v', 'info', '-i', str(source),
            '-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '-1',
            '-vf', f'subtitles=bilingual.ass:fontsdir=fonts,scale={width}:-2',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', '6',
            '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart',
            '-progress', str(root / 'render-progress.txt'), str(movie)]
    outcome = {}
    try:
        stage('rendering')
        if remote_host:
            from scripts.subtitle_remote_render import render
            outcome['remote_decode'] = render(remote_host, args, root, c['media_sha256'],
                                              encode_release, render_lease, outcome)
        else:
            encode_release(args, root, render_lease, outcome)
        finalize_rendered(config, c, root, stage, source, source_stat, width, movie, removed, outcome)
    finally:
        if outcome.get('stopped', True) and lock.is_file() and json.loads(lock.read_text())['owner_pid'] == os.getpid():
            lock.unlink()


def verified_movie_decode(c, root, movie, duration, outcome):
    from scripts.subtitle_remote_media import preparation
    host = json.loads((PROJECT / 'data/subtitle-render-worker.json').read_text(encoding='utf-8-sig'))['host']
    expected = digest(movie)
    size = movie.stat().st_size
    for name in ('remote-render.json', 'mac-final-decode.json'):
        try:
            saved = json.loads((root / name).read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            continue
        if (isinstance(saved, dict) and saved.get('host') == host and saved.get('stage') == 'returned'
                and saved.get('decode') is True and saved.get('sha256') == expected and saved.get('bytes') == size):
            outcome['worker'] = host
            return saved
    with preparation(host, movie, expected, PROJECT / 'data', {'mode': 'decode'},
                     lambda: check_unwatched(c)) as (progress, route):
        lines = progress.read_text().strip().splitlines()
        times = [int(line.split('=', 1)[1]) for line in lines if line.startswith('out_time_us=')]
        if not times or lines[-1] != 'progress=end' or abs(times[-1]/1e6 - duration) > 1:
            raise ValueError('Incomplete Mac movie decode')
        if digest(movie) != expected:
            raise ValueError('Movie changed during Mac verification')
        saved = dict(route, stage='returned', decode=True, sha256=expected, bytes=size, decoded_us=times[-1])
    path = root / 'mac-final-decode.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(saved), encoding='utf-8')
    temp.replace(path)
    outcome['worker'] = host
    return saved


def finalize_rendered(config, c, root, stage, source, source_stat, width, movie, removed, outcome, symbol_review=None):
    ffmpeg, ffprobe = c['ffmpeg'], c['ffprobe']
    licenses = []
    for notice in c.get('font_licenses', []):
        target = root / 'font-licenses' / Path(notice['path']).name
        assert digest(notice['path']) == notice['sha256'] == digest(target), 'Font license changed'
        licenses.append(target)
    stage('checking')
    probe_args = [ffprobe, '-v', 'error', '-show_entries',
                  'format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels', '-of', 'json']
    probe = json.loads(run(probe_args + [str(movie)], root).stdout)
    src_probe = json.loads(run(probe_args + [str(source)], root).stdout)
    duration_check = mapped_duration_check(ffprobe, source, movie, root, src_probe, probe)
    assert next(s for s in probe['streams'] if s['codec_type'] == 'video')['width'] == width
    remote_decode = verified_movie_decode(c, root, movie, float(probe['format']['duration']), outcome)
    (root / 'decode.log').write_bytes(b'')
    def audio_hash(path):
        return run([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:a:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'], root).stdout.decode().strip()
    source_audio, output_audio = audio_hash(source), audio_hash(movie)
    assert source_audio == output_audio
    assert source_stat == (source.stat().st_size, source.stat().st_mtime_ns)
    selected = [line for line in (root / 'render.log').read_text(encoding='utf-8', errors='replace').splitlines() if 'fontselect:' in line]
    from scripts.subtitle_render_fonts import required_for_render, validate as validate_render_fonts
    font_review = validate_render_fonts(selected, required_for_render(c,root), root, digest, symbol_review)
    (root / 'frames').mkdir(exist_ok=True)
    for t in c.get('sample_seconds', [60, 600, 1200]):
        if (root / 'frames' / f'{t}.png').is_file():
            continue
        run([ffmpeg, '-nostdin', '-v', 'error', '-ss', str(t), '-i', str(movie), '-frames:v', '1',
             '-threads', '2', str(root / 'frames' / f'{t}.png')], root)
    notes = c['title'] + '\n\n' + c['notes'] + '\n\n视频已烧好字幕，直接播放MP4即可，无需再加载外挂。外挂字幕包供原片使用。\n'
    (root / '版本说明.txt').write_text(notes, encoding='utf-8-sig')
    files = [root / 'bilingual.ass', root / 'static.ass', root / '版本说明.txt',
             root / 'chinese-coverage-review.json',
             *sorted((root / 'fonts').glob('*.otf')), *licenses,
             *[root / ('bilingual.'+ext) for ext in ('srt', 'vtt') if (root / ('bilingual.'+ext)).is_file()]]
    if c.get('edition') == 'v1':
        files.append(root / 'refinement-review.json')
    with zipfile.ZipFile(root / 'subtitles.zip', 'x', zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, str(f.relative_to(root)))
    with zipfile.ZipFile(root / 'subtitles.zip') as z:
        assert z.testzip() is None
    quality = {'decode_passed': True, 'duration_seconds': float(probe['format']['duration']),
               'duration_check': duration_check,
               'source_audio_packet_sha256': source_audio, 'output_audio_packet_sha256': output_audio,
               'font_selection': selected, 'removed_label_events': removed,
               'source_stat_unchanged': True, 'actual_listening_claimed': False,
               'whole_episode_review_complete': False, 'probe': probe}
    if remote_decode:
        quality['remote_decode'] = {**remote_decode, 'worker': outcome['worker']}
    if font_review:
        quality['symbol_fallback_review'] = font_review
    write(root / 'technical-qa.json', quality)
    files += [movie, root / 'subtitles.zip', root / 'technical-qa.json']
    release = {'id': c['id'], 'show_id': c['show_id'], 'season': c['season'], 'episode': c['episode'],
               'version': c['version'], 'title': c['title'], 'status': 'finished',
               'finished_at': datetime.now().astimezone().isoformat(),
               'input_config_sha256': digest(config), 'source_media_sha256': c['media_sha256'],
               'source_ass_sha256': c['ass_sha256'], 'render_seconds': outcome['seconds'],
               'duration_seconds': quality['duration_seconds'], 'width': width,
               'height': next(s['height'] for s in probe['streams'] if s['codec_type']=='video'),
               'formal_review_complete': False, 'known_notes': c['notes'],
               'files': {str(p.relative_to(root)): {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files}}
    if c.get('edition'):
        release.update(edition=c['edition'], baseline_release_id=c.get('baseline_release_id'))
    write(root / 'release.json', release)
    print(json.dumps({'status': 'finished', 'video': str(movie), 'seconds': outcome['seconds'],
                      'bytes': movie.stat().st_size}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    build(parser.parse_args().config)
