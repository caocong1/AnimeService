import re,time
from urllib.parse import urlparse
import requests

HOSTS={'api.bgm.tv','bgm.tv','bangumi.tv','mikanani.me','mikanime.tv','mikanani.hacgn.fun',
       'share.dmhy.org','dl.dmhy.org'}
class NetworkError(RuntimeError):pass

def safe_error(e):
    # Never retain request URLs, headers, response bodies, or auth-bearing exception strings.
    if isinstance(e,NetworkError):return str(e)
    return type(e).__name__+'：请求失败，请检查网络或本机接口'

def validate_url(url):
    p=urlparse(url)
    if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None,443):
        raise ValueError('仅支持已知来源的 HTTPS 地址')
    if re.search(r'(token|password|cookie|key)=',p.query,re.I):
        raise ValueError('第一版请使用公开单番 RSS，不接收含凭据的私人订阅')
    return url

class Web:
    def __init__(self):
        self.session=requests.Session();self.session.headers['User-Agent']='AnimeService/0.1 (local personal anime tracker; https://github.com/bangumi/api)'
    def fetch(self,url,method='GET',**kwargs):
        validate_url(url)
        for attempt in range(3):
            try:
                target=url
                for _ in range(4):
                    r=self.session.request(method,target,timeout=(8,20),allow_redirects=False,**kwargs)
                    if r.is_redirect:
                        from urllib.parse import urljoin
                        target=validate_url(urljoin(target,r.headers['Location']));continue
                    if not r.ok:raise NetworkError(f'{urlparse(url).hostname} HTTP {r.status_code}')
                    if len(r.content)>12*1024*1024:raise NetworkError('响应超过12MiB限制')
                    if 'text/' in r.headers.get('Content-Type','') and 'charset=' not in r.headers.get('Content-Type','').lower():r.encoding='utf-8'
                    return r
                raise NetworkError('重定向次数过多')
            except (requests.RequestException,NetworkError) as e:
                if attempt==2:raise NetworkError(safe_error(e)) from None
                time.sleep(0.5*(2**attempt))
