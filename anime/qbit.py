import json,threading,socket
from pathlib import Path
import requests
from .net import NetworkError

CATEGORY='AnimeService'; TAG='AnimeService-v1'
class QbitUnavailable(NetworkError):
    """Local dependency failure; never evidence that a torrent is absent."""
    code='qbit_unavailable'

class Qbit:
    def __init__(self,credential='D:/MediaService/config/downloader-account.json'):
        self.credential=Path(credential);self.session=requests.Session();self.base='http://127.0.0.1:4870/api/v2/'
        # The fixed loopback API must never inherit outbound proxy/netrc settings.
        self.session.trust_env=False
        self.session.headers['Referer']='http://127.0.0.1:4870';self.lock=threading.RLock()
    def ensure_running(self):
        from .qbit_recovery import restore_original
        return restore_original(Path(__file__).resolve().parents[1]/'data')
    def call(self,endpoint,method='GET',**kwargs):
        with self.lock:
            for attempt in range(2):
                try:
                    r=self.session.request(method,self.base+endpoint,timeout=(3,15),**kwargs)
                    if r.status_code==403 and attempt==0:
                        credentials=json.loads(self.credential.read_text(encoding='utf-8-sig'))
                        login=self.session.post(self.base+'auth/login',data=credentials,timeout=(3,10))
                        if not login.ok or not self.session.cookies:raise NetworkError('qBittorrent登录失败，凭据保持原样')
                        continue
                    if not r.ok:raise NetworkError(f'qBittorrent {endpoint} HTTP {r.status_code}')
                    try:return r.json()
                    except ValueError:return r.text
                except requests.Timeout:
                    raise QbitUnavailable('qBittorrent本机接口响应超时；保留原任务，后台继续对账') from None
                except requests.ConnectionError:
                    try:
                        with socket.create_connection(('127.0.0.1',4870),timeout=1):pass
                    except OSError:
                        raise QbitUnavailable('qBittorrent本机接口未监听（127.0.0.1:4870）；需恢复原下载器，任务与回执已保留') from None
                    raise QbitUnavailable('qBittorrent本机接口连接中断；保留原任务，后台继续对账') from None
                except requests.RequestException:
                    raise QbitUnavailable('qBittorrent本机接口请求失败；保留原任务，后台继续对账') from None
            raise NetworkError('qBittorrent认证失败')
    def info(self,hash=None):return self.call('torrents/info',params={'hashes':hash} if hash else {})
    def files(self,h):return self.call('torrents/files',params={'hash':h})
    def owned(self,t):return t.get('category')==CATEGORY and TAG in [x.strip() for x in t.get('tags','').split(',')]
    def check_storage(self, savepath=None):
        from .download_storage import download_roots, torrent_staging
        p=self.call('app/preferences')
        if torrent_staging(savepath or download_roots()[0]):
            # Explicit per-torrent paths override global preferences. The real
            # torrent path must still match before any selected files start.
            return
        if not p.get('temp_path_enabled') or p.get('temp_path','').replace('\\','/').rstrip('/').lower()!='d:/mediadownloads/incomplete':
            raise NetworkError('下载器未完成目录与约定不一致，未修改全局设置')
    def add(self,h,data,savepath):
        self.check_storage(savepath)
        categories=self.call('torrents/categories')
        from .download_storage import download_roots, torrent_staging
        stage = torrent_staging(savepath)
        options = {}
        if stage:
            stage.mkdir(parents=True,exist_ok=True)
            options = {'useDownloadPath':'true','downloadPath':str(stage)}
        if CATEGORY not in categories:self.call('torrents/createCategory','POST',data={'category':CATEGORY,'savePath':str(download_roots()[0])})
        return self.call('torrents/add','POST',files={'torrents':(h+'.torrent',data,'application/x-bittorrent')},data={
            **options,'savepath':savepath,'category':CATEGORY,'tags':TAG,'stopped':'true','paused':'true','autoTMM':'false',
            'contentLayout':'Original','ratioLimit':'1','seedingTimeLimit':'60'})
    def priorities(self,h,ids,priority):
        if ids:self.call('torrents/filePrio','POST',data={'hash':h,'id':'|'.join(str(i) for i in ids),'priority':priority})
    def stop(self,h):self.call('torrents/stop','POST',data={'hashes':h})
    def start(self,h):self.call('torrents/start','POST',data={'hashes':h})
