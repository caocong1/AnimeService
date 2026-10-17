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


def build(config):
    c = json.loads(Path(config).read_text(encoding='utf-8-sig'))
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if c.get('edition') is not None and c['edition'] not in ('v0', 'v1'):
        raise ValueError('Unknown viewing edition')
    if c.get('delivery_job_id'):
        from scripts.subtitle_delivery_queue import ReadStore, ROOT
        db = ReadStore(ROOT / 'data/anime.db')
        try:
            if db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (c['show_id'],c['episode'])):
                raise ValueError('Episode already watched; preserve existing subtitles')
        finally:
            db.con.rollback(); db.con.close()
    if c.get('edition') == 'v1':
        from anime.subtitle_refinement import validate_review
        validate_review(Path(__file__).resolve().parents[1], c['refinement_review'], c['show_id'], c['episode'],
                        c['media_sha256'], c.get('baseline_release_id'))
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
    if c.get('edition') == 'v1':
        shutil.copy2(c['refinement_review'], root / 'refinement-review.json')
    if c.get('static_ass'):
        assert digest(c['static_ass']) == c['static_ass_sha256'], 'Static ASS changed'
        shutil.copy2(c['static_ass'], root / 'static.ass')
    else:
        shutil.copy2(root / 'bilingual.ass', root / 'static.ass')
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
    start = time.monotonic()
    args = [ffmpeg, '-nostdin', '-n', '-v', 'info', '-i', str(source),
            '-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '-1',
            '-vf', f'subtitles=bilingual.ass:fontsdir=fonts,scale={width}:-2',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', '6',
            '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart',
            '-progress', str(root / 'render-progress.txt'), str(movie)]
    outcome = {}
    try:
        stage('rendering')
        with (root / 'render.log').open('xb') as log:
            process = subprocess.Popen(args, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            write(root / 'process.json', {'controller_pid': os.getpid(), 'worker_pid': process.pid})
            print(json.dumps({'render_started': True, 'pid': process.pid, 'root': str(root)}), flush=True)
            try:
                code = process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=20)
                outcome['timeout'] = True
                raise
            finally:
                outcome.update(stopped=process.poll() is not None, exit_code=process.returncode,
                               seconds=time.monotonic()-start)
                write(root / 'render-outcome.json', outcome)
            assert code == 0, 'Render failed; preserve output and log'
        stage('checking')
        probe_args = [ffprobe, '-v', 'error', '-show_entries',
                      'format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels', '-of', 'json']
        probe = json.loads(run(probe_args + [str(movie)], root).stdout)
        src_probe = json.loads(run(probe_args + [str(source)], root).stdout)
        duration_check = mapped_duration_check(ffprobe, source, movie, root, src_probe, probe)
        assert next(s for s in probe['streams'] if s['codec_type'] == 'video')['width'] == width
        decode = run([ffmpeg, '-v', 'error', '-i', str(movie), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'], root, 180)
        (root / 'decode.log').write_bytes(decode.stderr)
        def audio_hash(path):
            return run([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:a:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'], root).stdout.decode().strip()
        source_audio, output_audio = audio_hash(source), audio_hash(movie)
        assert source_audio == output_audio
        assert source_stat == (source.stat().st_size, source.stat().st_mtime_ns)
        selected = [line for line in (root / 'render.log').read_text(encoding='utf-8', errors='replace').splitlines() if 'fontselect:' in line]
        assert selected and all(any(f in line.split(' -> ')[-1] for f in c['font_postscript_names']) for line in selected)
        assert all(any(f in line for line in selected) for f in c['font_postscript_names'])
        (root / 'frames').mkdir()
        for t in c.get('sample_seconds', [60, 600, 1200]):
            run([ffmpeg, '-nostdin', '-v', 'error', '-ss', str(t), '-i', str(movie), '-frames:v', '1',
                 '-threads', '2', str(root / 'frames' / f'{t}.png')], root)
        notes = c['title'] + '\n\n' + c['notes'] + '\n\n视频已烧好字幕，直接播放MP4即可，无需再加载外挂。外挂字幕包供原片使用。\n'
        (root / '版本说明.txt').write_text(notes, encoding='utf-8-sig')
        files = [root / 'bilingual.ass', root / 'static.ass', root / '版本说明.txt',
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
    finally:
        if lock.is_file() and json.loads(lock.read_text())['owner_pid'] == os.getpid():
            lock.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    build(parser.parse_args().config)
