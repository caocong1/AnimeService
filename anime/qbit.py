import json,threading,subprocess,time
from pathlib import Path
import requests
from .net import NetworkError

CATEGORY='AnimeService'; TAG='AnimeService-v1'
class Qbit:
    def __init__(self,credential='D:/MediaService/config/downloader-account.json'):
        self.credential=Path(credential);self.session=requests.Session();self.base='http://127.0.0.1:4870/api/v2/'
        self.session.headers['Referer']='http://127.0.0.1:4870';self.lock=threading.RLock()
        self.last_launch=0
    def ensure_running(self):
        import psutil
        if time.time()-self.last_launch<60:return
        for p in psutil.process_iter(['name','cmdline']):
            if (p.info['name'] or '').lower()=='qbittorrent.exe':
                cmd=' '.join(p.info['cmdline'] or []).replace('\\','/').lower()
                if '--profile=d:/mediaservice/qbit-profile' in cmd:return
        exe=Path('C:/Program Files/qBittorrent/qbittorrent.exe')
        if not exe.is_file():raise NetworkError('现有qBittorrent未安装在预期路径')
        si=subprocess.STARTUPINFO();si.dwFlags|=subprocess.STARTF_USESHOWWINDOW;si.wShowWindow=0
        subprocess.Popen([str(exe),'--profile=D:\\MediaService\\qbit-profile','--webui-port=4870'],startupinfo=si)
        self.last_launch=time.time()
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
                    except ValueError:return r.text()
                except requests.RequestException:raise NetworkError('qBittorrent请求超时或连接不可达') from None
            raise NetworkError('qBittorrent认证失败')
    def info(self,hash=None):return self.call('torrents/info',params={'hashes':hash} if hash else {})
    def files(self,h):return self.call('torrents/files',params={'hash':h})
    def owned(self,t):return t.get('category')==CATEGORY and TAG in [x.strip() for x in t.get('tags','').split(',')]
    def check_storage(self):
        p=self.call('app/preferences')
        if not p.get('temp_path_enabled') or p.get('temp_path','').replace('\\','/').rstrip('/').lower()!='d:/mediadownloads/incomplete':
            raise NetworkError('下载器未完成目录与约定不一致，未修改全局设置')
    def add(self,h,data,savepath):
        self.check_storage()
        categories=self.call('torrents/categories')
        if CATEGORY not in categories:self.call('torrents/createCategory','POST',data={'category':CATEGORY,'savePath':'D:/MediaLibrary/Anime'})
        return self.call('torrents/add','POST',files={'torrents':(h+'.torrent',data,'application/x-bittorrent')},data={
            'savepath':savepath,'category':CATEGORY,'tags':TAG,'stopped':'true','paused':'true','autoTMM':'false',
            'contentLayout':'Original','ratioLimit':'1','seedingTimeLimit':'60'})
    def priorities(self,h,ids,priority):
        if ids:self.call('torrents/filePrio','POST',data={'hash':h,'id':'|'.join(str(i) for i in ids),'priority':priority})
    def stop(self,h):self.call('torrents/stop','POST',data={'hashes':h})
    def start(self,h):self.call('torrents/start','POST',data={'hashes':h})
