"""Resolve public Bilibili video IDs without relying on the search index."""
import re
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
                             'episodeNumber': '', 'url': url, 'duration': duration})
        if len({ep['url'] for ep in episodes}) != len(episodes):
            raise ValueError
        preferred = f'https://www.bilibili.com/video/{bvid}?p={part}'
        if not any(ep['url'] == preferred for ep in episodes):
            raise HTTPException(400, '这个视频没有指定的分P，请核对网址中的 p 参数')
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(502, 'B站视频信息格式异常，请稍后重试') from None
    return {'bangumi': {'animeTitle': title, 'source': 'bilibili', 'site': 'bilibili',
                        'type': 'B站视频·需核对版本', 'episodeCount': len(episodes),
                        'preferred_url': preferred, 'episodes': episodes}}
