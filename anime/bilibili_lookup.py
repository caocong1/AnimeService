"""Resolve public Bilibili video IDs without relying on the search index."""
import copy
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import requests
from fastapi import HTTPException


def video_reference(value):
    value = value.strip()
    if re.fullmatch(r'BV[0-9A-Za-z]{10}', value, re.I):
        return 'BV' + value[2:], 1
    try:
        u = urlparse(value if '://' in value else 'https://' + value)
        if (u.scheme not in ('http', 'https') or
                u.hostname not in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com') or
                u.username or u.password or u.port):
            raise ValueError
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10})/?', u.path, re.I)
        if not match:
            raise ValueError
        parts = parse_qs(u.query, keep_blank_values=True).get('p', ['1'])
        if len(parts) != 1 or not re.fullmatch(r'[1-9][0-9]{0,4}', parts[0]):
            raise ValueError
        return 'BV' + match[1][2:], int(parts[0])
    except ValueError:
        raise HTTPException(400, '请输入有效的 B站视频网址或 BV号；分P参数 p 必须是正整数') from None


def resolve_video(value):
    bvid, part = video_reference(value)
    try:
        response = requests.get('https://api.bilibili.com/x/web-interface/view',
                                params={'bvid': bvid},
                                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
                                timeout=(3, 20), allow_redirects=False)
    except requests.Timeout:
        raise HTTPException(504, 'B站视频查询超时，请稍后重试') from None
    except requests.RequestException:
        raise HTTPException(503, '暂时无法连接 B站，请稍后重试') from None
    if response.status_code == 404:
        raise HTTPException(404, 'B站视频不存在或已删除')
    if not response.ok or response.is_redirect:
        raise HTTPException(502, 'B站视频接口暂时不可用，请稍后重试')
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        if result.get('code') in (-404, 62002):
            raise HTTPException(404, 'B站视频不存在或已删除')
        if result.get('code') != 0:
            raise HTTPException(502, 'B站未返回视频信息，可能受访问限制，请稍后重试')
        data = result['data']
        if data['bvid'] != bvid:
            raise ValueError
        title = data['title']
        pages = data['pages']
        if not isinstance(title, str) or not title.strip() or not isinstance(pages, list) or not pages:
            raise ValueError
        episodes = []
        for page in pages:
            n, cid, duration = int(page['page']), int(page['cid']), int(page['duration'])
            if n <= 0 or cid <= 0 or duration < 0:
                raise ValueError
            url = f'https://www.bilibili.com/video/{bvid}?p={n}'
            episodes.append({'episodeTitle': f'【bilibili1】 P{n} · {page.get("part", "")} · {duration // 60}:{duration % 60:02d}',
                             'episodeNumber': '', 'url': url, 'duration': duration, 'cid': cid})
        if len({ep['url'] for ep in episodes}) != len(episodes):
            raise ValueError
        preferred = f'https://www.bilibili.com/video/{bvid}?p={part}'
        if not any(ep['url'] == preferred for ep in episodes):
            raise HTTPException(400, '这个视频没有指定的分P，请核对网址中的 p 参数')
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(502, 'B站视频信息格式异常，请稍后重试') from None
    owner = data.get('owner') or {}
    uploader = owner.get('name', '') if isinstance(owner, dict) else ''
    uploader = uploader.strip()[:100] if isinstance(uploader, str) else ''
    return {'bangumi': {'animeTitle': title, 'source': 'bilibili', 'site': 'bilibili', 'uploader': uploader,
                        'type': 'B站视频·需核对版本', 'episodeCount': len(episodes),
                        'preferred_url': preferred, 'episodes': episodes}}


