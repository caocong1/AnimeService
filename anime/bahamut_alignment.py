"""Bounded Bahamut HLS reference samples. Never persists cookies, URLs or audio."""
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from .audio_alignment import MAX_BYTES, Unavailable, validate_public_host

LIMIT = 8 * 1024 * 1024
_limited_until = 0
_limit_lock = threading.Lock()


def validate_url(url, api=False, proxy_dns=False):
    try:
        u = urlsplit(url)
        host = u.hostname or ''
        allowed = host in ('ani.gamer.com.tw', 'api.gamer.com.tw') if api else (
            host == 'bahamut.akamaized.net' or
            bool(re.fullmatch(r'gamer(?:-cds\d*)?\.cdn\.hinet\.net', host)))
        if (not allowed or u.scheme != 'https' or u.username or u.password
                or u.port not in (None, 443) or u.fragment):
            raise Unavailable('bahamut_playlist')
        if proxy_dns: validate_public_host(host, allow_proxy_dns=True)
        else: validate_public_host(host)
    except (ValueError, OSError):
        raise Unavailable('bahamut_playlist') from None


def configured_proxy():
    value = os.environ.get('ANIMESERVICE_BAHAMUT_PROXY', '')
    if not value:
        config = Path(__file__).resolve().parents[1] / 'vendor/danmuapi/config/.env'
        if config.is_file():
            for line in config.read_text(encoding='utf-8').splitlines():
                if line.startswith('ANIMESERVICE_BAHAMUT_PROXY='):
                    value = line.split('=', 1)[1].strip(); break
    if not value:
        return None
    try:
        u = urlsplit(value)
        if (u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1')
                or not u.port or u.username or u.password or u.path not in ('', '/')
                or u.query or u.fragment):
            raise ValueError()
    except ValueError:
        raise Unavailable('bahamut_proxy') from None
    return value


def attributes(line):
    return {k: quoted if quoted else bare for k, quoted, bare in
            re.findall(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line.split(':', 1)[1])}


