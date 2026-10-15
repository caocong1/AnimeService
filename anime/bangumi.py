"""Documented public v0 API only; mutations are reconciled before every retry."""
import time
from urllib.parse import quote
import requests

USER_AGENT = 'sorawatcher/AnimeService/2.0 (personal anime tracker)'


class BangumiError(RuntimeError):
    def __init__(self, message, status=0, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class BangumiClient:
    def __init__(self, token='', session=None):
        self.token = token
        self.session = session or requests.Session()
        self.last_request = 0

    def request(self, method, path, **kwargs):
        if not path.startswith('/v0/') or '?' in path or '://' in path:
            raise ValueError('无效 Bangumi API 路径')
        time.sleep(max(0, .3 - (time.monotonic() - self.last_request)))
        headers = {'User-Agent': USER_AGENT}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        self.last_request = time.monotonic()
        try:
            r = self.session.request(method, 'https://api.bgm.tv' + path, headers=headers,
                                     timeout=(5, 15), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise BangumiError('Bangumi 网络暂时不可用；保留队列，稍后先回读再重试') from None
        accepted = (r.status_code == 202 and method in ('POST', 'PATCH', 'PUT')
                    and path.startswith('/v0/users/-/collections/'))
        if r.status_code not in (200, 204) and not accepted:
            try:
                retry_after = min(86400, max(30, int(r.headers.get('Retry-After', '60'))))
            except ValueError:
                retry_after = 60
            messages = {401: 'Bangumi 授权已失效，请在本机重新连接账号',
                        403: 'Bangumi 拒绝访问，请检查授权权限',
                        404: 'Bangumi 条目或收藏不存在',
                        429: 'Bangumi 请求过于频繁，已延后重试'}
            raise BangumiError(messages.get(r.status_code, f'Bangumi HTTP {r.status_code}，稍后回读重试'),
                               r.status_code, retry_after if r.status_code == 429 else 0)
        # Production collection writes can return 202. This acknowledges receipt;
        # BangumiSync still verifies the remote values before recording success.
        if r.status_code == 204 or accepted:
            return None
        try:
            return r.json()
        except ValueError:
            raise BangumiError('Bangumi 响应格式无效，保留队列') from None

    def pages(self, path, params=None):
        offset = 0
        seen = set()
        items = []
        expected = 0
        while offset <= 10000:
            page = self.request('GET', path, params={**(params or {}), 'limit': 100, 'offset': offset})
            rows, total = page.get('data'), page.get('total')
            if not isinstance(rows, list) or type(total) is not int or total < 0 or page.get('offset', offset) != offset:
                raise BangumiError('Bangumi 分页响应无效，未使用不完整章节')
            expected = max(expected, total)
            for row in rows:
                identity = row.get('id') or row.get('episode', {}).get('id')
                if type(identity) is not int or identity <= 0 or identity in seen:
                    raise BangumiError('Bangumi 章节分页重复或缺少 ID，未使用不完整章节')
                seen.add(identity)
            items.extend(rows)
            offset += len(rows)
            if offset >= expected:
                return items
            if not rows:
                raise BangumiError('Bangumi 章节分页提前结束，未使用不完整章节')
        raise BangumiError('Bangumi 章节超过分页保护范围')

    def me(self):
        return self.request('GET', '/v0/me')

    def collection(self, username, sid):
        try:
            return self.request('GET', f'/v0/users/{quote(str(username), safe="")}/collections/{int(sid)}')
        except BangumiError as e:
            if e.status == 404:
                return None
            raise

    def episodes(self, sid):
        return self.pages('/v0/episodes', {'subject_id': sid, 'type': 0})

    def episode_collections(self, sid):
        return self.pages(f'/v0/users/-/collections/{sid}/episodes', {'episode_type': 0})

    def write_collection(self, sid, body, exists):
        return self.request('PATCH' if exists else 'POST', f'/v0/users/-/collections/{sid}', json=body)

    def write_episodes(self, sid, ids, state):
        return self.request('PATCH', f'/v0/users/-/collections/{sid}/episodes',
                            json={'episode_id': ids, 'type': state})


def episode_map(db, sid, episodes):
    """Local episodes -> stable Bangumi IDs. Never use release/file numbering offsets."""
    mapping = db.mapping(sid)
    explicit = mapping.get('airing_offset')
    verified_shift = db.get('airing_sort_offset_' + str(sid))
    regular = [r for r in episodes if r.get('type') == 0]
    # Audited legacy watch numbering belongs to sync alone. Keep the original
    # watches and downloader mapping intact, and fail closed if the API changes.
    proof = db.get('bangumi_sync_numbering_' + str(sid))
    basis = None
    if proof:
        anchor = proof.get('anchor', {}) if isinstance(proof, dict) else {}
        if (not isinstance(proof, dict) or proof.get('subject_id') != sid
                or proof.get('basis') not in ('ep', 'sort')
                or type(proof.get('sort_offset')) is not int
                or not isinstance(anchor, dict)
                or any(type(anchor.get(k)) is not int for k in ('id', 'sort', 'ep'))
                or not isinstance(proof.get('evidence'), list) or not proof['evidence']
                or not all(isinstance(url, str) and url.startswith('https://') for url in proof['evidence'])
                or not regular or not any(all(row.get(k) == anchor.get(k) for k in ('id', 'sort', 'ep'))
                                          for row in regular)
                or any(row.get('subject_id') != sid
                       or type(row.get('ep')) is not int or row['ep'] <= 0
                       or type(row.get('sort')) is not int
                       or row['sort'] - row['ep'] != proof['sort_offset'] for row in regular)):
            raise ValueError('已核验的 Bangumi 同步编号证据不匹配，等待重新核验')
        basis = proof['basis']
    sequential_sort = sorted(r.get('sort', 0) for r in regular) == list(range(1, len(regular) + 1))
    result = {}
    for row in episodes:
        if row.get('type') != 0:
            continue
        if row.get('subject_id', sid) != sid:
            raise ValueError('Bangumi 章节归属不匹配，等待核验')
        if basis:
            number = row[basis]
        elif explicit is not None:
            number = row.get('sort', 0) - explicit
        else:
            number = row.get('ep')
            if number is None or number == 0:
                if not verified_shift and not sequential_sort:
                    raise ValueError('Bangumi 章节缺少季内编号，等待跨季编号核验')
                number = row.get('sort', 0) - (verified_shift or 0)
        if not isinstance(number, (int, float)) or not float(number).is_integer() or not 1 <= number <= 999:
            continue
        number = int(number)
        identity = row.get('id')
        if type(identity) is not int or identity <= 0 or number in result:
            raise ValueError('Bangumi 章节编号含糊或重复，等待核验')
        result[number] = identity
    if len(set(result.values())) != len(result):
        raise ValueError('Bangumi 章节 ID 重复，等待核验')
    return result
