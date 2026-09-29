"""Extract text subtitles from registered local media; never modify the media."""
import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path


class Subtitles:
    TEXT_CODECS = {'subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.probes = {}
        self.cache = db.path.parent / 'cache' / 'subtitles'

    def tool(self, name):
        bundled = Path(self.db.get('dandan_exe')).parent / 'ffmpeg' / (name + '.exe')
        found = str(bundled) if bundled.is_file() else shutil.which(name)
        if not found:
            raise ValueError('未找到字幕读取工具，请用弹弹play打开')
        return found

    def fingerprint(self, item):
        p = Path(item['path'])
        stat = p.stat()
        if stat.st_size != item['size']:
            raise ValueError('文件大小已变化，请重新核查')
        return hashlib.sha256(f'{p}|{stat.st_size}|{stat.st_mtime_ns}'.encode()).hexdigest()

    def run(self, args, timeout):
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('字幕读取超时或工具不可用，请重试') from None
        if result.returncode:
            # Tool stderr can contain local paths: never return it to clients.
            raise ValueError('无法读取这份文件的字幕，请用弹弹play打开')
        return result.stdout

    def tracks(self, item):
        with self.lock:
            key = self.fingerprint(item)
            if key in self.probes:
                return self.probes[key]
            raw = self.run([self.tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                            '-select_streams', 's', '-show_entries',
                            'stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced',
                            '-of', 'json', item['path']], 25)
            result = []
            for stream in json.loads(raw).get('streams', []):
                tags = stream.get('tags', {})
                title, language = tags.get('title', ''), tags.get('language', 'und')
                hint = (title + ' ' + language).lower()
                simplified = any(x in hint for x in ('simplified', '简', '簡', 'chs', 'zh-hans'))
                traditional = any(x in hint for x in ('traditional', '繁', 'cht', 'zh-hant'))
                label = '简体中文' if simplified else '繁體中文' if traditional else title or language
                supported = stream['codec_name'] in self.TEXT_CODECS
                chinese = simplified or traditional or language.lower() in ('chi', 'zho', 'zh')
                result.append({'index': stream['index'], 'codec': stream['codec_name'],
                               'label': label, 'language': language, 'supported': supported,
                               'rank': 0 if simplified else 1 if chinese else 2 if stream.get('disposition', {}).get('default') else 3})
            result.sort(key=lambda x: (x['rank'], x['index']))
            if len(self.probes) >= 200:
                self.probes.clear()
            self.probes[key] = result
            return result

    def extract(self, item, index):
        tracks = self.tracks(item)
        if not any(t['index'] == index and t['supported'] for t in tracks):
            raise ValueError('该字幕轨道不可用；图形字幕请用弹弹play打开')
        with self.lock:
            key = self.fingerprint(item)
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache / f'{key}-{index}.vtt'
            if target.exists():
                return target
            # Pipe output prevents a failed extraction from leaving a partial cache file.
            data = self.run([self.tool('ffmpeg'), '-nostdin', '-v', 'error', '-protocol_whitelist',
                             'file,pipe', '-i', item['path'], '-map', f'0:{index}', '-c:s', 'webvtt',
                             '-f', 'webvtt', 'pipe:1'], 90)
            if not data.startswith(b'WEBVTT') or len(data) > 8 * 1024 * 1024:
                raise ValueError('字幕内容无效或过大，请用弹弹play打开')
            if key != self.fingerprint(item):
                raise ValueError('文件已变化，请重新打开')
            target.write_bytes(data)
            return target
