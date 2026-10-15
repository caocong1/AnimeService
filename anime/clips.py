"""Local, cancellable clip exports from registered media; originals stay read-only."""
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse


def number(value, label, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + '必须是有效数字')
    if integer and value != int(value):
        raise ValueError(label + '必须是整数')
    return int(value) if integer else float(value)


def options(data, info):
    if set(data) - {'start', 'end', 'format', 'audio', 'audio_index', 'width', 'fps', 'crop', 'quality', 'loop'}:
        raise ValueError('截取参数不受支持')
    start, end = number(data.get('start'), '开始时间'), number(data.get('end'), '结束时间')
    fmt = data.get('format', 'mp4')
    if fmt not in ('mp4', 'gif'):
        raise ValueError('请选择MP4或GIF')
    limit = 300 if fmt == 'mp4' else 30
    if not 0 <= start < end <= info['duration'] or not .1 <= end-start <= limit:
        raise ValueError(f'请选择视频内的有效片段（至少0.1秒；{fmt.upper()}最长{limit}秒）')
    width = number(data.get('width', 0), '输出宽度', True)
    if width != 0 and not 64 <= width <= 3840:
        raise ValueError('输出宽度范围为64–3840像素，0表示原尺寸')
    fps = number(data.get('fps', 0), '帧率', True)
    if (fmt == 'gif' and not 5 <= fps <= 30) or (fmt == 'mp4' and fps != 0 and not 5 <= fps <= 60):
        raise ValueError('GIF帧率为5–30；MP4为5–60，0表示原帧率')
    crop = data.get('crop')
    w, h = info['width'], info['height']
    if crop is not None:
        if not isinstance(crop, dict) or set(crop) != {'x', 'y', 'width', 'height'}:
            raise ValueError('裁剪区域无效')
        crop = {k: number(v, '裁剪区域', True) for k, v in crop.items()}
        if not (0 <= crop['x'] and 0 <= crop['y'] and 2 <= crop['width'] and 2 <= crop['height']
                and crop['x']+crop['width'] <= w and crop['y']+crop['height'] <= h):
            raise ValueError('裁剪区域超出视频画面')
        w, h = crop['width'], crop['height']
    out_w = width or w
    out_h = max(2, round(out_w*h/w))
    if fmt == 'mp4':
        out_w = max(2, out_w//2*2); out_h = max(2, out_h//2*2)
    if out_w > 3840 or out_h > 3840 or (fmt == 'gif' and out_w*out_h > 1280*720):
        raise ValueError('输出画面过大，请缩小尺寸（GIF最多约92万像素，建议宽度480或640）')
    audio = data.get('audio', True)
    loop = data.get('loop', True)
    if not isinstance(audio, bool) or not isinstance(loop, bool):
        raise ValueError('音频和循环选项必须为开关')
    audio_index = data.get('audio_index')
    if audio_index is not None:
        audio_index = number(audio_index, '音轨', True)
        if not any(t['index'] == audio_index for t in info['audio_tracks']):
            raise ValueError('所选音轨不存在')
    else:
        tracks = info['audio_tracks']
        chosen = next((t for t in tracks if t['default']), tracks[0] if tracks else None)
        audio_index = chosen['index'] if chosen else None
    quality = data.get('quality', 'standard')
    if quality not in ('standard', 'high', 'compact'):
        raise ValueError('画质选项无效')
    return dict(start=start, end=end, format=fmt, audio=bool(audio and fmt == 'mp4' and audio_index is not None),
                audio_index=audio_index, width=out_w, height=out_h, fps=fps, crop=crop, quality=quality, loop=loop)


def command(tool, item, info, opt, target):
    args = [tool, '-nostdin', '-v', 'error', '-n', '-threads', '2', '-filter_complex_threads', '1',
            '-protocol_whitelist', 'file,pipe', '-ss', str(opt['start']),
            '-t', str(opt['end']-opt['start']), '-i', item['path'],
            '-t', str(opt['end']-opt['start']), '-map_metadata', '-1', '-sn', '-dn']
    filters = [f"scale={info['width']}:{info['height']}:flags=lanczos", 'setsar=1']
    c = opt['crop']
    if c:
        filters.append(f"crop={c['width']}:{c['height']}:{c['x']}:{c['y']}:exact=1")
    filters.append(f"scale={opt['width']}:{opt['height']}:flags=lanczos")
    if opt['fps']:
        filters.append(f"fps={opt['fps']}")
    vf = ','.join(filters)
    if opt['format'] == 'gif':
        args += ['-filter_complex', f"[0:{info['video_index']}]{vf},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a[v]",
                 '-map', '[v]', '-an', '-loop', '0' if opt['loop'] else '-1']
    else:
        args += ['-map', f"0:{info['video_index']}", '-vf', vf, '-c:v', 'libx264', '-threads', '2',
                 '-preset', 'veryfast', '-crf', {'high':'18','standard':'23','compact':'28'}[opt['quality']],
                 '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
        if opt['audio']:
            args += ['-map', f"0:{opt['audio_index']}", '-c:a', 'aac', '-b:a', '192k']
        else:
            args += ['-an']
    return args + ['-progress', 'pipe:1', '-nostats', str(target)]


class Clips:
    MAX_FILE = 512*1024**2
    MAX_CACHE = 8*1024**3

    def __init__(self, player):
        self.player = player
        self.tools = player.subtitles
        self.root = player.db.path.parent/'exports'/'clips'
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False
        self.cache = {}

    def probe(self, item):
        fingerprint = self.tools.fingerprint(item)
        with self.lock:
            cached = self.cache.get(fingerprint)
        if cached:
            return cached
        raw = self.tools.run([self.tools.tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                              '-show_streams', '-show_format', '-of', 'json', item['path']], 25)
        data = json.loads(raw)
        video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'
                      and not s.get('disposition', {}).get('attached_pic')), None)
        if not video:
            raise ValueError('没有可截取的视频画面')
        duration = float(data.get('format', {}).get('duration', video.get('duration', 0)))
        w, h = int(video['width']), int(video['height'])
        sar = str(video.get('sample_aspect_ratio', '1:1')).split(':')
        if len(sar) == 2 and int(sar[0]) > 0 and int(sar[1]) > 0:
            w = round(w*int(sar[0])/int(sar[1]))
        rotation = next((s.get('rotation', 0) for s in video.get('side_data_list', []) if 'rotation' in s), 0)
        if abs(rotation)%180 == 90:
            w, h = h, w
        if not math.isfinite(duration) or not 0 < duration <= 86400 or not 2 <= w <= 16384 or not 2 <= h <= 16384:
            raise ValueError('无法核实视频尺寸或时长')
        if video.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
            raise ValueError('此HDR视频暂不支持片段转码，请选择SDR版本')
        from .audio_tracks import LANGUAGES
        tracks = []
        for s in data.get('streams', []):
            if s.get('codec_type') != 'audio':
                continue
            tags = s.get('tags', {}); lang = tags.get('language', 'und')
            tracks.append(dict(index=int(s['index']), label=LANGUAGES.get(lang, lang)+' · '+str(tags.get('title') or f'音轨 {len(tracks)+1}')[:120],
                               default=bool(s.get('disposition', {}).get('default'))))
        info = dict(duration=duration, width=w, height=h, video_index=int(video['index']),
                    audio_tracks=tracks, fingerprint=fingerprint, mp4_max_seconds=300, gif_max_seconds=30)
        if fingerprint != self.tools.fingerprint(item):
            raise ValueError('原文件已变化，请重新打开')
        with self.lock:
            if len(self.cache) >= 100:
                self.cache.clear()
            self.cache[fingerprint] = info
        return info

    def save(self, job):
        record = {k:v for k,v in job.items() if k not in ('process', 'thread', 'cancel', 'info')}
        temp = self.root/(job['id']+'.json.tmp')
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, self.root/(job['id']+'.json'))

    def start(self, key, data):
        item = self.player.item(key); info = self.probe(item); opt = options(data, info)
        self.tools.tool('ffmpeg')
        with self.lock:
            if self.closed:
                raise ValueError('服务正在退出，请稍后再试')
            if any(j['state'] in ('queued', 'running', 'cancelling') for j in self.jobs.values()):
                raise HTTPException(409, '已有片段正在导出，请等待完成或取消')
            self.root.mkdir(parents=True, exist_ok=True)
            if sum(p.stat().st_size for p in self.root.glob('*') if p.is_file()) + self.MAX_FILE > self.MAX_CACHE:
                raise ValueError('片段导出目录已达到8GiB，请先整理已导出的片段')
            if shutil.disk_usage(self.root).free < self.MAX_FILE+256*1024**2:
                raise ValueError('导出目录可用空间不足768MiB')
            jid = secrets.token_hex(16)
            base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', Path(item['name']).stem)[:120].strip() or '片段'
            name = f"{base} {opt['start']:.3f}-{opt['end']:.3f}.{opt['format']}"
            job = dict(id=jid, media_id=key, fingerprint=info['fingerprint'], options=opt,
                       name=name, state='queued', progress=0, created=time.time(), cancel=threading.Event(), info=info)
            self.jobs[jid] = job; self.save(job)
            if len(self.jobs) > 100:
                old = next((k for k,j in self.jobs.items() if j['state'] not in ('queued','running','cancelling')), None)
                if old:
                    del self.jobs[old]
            thread = threading.Thread(target=self.run, args=(job,), daemon=True, name='clip-export')
            job['thread'] = thread; thread.start()
            return self.public(job)

    def public(self, job):
        return {k:job[k] for k in ('id', 'media_id', 'name', 'state', 'progress', 'created', 'options')}

    def load(self, key, jid):
        if not re.fullmatch('[0-9a-f]{32}', jid):
            raise HTTPException(404, '片段不存在')
        job = self.jobs.get(jid)
        if not job:
            try:
                job = json.loads((self.root/(jid+'.json')).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise HTTPException(404, '片段不存在') from None
            if job['state'] in ('queued', 'running', 'cancelling'):
                job['state'] = 'interrupted'
        if job['media_id'] != key:
            raise HTTPException(404, '片段不存在')
        return job

    def status(self, key, jid):
        # Revalidate source visibility (including publisher blacklist) on every access.
        self.player.item(key)
        with self.lock:
            return self.public(self.load(key, jid))

    def cancel(self, key, jid):
        self.player.item(key)
        with self.lock:
            job = self.load(key, jid)
            if job.get('cancel') and job['state'] in ('queued', 'running', 'cancelling'):
                job['cancel'].set(); job['state'] = 'cancelling'
                process = job.get('process')
                if process and process.poll() is None:
                    process.terminate()
            return self.public(job)

    def run(self, job):
        opt = job['options']; jid = job['id']; process = None
        partial = self.root/(jid+'.partial.'+opt['format']); target = self.root/(jid+'.'+opt['format'])
        try:
            item = self.player.item(job['media_id'])
            if self.tools.fingerprint(item) != job['fingerprint']:
                raise ValueError('原文件已变化')
            with self.lock:
                if job['cancel'].is_set():
                    job['state'] = 'cancelled'; return
                process = subprocess.Popen(command(self.tools.tool('ffmpeg'), item, job['info'], opt, partial),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                job['process'] = process; job['state'] = 'running'
            # Independent watchdog covers stalls where FFmpeg writes no progress.
            def watch():
                deadline = time.monotonic()+600
                while process.poll() is None:
                    if job['cancel'].wait(.3) or time.monotonic() > deadline or (partial.exists() and partial.stat().st_size > self.MAX_FILE):
                        if process.poll() is None:
                            process.kill()
                        return
            threading.Thread(target=watch, daemon=True, name='clip-watchdog').start()
            for line in process.stdout:
                if line.startswith('out_time_us='):
                    try:
                        progress = min(99, max(0, int(float(line.partition('=')[2])/1e6/(opt['end']-opt['start'])*100)))
                    except ValueError:
                        continue
                    with self.lock:
                        job['progress'] = progress
            code = process.wait()
            with self.lock:
                if job['cancel'].is_set():
                    job['state'] = 'cancelled'
                elif code or not partial.exists() or not 0 < partial.stat().st_size <= self.MAX_FILE:
                    job['state'] = 'failed'
                else:
                    item = self.player.item(job['media_id'])
                    if self.tools.fingerprint(item) != job['fingerprint']:
                        raise ValueError('原文件已变化')
                    os.replace(partial, target); job['state'] = 'complete'; job['progress'] = 100
        except Exception:
            with self.lock:
                job['state'] = 'cancelled' if job['cancel'].is_set() else 'failed'
        finally:
            if process:
                if process.poll() is None:
                    process.kill(); process.wait()
                process.stdout.close()
            partial.unlink(missing_ok=True)
            with self.lock:
                job.pop('process', None); self.save(job)

    def file(self, key, jid):
        self.player.item(key)
        with self.lock:
            job = self.load(key, jid)
            if job['state'] != 'complete':
                raise HTTPException(409, '片段尚未导出完成')
            target = self.root/(jid+'.'+job['options']['format'])
            if not target.is_file():
                raise HTTPException(404, '片段文件已不存在')
            return target, job

    def close(self):
        with self.lock:
            self.closed = True
            active = [j for j in self.jobs.values() if j['state'] in ('queued', 'running', 'cancelling')]
            for job in active:
                job['cancel'].set()
                process = job.get('process')
                if process and process.poll() is None:
                    process.kill()
        for job in active:
            job['thread'].join(timeout=3)


def register_clips(app, player):
    clips = Clips(player); player.clips = clips

    @app.get('/api/web/media/{key}/clips')
    def info(key: str):
        return clips.probe(player.item(key))

    @app.post('/api/web/media/{key}/clips')
    def start(key: str, data: dict):
        return clips.start(key, data)

    @app.get('/api/web/media/{key}/clips/{jid}')
    def status(key: str, jid: str):
        return clips.status(key, jid)

    @app.post('/api/web/media/{key}/clips/{jid}/cancel')
    def cancel(key: str, jid: str):
        return clips.cancel(key, jid)

    @app.get('/api/web/media/{key}/clips/{jid}/file')
    def file(key: str, jid: str, download: bool = False):
        path, job = clips.file(key, jid)
        return FileResponse(path, media_type='video/mp4' if job['options']['format'] == 'mp4' else 'image/gif',
                            filename=job['name'], content_disposition_type='attachment' if download else 'inline')