def danmaku_count(body):
    """Read DmWebViewReply.count (field 8), skipping unrelated protobuf fields.

    An omitted count is protobuf's zero only in a recognizable, open reply.
    Invalid/closed replies remain unknown, never a fabricated zero.
    """
    if not isinstance(body, bytes) or not body or len(body) > 262144:
        raise ValueError('invalid metadata')
    pos = 0
    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(body):raise ValueError('truncated metadata')
            byte = body[pos]; pos += 1
            value |= (byte & 127) << shift
            if byte < 128:return value
        raise ValueError('invalid varint')
    count, state, recognized = None, 0, False
    while pos < len(body):
        tag = varint(); field, wire = tag >> 3, tag & 7
        if field == 0:raise ValueError('invalid field')
        if wire == 0:
            value = varint()
            if field == 8:
                if value > 2**31 - 1:raise ValueError('invalid count')
                count = value; recognized = True
            elif field == 1:state = value
        elif wire == 2:
            length = varint()
            pos += length
            if field == 4:recognized = True
        elif wire in (1, 5):pos += 8 if wire == 1 else 4
        else:raise ValueError('invalid wire type')
        if pos > len(body):raise ValueError('truncated metadata')
    if not recognized or state != 0:raise ValueError('unavailable metadata')
    return count if count is not None else 0


class BilibiliMetadata:
    """Small read-only metadata cache. One lookup at a time, no comment downloads."""
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    def _cached(self, key, fetch):
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            if cached[2]:raise ValueError('metadata temporarily unavailable')
            return copy.deepcopy(cached[1])
        self.cache = {k:v for k,v in self.cache.items() if v[0] > time.monotonic()}
        if len(self.cache) >= 256:self.cache.pop(next(iter(self.cache)))
        try:value = fetch()
        except (HTTPException, requests.RequestException, ValueError, KeyError, TypeError):
            if key[0] == 'count':self.cache[key] = (time.monotonic() + 30, None, True)
            raise
        self.cache[key] = (time.monotonic() + 600, copy.deepcopy(value), False)
        return value

    def _video(self, value):
        bvid, part = video_reference(value)
        result = self._cached(('video', bvid), lambda: resolve_video(bvid))
        preferred = f'https://www.bilibili.com/video/{bvid}?p={part}'
        if not any(ep['url'] == preferred for ep in result['bangumi']['episodes']):
            raise HTTPException(400, '这个视频没有指定的分P，请核对网址中的 p 参数')
        result['bangumi']['preferred_url'] = preferred
        return result

    def video(self, value):
        with self.lock:return self._video(value)

    def source_info(self, url):
        with self.lock:
            info = {'uploader': '', 'count': None}
            try:
                match = re.fullmatch(r'https://www\.bilibili\.com/bangumi/play/ep([1-9][0-9]*)', url)
                if match:
                    epid = int(match[1])
                    def fetch_episode_cid():
                        r = requests.get('https://api.bilibili.com/pgc/view/web/season',
                            params={'ep_id': epid}, timeout=(3, 8), allow_redirects=False,
                            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
                        if not r.ok or r.is_redirect:raise ValueError('metadata unavailable')
                        d = r.json()
                        if d.get('code') != 0:raise ValueError('metadata unavailable')
                        result = d['result']
                        episodes = list(result.get('episodes', []))
                        for section in result.get('section', []):episodes.extend(section.get('episodes', []))
                        cid = next(int(ep['cid']) for ep in episodes if int(ep['id']) == epid)
                        if cid <= 0:raise ValueError('invalid cid')
                        return cid
                    cid = self._cached(('episode', epid), fetch_episode_cid)
                else:
                    b = self._video(url)['bangumi']
                    info['uploader'] = b['uploader']
                    ep = next(ep for ep in b['episodes'] if ep['url'] == b['preferred_url'])
                    cid = ep['cid']
                def fetch_count():
                    r = requests.get('https://api.bilibili.com/x/v2/dm/web/view',
                        params={'type': 1, 'oid': cid}, timeout=(3, 8), allow_redirects=False,
                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
                    if not r.ok or r.is_redirect:raise ValueError('metadata unavailable')
                    return danmaku_count(r.content)
                info['count'] = self._cached(('count', cid), fetch_count)
            except (HTTPException, requests.RequestException, ValueError, KeyError, TypeError, AttributeError, StopIteration):
                info['message'] = '数量暂不可用，可添加后查看实际加载条数'
            return info
