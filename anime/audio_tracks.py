"""Selected-audio playback copies video into a bounded, disposable local cache."""
import json
import math
import shutil
import subprocess
import threading
from pathlib import Path


LANGUAGES = {
    'eng': '英语', 'en': '英语', 'jpn': '日语', 'ja': '日语',
    'chi': '中文', 'zho': '中文', 'zh': '中文', 'cze': '捷克语', 'ces': '捷克语',
    'ara': '阿拉伯语', 'ger': '德语', 'deu': '德语', 'gre': '希腊语',
    'ell': '希腊语', 'spa': '西班牙语', 'fil': '菲律宾语', 'fre': '法语',
    'fra': '法语', 'heb': '希伯来语', 'hin': '印地语', 'hun': '匈牙利语',
    'ind': '印度尼西亚语', 'ita': '意大利语', 'kan': '卡纳达语',
    'mal': '马拉雅拉姆语', 'may': '马来语', 'msa': '马来语',
    'dut': '荷兰语', 'nld': '荷兰语', 'pol': '波兰语', 'por': '葡萄牙语',
    'rum': '罗马尼亚语', 'ron': '罗马尼亚语', 'tam': '泰米尔语',
    'tel': '泰卢固语', 'tha': '泰语', 'tur': '土耳其语', 'vie': '越南语',
    'rus': '俄语', 'kor': '韩语', 'ko': '韩语', 'und': '语言未标注',
}


