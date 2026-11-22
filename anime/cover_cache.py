"""Persistent, versioned display-size covers; no remote image work in page APIs."""
import hashlib
import io
import re
import stat
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageOps

WIDTHS = (160, 480)
MAX_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 12_000_000
IMAGE_PATH = re.compile(r'(?:r/[1-9][0-9]{0,3}/)?pic/cover/[a-z]/[a-f0-9]{2}/[a-f0-9]{2}/[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png|webp|gif)')
CACHE_CONTROL = 'private, max-age=31536000, immutable'


def checked_url(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.netloc != 'lain.bgm.tv' or p.query or p.fragment
            or not IMAGE_PATH.fullmatch(p.path[1:])):
        raise ValueError('仅支持公开 Bangumi 封面地址')
    return url


def local_url(url, width=480):
    try:
        checked_url(url)
        if width not in WIDTHS:
            return ''
        return f'/covers/v1/{width}' + urlsplit(url).path
    except (ValueError, TypeError):
        return ''


class CoverUnavailable(RuntimeError):
    pass


class CoverCache:
    def __init__(self, root, *, fetch=None):
        self.root = Path(root)
        self.fetch = fetch or self._download
        self.guard = threading.Lock()
        self.locks = {}
        self.failed_until = {}
        self.http = threading.local()
        self.last_warm = {'state': 'not_started'}

    def _download(self, url):
        if not hasattr(self.http, 'session'):
            self.http.session = requests.Session()
            self.http.session.headers['User-Agent'] = 'sorawatcher/AnimeService/2.0 (cover cache)'
        target, started = url, time.monotonic()
        for _ in range(3):
            checked_url(target)
            with self.http.session.get(target, stream=True, allow_redirects=False, timeout=(3, 5)) as response:
                if response.is_redirect:
                    target = checked_url(urljoin(target, response.headers.get('Location', '')))
                    continue
                response.raise_for_status()
                length = response.headers.get('Content-Length', '')
                if length.isdigit() and int(length) > MAX_BYTES:
                    raise CoverUnavailable('封面文件过大')
                parts, size = [], 0
                for block in response.iter_content(65536):
                    size += len(block)
                    if size > MAX_BYTES or time.monotonic() - started > 12:
                        raise CoverUnavailable('封面获取超出限制')
                    parts.append(block)
                return b''.join(parts)
        raise CoverUnavailable('封面重定向次数过多')

    def path(self, url, width):
        checked_url(url)
        if width not in WIDTHS:
            raise ValueError('不支持的封面尺寸')
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.root / f'{key}-v1-{width}.webp'

    @staticmethod
    def valid(path):
        try:
            info = path.stat()
            return stat.S_ISREG(info.st_mode) and info.st_size > 12
        except OSError:
            return False

    def _render(self, url, data):
        if len(data) > MAX_BYTES:
            raise CoverUnavailable('封面文件过大')
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in ('JPEG', 'PNG', 'WEBP', 'GIF') or original.width * original.height > MAX_PIXELS:
                raise CoverUnavailable('封面格式或尺寸无效')
            image = ImageOps.exif_transpose(original).convert('RGBA' if 'A' in original.getbands() or 'transparency' in original.info else 'RGB')
            self.root.mkdir(parents=True, exist_ok=True)
            for width in WIDTHS:
                thumb = image.copy()
                thumb.thumbnail((width, width * 2), Image.Resampling.LANCZOS, reducing_gap=3)
                target = self.path(url, width)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.root, suffix='.tmp', delete=False) as f:
                        temporary = Path(f.name)
                        thumb.save(f, 'WEBP', quality=84, method=4)
                    temporary.replace(target)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)

    def get(self, url, width=480):
        target = self.path(url, width)
        if self.valid(target):
            return target
        with self.guard:
            lock = self.locks.setdefault(url, threading.Lock())
        with lock:
            if self.valid(target):
                return target
            if self.failed_until.get(url, 0) > time.monotonic():
                raise CoverUnavailable('封面暂时无法获取，稍后自动重试')
            try:
                self._render(url, self.fetch(url))
            except Exception as error:
                self.failed_until[url] = time.monotonic() + 60
                raise CoverUnavailable('封面暂时无法获取') from error
            self.failed_until.pop(url, None)
            return target

    def warm(self, urls, *, workers=4, stop=None):
        urls = list(dict.fromkeys(urls))

        def one(url):
            if stop is not None and stop.is_set():
                return 'stopped'
            try:
                cached = all(self.valid(self.path(url, width)) for width in WIDTHS)
                for width in WIDTHS:
                    self.get(url, width)
                return 'cached' if cached else 'downloaded'
            except (ValueError, CoverUnavailable):
                return 'failed'

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cover-cache') as pool:
            outcomes = list(pool.map(one, urls))
        return {state: outcomes.count(state) for state in ('cached', 'downloaded', 'failed', 'stopped')}

    def maintain(self, db, stop):
        while not stop.is_set():
            try:
                # Selected shows get the first slots. New catalog images are
                # warmed outside all page/API requests and persist over restarts.
                urls = [r['image'] for r in db.rows("SELECT image FROM shows WHERE image!='' ORDER BY selected DESC,updated DESC,id")]
                self.last_warm = {'state': 'complete', 'checked_at': time.time(), **self.warm(urls, stop=stop)}
            except Exception as error:
                self.last_warm = {'state': 'error', 'checked_at': time.time(), 'error': type(error).__name__}
            stop.wait(300)


def register_covers(app, cache):
    from fastapi import HTTPException, Request
    from fastapi.responses import FileResponse, Response

    @app.api_route('/covers/v1/{width}/{image_path:path}', methods=['GET', 'HEAD'])
    def cover_image(width: int, image_path: str, request: Request):
        try:
            path = cache.get('https://lain.bgm.tv/' + image_path, width)
        except ValueError:
            raise HTTPException(404, '没有此封面') from None
        except CoverUnavailable:
            raise HTTPException(503, '封面暂时无法获取', headers={'Cache-Control': 'no-store'}) from None
        response = FileResponse(path, media_type='image/webp', headers={'Cache-Control': CACHE_CONTROL}, stat_result=path.stat())
        etag = response.headers['etag']
        if etag in (value.strip().removeprefix('W/') for value in request.headers.get('if-none-match', '').split(',')):
            return Response(status_code=304, headers={'ETag': etag, 'Cache-Control': CACHE_CONTROL})
        return response
