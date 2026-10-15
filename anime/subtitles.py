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

    def sidecars(self, item):
        """Only selected, completed, same-video files in the owned torrent manifest."""
        episode = self.db.one('SELECT * FROM episodes WHERE show_id=? AND episode=?',
                              (item.get('show_id'), item.get('episode')))
        if not episode or not episode.get('hash') or episode['path'] != item['path']:
            return []
        task = self.db.one("SELECT * FROM tasks WHERE hash=? AND show_id=? AND status='complete' AND purpose='episode'",
                           (episode['hash'], item.get('show_id')))
        if not task:
            return []
        try:
            from .releases import bdecode
            selection = json.loads(task['selection'])
            h, manifest = bdecode((self.db.path.parent / 'torrents' / (task['hash']+'.torrent')).read_bytes())
            if h != task['hash'] or manifest != selection['files']:
                return []
            video = selection['selected'][str(item['episode'])]
            root = Path(task['save_path']).resolve()
            media = (root / video['name']).resolve()
            if not media.is_relative_to(root) or media != Path(item['path']).resolve() or video['size'] != item['size']:
                return []
            result = []
            for f in manifest:
                p = (root / f['name']).resolve()
                suffix = p.suffix.lower()
                if f['index'] not in selection['ids'] or suffix not in ('.ass', '.ssa', '.srt'):
                    continue
                if p.parent != media.parent or not p.is_relative_to(root) or not p.stem.startswith(media.stem + '.'):
                    continue
                if not p.is_file() or p.stat().st_size != f['size'] or not 0 < f['size'] <= 8*1024*1024:
                    continue
                chinese = p.stem[len(media.stem)+1:].lower() in ('sc','chs','zh-hans','tc','cht','zh-hant')
                simplified = p.stem[len(media.stem)+1:].lower() in ('sc','chs','zh-hans')
                ai = 'AI翻译' in task['title']
                result.append({'index': -20000-f['index'], 'codec': 'ass' if suffix in ('.ass','.ssa') else 'subrip',
                    'language': 'chi' if chinese else 'und', 'label': ('简体中文' if simplified else '繁體中文' if chinese else '随包字幕') + (' · AI翻译' if ai else ''),
                    'supported': True, 'rank': 0 if simplified else 1 if chinese else 3,
                    'origin': 'sidecar', 'file_name': p.name, '_path': str(p)})
            return result
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def tracks(self, item):
        embedded = self.embedded_tracks(item)
        sidecars = [{k:v for k,v in t.items() if k != '_path'} for t in self.sidecars(item)]
        return sorted(embedded + sidecars, key=lambda t: (t['rank'], t['index']))

    def embedded_tracks(self, item):
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

    def extract(self, item, index, format='vtt'):
        tracks = self.tracks(item)
        if not any(t['index'] == index and t['supported'] for t in tracks):
            raise ValueError('该字幕轨道不可用；图形字幕请用弹弹play打开')
        if format not in ('vtt', 'ass') or (format == 'ass' and not any(
                t['index'] == index and t['codec'] in ('ass', 'ssa') for t in tracks)):
            raise ValueError('该轨道不支持原样式字幕')
        with self.lock:
            key = self.fingerprint(item)
            sidecar = next((t for t in self.sidecars(item) if t['index'] == index), None)
            if index <= -20000 and not sidecar:
                raise ValueError('随包字幕已变化，请重新打开')
            if sidecar:
                data = Path(sidecar['_path']).read_bytes()
                key += '-' + hashlib.sha256(data).hexdigest()
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache / f'{key}-{index}.{format}'
            if target.exists():
                return target
            # Pipe output prevents a failed extraction from leaving a partial cache file.
            data = data if sidecar and format == 'ass' else self.run([self.tool('ffmpeg'), '-nostdin', '-v', 'error', '-protocol_whitelist',
                             'file,pipe', '-i', sidecar['_path'] if sidecar else item['path'], '-map', '0:0' if sidecar else f'0:{index}',
                             '-c:s', 'copy' if format == 'ass' else 'webvtt',
                             '-f', 'ass' if format == 'ass' else 'webvtt', 'pipe:1'], 90)
            header = b'[Script Info]' if format == 'ass' else b'WEBVTT'
            if not data.lstrip(b'\xef\xbb\xbf\r\n ').startswith(header) or len(data) > 8 * 1024 * 1024:
                raise ValueError('字幕内容无效或过大，请用弹弹play打开')
            expected = self.fingerprint(item)
            if sidecar:
                current = next((t for t in self.sidecars(item) if t['index'] == index), None)
                if not current:raise ValueError('随包字幕已变化，请重新打开')
                expected += '-' + hashlib.sha256(Path(current['_path']).read_bytes()).hexdigest()
            if key != expected:
                raise ValueError('文件已变化，请重新打开')
            target.write_bytes(data)
            return target

    def fonts(self, item):
        # Only font attachments belonging to this registered media are exposed.
        self.fingerprint(item)
        raw = self.run([self.tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                        '-select_streams', 't', '-show_entries',
                        'stream=index,codec_name,extradata_size:stream_tags=filename,mimetype',
                        '-of', 'json', item['path']], 25)
        result = []
        total = 0
        for s in json.loads(raw).get('streams', []):
            size = s.get('extradata_size', 0)
            if s.get('codec_name') not in ('ttf', 'otf') or not 0 < size <= 32 * 1024 * 1024:
                continue
            total += size
            if len(result) >= 32 or total > 96 * 1024 * 1024:
                raise ValueError('内嵌字体过多，请用本机播放器保留完整样式')
            result.append({'index': s['index'], 'size': size})
        return result

    def extract_font(self, item, index):
        fonts = self.fonts(item)
        font = next((f for f in fonts if f['index'] == index), None)
        if font is None:
            raise ValueError('该字体附件不可用')
        with self.lock:
            key = self.fingerprint(item)
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache / f'{key}-font-{index}.font'
            if target.exists():
                return target
            # Never use a filename supplied by container metadata as an output path.
            data = self.run([self.tool('ffmpeg'), '-nostdin', '-v', 'error',
                             '-protocol_whitelist', 'file,pipe', f'-dump_attachment:{index}',
                             'pipe:1', '-i', item['path'], '-map', '0:v:0', '-c:v', 'copy',
                             '-frames:v', '0', '-an', '-sn', '-f', 'null', 'pipe:2'], 30)
            if len(data) != font['size'] or data[:4] not in (b'\x00\x01\x00\x00', b'OTTO', b'ttcf', b'true'):
                raise ValueError('字体附件无效')
            if key != self.fingerprint(item):
                raise ValueError('文件已变化，请重新打开')
            target.write_bytes(data)
            return target