class AudioTracks:
    CACHE_LIMIT = 8 * 1024 ** 3
    HEADROOM = 256 * 1024 ** 2

    def __init__(self, subtitles):
        self.tools = subtitles
        self.cache = subtitles.db.path.parent / 'cache' / 'audio-tracks'
        self.lock = threading.RLock()
        self.probes = {}
        self.jobs = {}
        self.process = None
        self.closed = False

    def probe(self, item):
        with self.lock:
            key = self.tools.fingerprint(item)
            if key in self.probes:
                return self.probes[key]
            raw = self.tools.run([self.tools.tool('ffprobe'), '-v', 'error',
                '-protocol_whitelist', 'file,pipe', '-show_entries',
                'stream=index,codec_type,codec_name,channels:stream_tags=language,title:'
                'stream_disposition=default:format=duration', '-of', 'json', item['path']], 25)
            data = json.loads(raw)
            duration = float(data.get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or not 0 < duration <= 86400:
                raise ValueError('无法核实媒体时长，请用本机播放器选择音轨')
            video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
            tracks = []
            for s in data.get('streams', []):
                if s.get('codec_type') != 'audio':
                    continue
                tags = s.get('tags', {})
                lang = str(tags.get('language', 'und')).lower()[:24]
                title = str(tags.get('title', ''))[:160]
                codec = str(s.get('codec_name', 'unknown'))[:32]
                channels = int(s.get('channels', 0))
                language = LANGUAGES.get(lang, lang)
                label = f'{language} · 音轨 {len(tracks) + 1}'
                if title:
                    label += f' · {title}'
                label += f' · {codec.upper()}' + (f' / {channels}声道' if channels else '')
                tracks.append({'index': int(s['index']), 'language': lang, 'label': label,
                    'codec': codec, 'channels': channels,
                    'default': bool(s.get('disposition', {}).get('default'))})
            result = {'tracks': tracks, 'duration': duration, 'video_codec': video.get('codec_name'),
                      'fingerprint': key, 'supported': video.get('codec_name') in ('h264', 'hevc', 'av1')}
            if len(self.probes) >= 200:
                self.probes.clear()
            self.probes[key] = result
            return result

    def identity(self, item, index):
        data = self.probe(item)
        if not any(t['index'] == index for t in data['tracks']):
            raise ValueError('该音轨不存在，请重新选择')
        return f'{data["fingerprint"]}-{index}', data

    def status(self, item, index):
        key, _ = self.identity(item, index)
        with self.lock:
            target = self.cache / f'{key}.mp4'
            if target.is_file():
                target.touch()
                return {'state': 'ready'}
            job = self.jobs.get(key, {'state': 'idle'})
            return dict(job if job['state'] != 'ready' else {'state': 'idle'})

    def prepare(self, item, index):
        key, data = self.identity(item, index)
        if not data['supported']:
            raise ValueError('这个视频编码暂不支持网页音轨切换，请用本机播放器')
        with self.lock:
            state = self.status(item, index)
            if state['state'] in ('ready', 'preparing'):
                return state
            if self.closed or any(j['state'] == 'preparing' for j in self.jobs.values()):
                raise ValueError('另一份音轨正在准备，请稍后重试')
            self.tools.tool('ffmpeg')
            self.cache.mkdir(parents=True, exist_ok=True)
            # Reserve enough for copied video and even a newly encoded 512 kb/s audio.
            reserve = item['size'] + int(data['duration'] * 64000) + 16 * 1024 ** 2
            if reserve > self.CACHE_LIMIT:
                raise ValueError('该文件超过音轨缓存上限，请用本机播放器')
            files = sorted(self.cache.glob('*.mp4'), key=lambda p: p.stat().st_mtime)
            used = sum(p.stat().st_size for p in files)
            for p in files:
                if used + reserve <= self.CACHE_LIMIT and shutil.disk_usage(self.cache).free >= reserve + self.HEADROOM:
                    break
                try:
                    size = p.stat().st_size
                    p.unlink()  # Only generated cache files; never the library media.
                    used -= size
                except OSError:
                    continue
            if used + reserve > self.CACHE_LIMIT or shutil.disk_usage(self.cache).free < reserve + self.HEADROOM:
                raise ValueError('音轨缓存空间不足，请用本机播放器')
            if len(self.jobs) >= 200:
                self.jobs.clear()
            self.jobs[key] = {'state': 'preparing'}
            threading.Thread(target=self.build, args=(dict(item), index, key, data),
                             daemon=True, name='audio-track-cache').start()
            return {'state': 'preparing'}

    def build(self, item, index, key, data):
        target = self.cache / f'{key}.mp4'
        partial = self.cache / f'{key}.part.mp4'
        track = next(t for t in data['tracks'] if t['index'] == index)
        process = None
        try:
            partial.unlink(missing_ok=True)
            args = [self.tools.tool('ffmpeg'), '-nostdin', '-v', 'error', '-n',
                    '-protocol_whitelist', 'file,pipe', '-i', item['path'],
                    '-map', '0:v:0', '-map', f'0:{index}', '-c:v', 'copy',
                    '-c:a', 'copy' if track['codec'] in ('aac', 'mp3') else 'aac']
            if track['codec'] not in ('aac', 'mp3'):
                args += ['-b:a', '256k' if track['channels'] <= 2 else '512k']
            if data['video_codec'] == 'hevc':
                args += ['-tag:v', 'hvc1']
            args += ['-map_metadata', '-1', '-map_chapters', '-1',
                     '-metadata:s:a:0', f'language={track["language"]}',
                     '-disposition:a:0', 'default', '-movflags', '+faststart', str(partial)]
            with self.lock:
                if self.closed:
                    raise ValueError()
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.process = process
            if process.wait(timeout=1200) != 0:
                raise ValueError()
            if data['fingerprint'] != self.tools.fingerprint(item):
                raise ValueError()
            if not partial.is_file() or not 0 < partial.stat().st_size <= self.CACHE_LIMIT:
                raise ValueError()
            raw = self.tools.run([self.tools.tool('ffprobe'), '-v', 'error', '-show_entries',
                'stream=codec_type:format=duration', '-of', 'json', str(partial)], 25)
            check = json.loads(raw)
            types = [s.get('codec_type') for s in check.get('streams', [])]
            if sorted(types) != ['audio', 'video'] or abs(float(check['format']['duration']) - data['duration']) > 2:
                raise ValueError()
            partial.replace(target)
            with self.lock:
                self.jobs[key] = {'state': 'ready'}
        except Exception:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            partial.unlink(missing_ok=True)
            with self.lock:
                self.jobs[key] = {'state': 'error', 'message': '音轨准备失败，请重试或用本机播放器'}
        finally:
            with self.lock:
                if self.process is process:
                    self.process = None

    def file(self, item, index):
        key, _ = self.identity(item, index)
        if self.status(item, index)['state'] != 'ready':
            raise ValueError('音轨尚未准备好，请先选择音轨')
        return self.cache / f'{key}.mp4'

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
