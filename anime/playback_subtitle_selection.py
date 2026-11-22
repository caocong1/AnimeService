"""Explicit playback-only subtitles, bound to one unchanged registered video.

This local registry never changes media, production jobs, accepted/default builds
or viewing records. Imported subtitles retain their publisher identity.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = 'playback-subtitle-selection/v1'
INDEX = -1000000000
LIMIT = 8 * 1024 * 1024


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def media_key(item):
    return _sha(str(item['path']).lower().encode())[:32]


def fingerprint(item):
    p = Path(item['path'])
    s = p.stat()
    if s.st_size != item['size']:
        raise ValueError('视频大小已变化，请重新核查')
    return _sha(f'{p}|{s.st_size}|{s.st_mtime_ns}'.encode())


def _root(db):
    return db.path.parent / 'playback-subtitles'


def _read(path, limit):
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError('已选中文字幕文件缺失或大小无效')
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('已选中文字幕读取期间发生变化')
    return raw


def _atomic(path, raw):
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_bytes(raw)
    temporary.replace(path)


def selected_sidecars(db, item):
    root = _root(db).resolve()
    manifest = root / 'selections' / (media_key(item) + '.json')
    if not manifest.exists():
        return []
    try:
        if not manifest.resolve().is_relative_to(root):
            raise ValueError('已选字幕登记路径无效')
        record = json.loads(_read(manifest, 32768))
        if record['schema'] != SCHEMA or record['media_id'] != media_key(item):
            raise ValueError('已选中文字幕登记无效')
        if record['fingerprint'] != fingerprint(item):
            return []  # A different video cannot inherit this selection.
        if record['show_id'] != item.get('show_id') or record['episode'] != item.get('episode'):
            raise ValueError('已选中文字幕的作品或集号不匹配')
        digest, suffix = record['subtitle_sha256'], record['suffix']
        if not re.fullmatch('[0-9a-f]{64}', digest) or suffix not in ('.ass', '.srt'):
            raise ValueError('已选中文字幕登记无效')
        source = root / 'objects' / (digest + suffix)
        if not source.resolve().is_relative_to(root):
            raise ValueError('已选中文字幕路径无效')
        if _sha(_read(source, LIMIT)) != digest:
            raise ValueError('已选中文字幕校验失败，请重新核查来源')
        return [{'index': INDEX, 'codec': 'ass' if suffix == '.ass' else 'subrip',
                 'language': 'chi', 'label': record['label'], 'supported': True,
                 'rank': -1, 'origin': 'selected', 'file_name': record['source_file'],
                 '_path': str(source)}]
    except (KeyError, TypeError, json.JSONDecodeError, OSError):
        raise ValueError('已选中文字幕登记或文件无法读取，请重新核查') from None


def register(db, item, source, *, expected_fingerprint, expected_subtitle_sha256,
             label, source_url, evidence, apply=False, expected_selection_sha256=None):
    """Plan first; explicit apply makes a content-addressed, auditable selection."""
    source = Path(source)
    suffix = source.suffix.lower()
    if suffix not in ('.ass', '.srt'):
        raise ValueError('仅支持已核实的 ASS 或 SRT 中文字幕')
    raw = _read(source, LIMIT)
    digest = _sha(raw)
    if digest != expected_subtitle_sha256 or fingerprint(item) != expected_fingerprint:
        raise ValueError('视频或字幕与预期 SHA 不符')
    text = raw.decode('utf-8-sig')
    timed = ('[Script Info]' in text and re.search(r'^Dialogue:', text, re.M)) if suffix == '.ass' else '-->' in text
    if not timed or len(re.findall(r'[\u4e00-\u9fff]', text)) < 10:
        raise ValueError('没有取得实际中文定时字幕')
    url = urlparse(source_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('须保留公开 HTTPS 字幕来源页')
    if not isinstance(label, str) or not label.strip() or len(label) > 120 or not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('须提供字幕来源名称及实际同集对轴核验记录')
    video = Path(item['path'])
    with video.open('rb') as handle:
        media_sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    if fingerprint(item) != expected_fingerprint:
        raise ValueError('核验期间视频已变化')
    record = {'schema': SCHEMA, 'media_id': media_key(item), 'fingerprint': expected_fingerprint,
              'media_sha256': media_sha, 'show_id': item.get('show_id'), 'episode': item.get('episode'),
              'subtitle_sha256': digest, 'suffix': suffix, 'label': label.strip(),
              'source_url': source_url, 'source_file': source.name, 'evidence': evidence}
    root = _root(db)
    manifest = root / 'selections' / (record['media_id'] + '.json')
    previous = _read(manifest, 32768) if manifest.exists() else None
    if previous:
        old = json.loads(previous)
        if all(old.get(k) == v for k, v in record.items()):
            return {'state': 'already_selected', 'selection': old}
        if _sha(previous) != expected_selection_sha256:
            raise ValueError('已有另一字幕选择，须明确提供其 SHA 才能替换')
    plan = {'state': 'planned', 'selection': record}
    if not apply:
        return plan
    root.mkdir(parents=True, exist_ok=True)
    lock = root / 'selection.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.close(fd)
        current = _read(manifest, 32768) if manifest.exists() else None
        if current != previous or fingerprint(item) != expected_fingerprint or _read(source, LIMIT) != raw:
            raise ValueError('应用前视频、字幕或当前选择已变化')
        (root / 'objects').mkdir(exist_ok=True)
        manifest.parent.mkdir(exist_ok=True)
        archive = root / 'history'
        archive.mkdir(exist_ok=True)
        target = root / 'objects' / (digest + suffix)
        if target.exists():
            if _read(target, LIMIT) != raw:
                raise ValueError('同 SHA 字幕档案冲突')
        else:
            _atomic(target, raw)
        if previous:
            prior = archive / (_sha(previous) + '.json')
            if not prior.exists():
                _atomic(prior, previous)
        record['selected_at'] = time.time()
        data = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode()
        history = archive / (_sha(data) + '.json')
        if not history.exists():
            _atomic(history, data)
        _atomic(manifest, data)
        return {'state': 'selected', 'selection': record}
    finally:
        lock.unlink()
