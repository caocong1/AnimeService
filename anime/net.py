import re,time
from pathlib import Path
from urllib.parse import urlparse
import requests

HOSTS={'api.bgm.tv','bgm.tv','bangumi.tv','mikanani.me','mikanime.tv','mikanani.hacgn.fun',
       'share.dmhy.org','dl.dmhy.org','nyaa.si','www.tokyotosho.info','tokyotosho.info',
       'www.tokyotosho.se','tokyotosho.se','www.tokyo-tosho.net','tokyo-tosho.net',
       'bt4g.org','bt4gprx.com','1337x.to','www.1337x.to','itorrents.net'}
# Responses that mean the site, not one feed, is unavailable: blocks, throttling, outages.
SITE_WIDE_STATUSES={403,408,425,429,500,502,503,504}
class NetworkError(RuntimeError):
    def __init__(self,message,status=None,site_wide=None,retry_after=0):
        super().__init__(message);self.status=status;self.retry_after=retry_after
        self.site_wide=site_wide if site_wide is not None else (status is None or status in SITE_WIDE_STATUSES)


def retry_after(response):
    """Seconds the server asked us to wait, capped at a day; dates and nonsense count as none."""
    value=str(response.headers.get('Retry-After','')).strip()
    return min(86400,int(value)) if value.isdigit() else 0

def safe_error(e):
    # Never retain request URLs, headers, response bodies, or auth-bearing exception strings.
    if isinstance(e,NetworkError) or getattr(e,'safe_message',False):return str(e)
    return type(e).__name__+'：请求失败，请检查网络或本机接口'+where(e)

def where(e):
    """Innermost frame of this package as module.function:line; no paths, arguments or values."""
    tb=e.__traceback__;spot=''
    while tb:
        code=tb.tb_frame.f_code
        if re.search(r'[\\/]anime[\\/][^\\/]+\.py$',code.co_filename):spot=f'（{Path(code.co_filename).stem}.{code.co_name}:{tb.tb_lineno}）'
        tb=tb.tb_next
    return spot

def validate_url(url):
    p=urlparse(url)
    if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None,443):
        raise ValueError('仅支持已知来源的 HTTPS 地址')
    if re.search(r'(token|password|cookie|key)=',p.query,re.I):
        raise ValueError('第一版请使用公开单番 RSS，不接收含凭据的私人订阅')
    return url

class Web:
    def __init__(self):
        self.session=requests.Session();self.session.headers['User-Agent']='sorawatcher/AnimeService/2.0 (personal anime tracker)'
    def fetch(self,url,method='GET',retries=3,**kwargs):
        validate_url(url)
        for attempt in range(retries):
            try:
                target=url
                for _ in range(4):
                    r=self.session.request(method,target,timeout=(8,20),allow_redirects=False,**kwargs)
                    if r.is_redirect:
                        from urllib.parse import urljoin
                        target=validate_url(urljoin(target,r.headers['Location']));continue
                    if not r.ok:raise NetworkError(f'{urlparse(url).hostname} HTTP {r.status_code}',r.status_code,retry_after=retry_after(r))
                    if len(r.content)>12*1024*1024:raise NetworkError('响应超过12MiB限制')
                    if 'text/' in r.headers.get('Content-Type','') and 'charset=' not in r.headers.get('Content-Type','').lower():r.encoding='utf-8'
                    return r
                raise NetworkError('重定向次数过多')
            except NetworkError:
                raise  # an HTTP answer, a block or an oversized body is final for this call
            except requests.RequestException as e:
                if attempt==retries-1:raise NetworkError(safe_error(e)) from None
                time.sleep(0.5*(2**attempt))  # only connection trouble is retried