def parse_playlist(text, base, proxy_dns=False):
    """Only finite MPEG-TS VOD; unsupported encryption/timelines never guessed."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U':
        raise Unavailable('bahamut_playlist')
    variants, segments = [], []
    variant = duration = None
    key = None
    sequence = 0
    at = 0.0
    discontinuity = 0
    for line in lines[1:]:
        if line.startswith('#EXT-X-STREAM-INF:'):
            variant = attributes(line)
        elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
            if not 0 <= sequence < 2**128: raise Unavailable('bahamut_playlist')
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',', 1)[0])
            if not math.isfinite(duration) or not 0 < duration <= 60:
                raise Unavailable('bahamut_playlist')
        elif line.startswith('#EXT-X-KEY:'):
            info = attributes(line)
            if info.get('METHOD') == 'NONE': key = None
            elif (info.get('METHOD') != 'AES-128' or not info.get('URI')
                    or info.get('KEYFORMAT', 'identity') != 'identity'):
                raise Unavailable('bahamut_playlist')
            else:
                iv = info.get('IV')
                if iv and not re.fullmatch(r'0x[0-9a-fA-F]{1,32}', iv):
                    raise Unavailable('bahamut_playlist')
                key = {'url':urljoin(base, info['URI']), 'iv':int(iv, 16) if iv else None}
        elif line == '#EXT-X-DISCONTINUITY':
            discontinuity += 1
        elif line.startswith(('#EXT-X-BYTERANGE:', '#EXT-X-MAP:', '#EXT-X-GAP')):
            raise Unavailable('bahamut_playlist')
        elif not line.startswith('#'):
            url = urljoin(base, line)
            validate_url(url, proxy_dns=proxy_dns)
            if variant is not None:
                variants.append((int(variant.get('BANDWIDTH') or 2**31), url)); variant = None
            elif duration is not None:
                segments.append({'url':url,'start':at,'duration':duration,'key':key,
                                 'sequence':sequence,'discontinuity':discontinuity})
                at += duration; duration = None; sequence += 1
            else: raise Unavailable('bahamut_playlist')
    if variants:
        if segments: raise Unavailable('bahamut_playlist')
        return {'variant':min(variants)[1]}
    if '#EXT-X-ENDLIST' not in lines or not segments or not 240 <= at <= 3600:
        raise Unavailable('bahamut_playlist')
    return {'segments':segments,'duration':at}


class BahamutReference:
    def __init__(self, url, check):
        self.url, self.check = url, check
        self.session = requests.Session(); self.session.trust_env = False
        proxy = configured_proxy()
        self.proxy_dns = bool(proxy)
        if proxy: self.session.proxies = {'https':proxy}
        self.session.headers.update({'User-Agent':'Mozilla/5.0', 'Referer':url,
                                     'Origin':'https://ani.gamer.com.tw'})
        self.downloaded = 0
        self.keys = {}

    def close(self):
        self.keys.clear(); self.session.cookies.clear(); self.session.close()

    def read(self, url, cap=256*1024, api=False):
        for _ in range(4):
            self.check(); validate_url(url, api, proxy_dns=self.proxy_dns)
            with self.session.get(url, timeout=(3, 8), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('Location', '')); continue
                if not 200 <= response.status_code < 300:
                    raise Unavailable('bahamut_access' if response.status_code in (401,403,451) else 'source_info')
                body = bytearray()
                for chunk in response.iter_content(8192):
                    self.check()
                    self.downloaded += len(chunk); body.extend(chunk)
                    if len(body) > cap or self.downloaded > min(LIMIT, MAX_BYTES):
                        raise Unavailable('budget')
                return bytes(body)
        raise Unavailable('bahamut_playlist')

    def api(self, url):
        global _limited_until
        try: data = json.loads(self.read(url, 64*1024, api=True))
        except (ValueError, TypeError): raise Unavailable('source_info') from None
        if not isinstance(data, dict): raise Unavailable('source_info')
        if data.get('error'):
            error = data['error']
            if isinstance(error, dict) and error.get('code') == 1018:
                with _limit_lock: _limited_until = time.monotonic()+1800
                raise Unavailable('bahamut_limited')
            raise Unavailable('bahamut_access')
        return data

    def prepare(self):
        with _limit_lock:
            if time.monotonic() < _limited_until: raise Unavailable('bahamut_limited')
        sn = parse_qs(urlsplit(self.url).query)['sn'][0]
        device = self.api('https://ani.gamer.com.tw/ajax/getdeviceid.php').get('deviceid')
        if not isinstance(device, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', device):
            raise Unavailable('source_info')
        grant = self.api('https://ani.gamer.com.tw/ajax/token.php?sn=' + sn + '&device=' + device)
        if grant.get('time') != 1 and grant.get('vip') is not True:
            raise Unavailable('bahamut_ad')
        # Public anonymous video-source API. No account imports or ad/access bypass.
        data = self.api('https://api.gamer.com.tw/anime/v1/video_src.php?videoSn=' + sn +
                        '&deviceid=' + device + '&deviceTypeUseCases=1').get('data', {})
        try: playlist = data['srcUseCases'][0]['src']['playlist']
        except (KeyError, IndexError, TypeError): raise Unavailable('bahamut_access') from None
        for _ in range(3):
            parsed = parse_playlist(self.read(playlist).decode('utf-8-sig'), playlist, self.proxy_dns)
            if 'variant' not in parsed:
                self.segments, self.duration = parsed['segments'], parsed['duration']; return self
            playlist = parsed['variant']
        raise Unavailable('bahamut_playlist')

    def sample(self, requested):
        first = next((i for i, s in enumerate(self.segments) if s['start'] >= requested), None)
        if first is None: raise Unavailable('short_source')
        selected, length = [], 0
        for segment in self.segments[first:]:
            if segment['discontinuity'] != self.segments[first]['discontinuity']:
                raise Unavailable('bahamut_playlist')
            selected.append(segment); length += segment['duration']
            if length >= 36: break
        if length < 35: raise Unavailable('short_source')
        chunks = []
        for segment in selected:
            raw = self.read(segment['url'], 2*1024*1024)
            key = segment['key']
            if key:
                from yt_dlp.aes import aes_cbc_decrypt_bytes
                if key['url'] not in self.keys:
                    self.keys[key['url']] = self.read(key['url'], 16)
                secret = self.keys[key['url']]
                if len(secret) != 16 or not raw or len(raw) % 16:
                    raise Unavailable('bahamut_playlist')
                iv = (key['iv'] if key['iv'] is not None else segment['sequence']).to_bytes(16, 'big')
                raw = aes_cbc_decrypt_bytes(raw, secret, iv)
                pad = raw[-1]
                if not 1 <= pad <= 16 or raw[-pad:] != bytes([pad])*pad:
                    raise Unavailable('bahamut_playlist')
                raw = raw[:-pad]
            chunks.append(raw)
        return b''.join(chunks), self.segments[first]['start']
