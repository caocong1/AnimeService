"""Local, conservative audio alignment. PCM and signed URLs exist only in memory."""
import hashlib
import io
import ipaddress
import json
import math
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

ALGORITHM = 'chromaprint2-three-anchors-v1'
HOP = 1365 / 11025
MAX_BYTES = 8 * 1024 * 1024
MAX_PCM = 80 * 1024 * 1024
ACTIVE = {'queued', 'running'}
MESSAGES = {
    'queued': '等待音频对齐', 'running': '正在比对音频', 'matched': '音频一致，已计算偏移',
    'unreliable': '音频未能可靠匹配，请手动调时', 'unavailable': '音频对齐暂时失败，可点击重新对齐或手动调时',
    'unsupported': '此来源暂不支持音频对齐', 'cancelled': '已停止自动对齐',
}
FAILURES = {
    'dependency': ('音频对齐依赖未安装或无法加载，请检查本机安装', False),
    'tool': ('音频读取工具不存在或无法执行，请检查本机安装', False),
    'media_changed': ('本地文件不存在或已变化，请重新核查', False),
    'local_audio': ('无法读取本地音轨，请检查文件或使用弹弹play', False),
    'duration': ('音频对齐目前支持4至60分钟的本地媒体', False),
    'fingerprint': ('音频指纹计算失败，可手动调时', False),
    'source_info': ('来源音轨信息暂时读取失败，可重试', True),
    'source_audio': ('来源音频片段暂时读取失败，可重试', True),
    'no_source_audio': ('此来源没有可读取的独立音轨，可换来源或手动调时', False),
    'short_source': ('此来源音频太短，无法可靠自动对齐', False),
    'budget': ('来源音轨读取已达到流量上限，请换来源或手动调时', False),
    'timeout': ('音频对齐超时，可重试', True),
    'busy': ('音频对齐正在忙，稍后可重试', True),
}


def failure(reason=None):
    message, retryable = FAILURES.get(reason, (MESSAGES['unavailable'], False))
    return {'status': 'unavailable', 'message': message, 'reason': reason if reason in FAILURES else 'unknown',
            'retryable': retryable}


class Unavailable(Exception):
    def __init__(self, reason=None):
        self.reason = reason if reason in FAILURES else None
        super().__init__()


class Cancelled(Exception):
    pass


def canonical_source(url):
    try:
        u = urlsplit(url)
        if u.scheme != 'https' or u.netloc != 'www.bilibili.com': return None
        if not re.fullmatch(r'/video/BV[0-9A-Za-z]{10}/?', u.path) or u.fragment: return None
        q = parse_qs(u.query, strict_parsing=True) if u.query else {}
        if set(q) - {'p'} or len(q.get('p', ['1'])) != 1: return None
        p = q.get('p', ['1'])[0]
        if not re.fullmatch(r'[1-9][0-9]{0,3}', p): return None
        return f'https://www.bilibili.com{u.path.rstrip("/")}?p={int(p)}'
    except (ValueError, TypeError): return None


def validate_cdn(url):
    try:
        u = urlsplit(url)
        host = u.hostname or ''
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443): raise Unavailable()
        if not any(host.endswith('.' + d) or host == d for d in ('bilivideo.com', 'bilivideo.cn')): raise Unavailable()
        ips = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not ips or any(not ipaddress.ip_address(a[4][0]).is_global for a in ips): raise Unavailable()
    except (OSError, ValueError): raise Unavailable() from None


def locate_fingerprint(reference, query):
    import numpy as np
    ref = np.asarray(reference, dtype=np.uint32)
    qry = np.asarray(query, dtype=np.uint32)
    if qry.size < 100 or ref.size < qry.size or np.unique(qry).size < 30:
        return {'reliable': False}
    # Eight identical frames in a row / low-changing fingerprints are poor anchors.
    if np.count_nonzero(qry[1:] != qry[:-1]) / (qry.size - 1) < .5:
        return {'reliable': False}
    table = np.array([n.bit_count() for n in range(256)], dtype=np.uint8)
    scores = np.empty(ref.size - qry.size + 1, dtype=float)
    for start in range(0, scores.size, 128):
        count = min(128, scores.size - start)
        windows = np.lib.stride_tricks.sliding_window_view(ref[start:start + count + qry.size - 1], qry.size)
        xor = np.ascontiguousarray(np.bitwise_xor(windows, qry))
        scores[start:start + count] = table[xor.view(np.uint8)].reshape(count, -1).mean(axis=1) / 8
    best = int(scores.argmin())
    independent = scores.copy(); radius = math.ceil(5 / HOP)
    independent[max(0, best-radius):best+radius+1] = np.inf
    error, alternative = float(scores[best]), float(independent.min())
    return {'reliable': error <= .18 and math.isfinite(alternative) and alternative - error >= .12,
            'local_time': best * HOP, 'error': error, 'alternative_error': alternative}


def decide_alignment(anchors, duration):
    if len(anchors) != 3 or any(not a.get('reliable') for a in anchors): return None
    try:
        local = [float(a['local_time']) for a in anchors]
        source = [float(a['source_time']) for a in anchors]
        offsets = sorted(l-s for l,s in zip(local, source))
        if not all(math.isfinite(n) for n in local + source + offsets): return None
        if min(local + source) < 120 or max(local) > duration-30: return None
        if max(local)-min(local) < max(90, duration*.3): return None
        if offsets[-1]-offsets[0] > .5 or abs(offsets[1]) > 3600: return None
        return round(offsets[1], 3)
    except (KeyError, ValueError, TypeError): return None


class RangeProxy:
    """One unguessable loopback path, one validated CDN, a shared hard read budget."""
    def __init__(self, url, headers, check):
        self.remote = url
        self.headers = {k:v for k,v in headers.items() if k.lower() in ('user-agent','referer') and '\r' not in v and '\n' not in v}
        self.check = check
        self.downloaded = 0
        self.failed = False
        self.error_type = None
        self.error_reason = None
        self.total = None
        self.blocks = []
        self.cache_lock = threading.Lock()
        self.generation = 0
        self.content_type = 'audio/mp4'
        self.session = requests.Session(); self.session.trust_env = False
        self.path = '/' + secrets.token_urlsafe(24)
        proxy = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_HEAD(self): self.serve(True)
            def do_GET(self): self.serve(False)
            def serve(self, head):
                response = None
                sent = False
                try:
                    with proxy.cache_lock:
                        if not head:proxy.generation+=1
                        generation=proxy.generation
                    self.connection.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,32768)
                    self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                    proxy.check()
                    if self.path != proxy.path: self.send_error(404); return
                    requested = self.headers.get('Range', 'bytes=0-')
                    match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
                    if not match: raise Unavailable()
                    start = int(match[1]); end = int(match[2]) if match[2] else None
                    if end is not None and end < start: raise Unavailable()
                    def cached(position):
                        for at,data in proxy.blocks:
                            if at <= position < at+len(data): return data[position-at:]
                        return None
                    if head or proxy.total is None:
                        response=proxy._open(start,end,head)
                    if proxy.total is None or start >= proxy.total: raise Unavailable()
                    end=min(end if end is not None else proxy.total-1,proxy.total-1)
                    if response is not None and not head:
                        end=min(end,int(response.headers['Content-Range'].split('/')[0].split('-')[1]))
                    if not head and cached(start) is None and proxy.downloaded>=MAX_BYTES:raise Unavailable('budget')
                    self.send_response(206)
                    self.send_header('Content-Type',proxy.content_type)
                    self.send_header('Content-Length',str(end-start+1))
                    self.send_header('Content-Range',f'bytes {start}-{end}/{proxy.total}')
                    self.send_header('Accept-Ranges','bytes')
                    self.send_header('Connection','close'); self.end_headers()
                    sent=True
                    if not head:
                        position=start
                        while position<=end:
                            proxy.check()
                            if proxy.stop.is_set() or generation!=proxy.generation:return
                            with proxy.cache_lock:
                                chunk=cached(position)
                                if chunk is None:
                                    remaining=MAX_BYTES-proxy.downloaded
                                    if remaining<=0:raise Unavailable('budget')
                                    if response is None:response=proxy._open(position,end)
                                    chunk=response.raw.read(min(8192,remaining,end-position+1))
                                    if not chunk:raise Unavailable()
                                    proxy.downloaded+=len(chunk);proxy.blocks.append((position,chunk))
                            if generation!=proxy.generation:return
                            chunk=chunk[:end-position+1]
                            self.wfile.write(chunk); self.wfile.flush()
                            position+=len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
                except Exception as error:
                    if proxy.stop.is_set() or generation!=proxy.generation:return
                    proxy.failed = True
                    proxy.error_type = type(error).__name__
                    proxy.error_reason = error.reason if isinstance(error, Unavailable) else None
                    try:
                        if not sent:self.send_error(502)
                    except OSError: pass
                finally:
                    if response is not None: response.close()
                    self.close_connection = True
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            def handle_error(self,*_):pass  # Client seek/disconnect is normal; no tool/URL logs.
            def get_request(self):
                client,address=super().get_request();client.settimeout(5);return client,address
        self.server = Server(('127.0.0.1',0),Handler)
        self.server.timeout = .2
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve,daemon=True)

    def _open(self,start,end,head=False):
        headers={**self.headers,'Range':f'bytes={start}-{end if end is not None else ""}','Accept-Encoding':'identity'}
        remote=self.remote;response=None
        try:
            for _ in range(4):
                self.check();validate_cdn(remote)
                response=self.session.request('HEAD' if head else 'GET',remote,headers=headers,stream=True,timeout=(3,5),allow_redirects=False)
                if response.status_code not in (301,302,303,307,308):break
                remote=response.headers.get('Location','');response.close();response=None
            if response is None or response.status_code not in ((200,206) if head else (206,)):raise Unavailable()
            if response.headers.get('Content-Encoding','identity') not in ('identity',''):raise Unavailable()
            cr=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
            if cr:
                if int(cr[1])!=start or end is not None and int(cr[2])>end:raise Unavailable()
                total=int(cr[3])
            elif head:total=int(response.headers['Content-Length'])
            else:raise Unavailable()
            if self.total is not None and self.total!=total:raise Unavailable()
            self.total=total;self.content_type=response.headers.get('Content-Type','audio/mp4')
            return response
        except Exception:
            if response is not None:response.close()
            raise

    def _serve(self):
        while not self.stop.is_set(): self.server.handle_request()

    def __enter__(self):
        validate_cdn(self.remote); self.thread.start()
        return f'http://127.0.0.1:{self.server.server_port}{self.path}'

    def __exit__(self, *_):
        self.stop.set(); self.thread.join(6); self.server.server_close(); self.session.close()


class Quiet:
    def debug(self,*_,**__): pass
    warning = error = debug


class AlignmentService:
    def __init__(self, cache_dir, ffmpeg, ffprobe, fpcalc):
        self.cache_dir = Path(cache_dir)
        self.ffmpeg, self.ffprobe, self.fpcalc = map(str,(ffmpeg,ffprobe,fpcalc))
        self.lock = threading.RLock(); self.queue = queue.Queue(maxsize=20)
        self.jobs = {}; self.pairs = {}; self.worker = None
        self.extractor_lock = threading.Lock()

    def _public(self, job):
        return {k:v for k,v in job.items() if k in ('job_id','status','message','offset','anchors','algorithm','reason','retryable')}

    def start(self, item, source_url, source_identity, force=False):
        url = canonical_source(source_url)
        if not url: return {'status':'unsupported','message':MESSAGES['unsupported']}
        try:
            import numpy
            import yt_dlp
        except Exception: return failure('dependency')
        if any(not (Path(p).is_file() or shutil.which(p)) for p in (self.ffmpeg,self.ffprobe,self.fpcalc)):
            return failure('tool')
        try:
            path = Path(item['path']); stat = path.stat()
            if stat.st_size != item['size']: raise Unavailable('media_changed')
        except Exception: return failure('media_changed')
        pair = hashlib.sha256(json.dumps([ALGORITHM,str(path.resolve()),stat.st_size,stat.st_mtime_ns,url,source_identity]).encode()).hexdigest()
        with self.lock:
            existing = self.jobs.get(self.pairs.get(pair))
            ttl = 86400 if existing and existing['status']=='matched' else 1800 if existing and existing['status']=='unreliable' else 60
            if existing and (existing['status'] in ACTIVE or not force and existing['status'] != 'cancelled'
                             and time.time()-existing.get('finished',0)<ttl):
                return self._public(existing)
            if self.queue.full(): return failure('busy')
            for key in list(self.jobs):
                if len(self.jobs) < 200: break
                if self.jobs[key]['status'] not in ACTIVE:
                    old = self.jobs.pop(key)
                    if self.pairs.get(old['pair']) == key: self.pairs.pop(old['pair'])
            job = {'job_id':secrets.token_urlsafe(18),'status':'queued','message':MESSAGES['queued'],
                   'pair':pair,'item':dict(item),'url':url,'identity':source_identity,'mtime':stat.st_mtime_ns,
                   'stop':threading.Event(),'deadline':None,'force':force,'process':None}
            self.jobs[job['job_id']] = job; self.pairs[pair] = job['job_id']
            self.queue.put_nowait(job)
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work,daemon=True); self.worker.start()
            return self._public(job)

    def status(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return self._public(job) if job else {'status':'unavailable','message':'对齐任务已失效'}

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job['status'] in ACTIVE:
                job['stop'].set(); job['status']='cancelled'; job['message']=MESSAGES['cancelled']
                if job['process']:
                    try: job['process'].kill()
                    except OSError: pass

    def _check(self, job):
        if job['stop'].is_set(): raise Cancelled()
        if job['deadline'] and time.monotonic() > job['deadline']: raise Unavailable('timeout')
        try:stat = Path(job['item']['path']).stat()
        except OSError:raise Unavailable('media_changed') from None
        if stat.st_size != job['item']['size'] or stat.st_mtime_ns != job['mtime']: raise Unavailable('media_changed')

    def _run(self, job, args, data=None, cap=MAX_PCM):
        self._check(job)
        try:
            process = subprocess.Popen(args,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError:raise Unavailable('tool') from None
        with self.lock: job['process']=process
        # Drain bounded stdout while checking cancellation; never buffer unbounded communicate().
        chunks=[]; total=0; overflow=threading.Event()
        def read():
            nonlocal total
            try:
                while True:
                    chunk=process.stdout.read(65536)
                    if not chunk:break
                    total+=len(chunk)
                    if total>cap:overflow.set();process.kill();break
                    chunks.append(chunk)
            except OSError:overflow.set()
        def write():
            try:process.stdin.write(data);process.stdin.close()
            except OSError:pass
        reader=threading.Thread(target=read,daemon=True);reader.start()
        writer=None
        if data is not None:writer=threading.Thread(target=write,daemon=True);writer.start()
        try:
            while process.poll() is None:
                self._check(job)
                if overflow.is_set():raise Unavailable()
                time.sleep(.05)
            reader.join(2)
            self._check(job)
            if reader.is_alive() or overflow.is_set() or process.returncode:raise Unavailable()
            return b''.join(chunks)
        finally:
            if process.poll() is None:process.kill()
            process.wait(timeout=3);reader.join(2)
            if writer:writer.join(2)
            process.stdout.close()
            if process.stdin and not process.stdin.closed:process.stdin.close()
            with self.lock:job['process']=None

    def _fingerprint(self, job, pcm):
        import numpy as np
        wav=io.BytesIO()
        with wave.open(wav,'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(11025);output.writeframes(pcm)
        raw=self._run(job,[self.fpcalc,'-format','wav','-algorithm','2','-raw','-json','-length','0','-'],wav.getvalue(),cap=1024*1024)
        result=json.loads(raw)
        return np.asarray(result['fingerprint'],dtype=np.uint32)

    def _probe(self, job):
        raw=self._run(job,[self.ffprobe,'-v','error','-protocol_whitelist','file,pipe','-select_streams','a',
                          '-show_entries','format=duration:stream=index:stream_tags=language:stream_disposition=default',
                          '-of','json',job['item']['path']],cap=1024*1024)
        data=json.loads(raw);duration=float(data['format']['duration'])
        if not math.isfinite(duration) or not 240<=duration<=3600:raise Unavailable('duration')
        if not data.get('streams'):raise Unavailable('local_audio')
        streams=sorted(data['streams'],key=lambda s:(s.get('tags',{}).get('language','').lower() not in ('jpn','ja'),not s.get('disposition',{}).get('default')))
        return duration,int(streams[0]['index'])

    def _source_audio(self, job):
        from yt_dlp import YoutubeDL
        result=[]; error=[]; done=threading.Event()
        if not self.extractor_lock.acquire(blocking=False):raise Unavailable('busy')
        # Extractor may block on a remote request. It never owns a worker result/cache.
        def extract():
            try:
                with YoutubeDL({'quiet':True,'no_warnings':True,'logger':Quiet(),'skip_download':True,
                                'noplaylist':True,'socket_timeout':8,'retries':0,'extractor_retries':0,
                                'cachedir':False,'http_headers':{'User-Agent':'Mozilla/5.0'}}) as ydl:
                    info=ydl.extract_info(job['url'],download=False)
                if info.get('entries'):raise Unavailable()
                formats=[f for f in info.get('formats',[]) if f.get('vcodec')=='none' and f.get('acodec') not in (None,'none')]
                if not formats:raise Unavailable('no_source_audio')
                audio=min(formats,key=lambda f:f.get('abr') or f.get('tbr') or float('inf'))
                validate_cdn(audio['url'])
                duration=float(info.get('duration') or 0)
                if not math.isfinite(duration) or duration<240:raise Unavailable('short_source')
                result.append((audio['url'],audio.get('http_headers') or info.get('http_headers') or
                               {'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'},duration, str(info.get('id',''))))
            except Exception as exc:error.append(exc.reason if isinstance(exc,Unavailable) and exc.reason else 'source_info')
            finally:self.extractor_lock.release();done.set()
        thread=threading.Thread(target=extract,daemon=True);thread.start()
        until=min(job['deadline'],time.monotonic()+30)
        while not done.wait(.1):
            self._check(job)
            if time.monotonic()>until:raise Unavailable('source_info')
        self._check(job)
        if error or not result:raise Unavailable(error[0] if error else 'source_info')
        return result[0]

    def _cache_write(self, path, value):
        self.cache_dir.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.'+secrets.token_hex(4)+'.tmp')
        try:
            temp.write_text(json.dumps(value,allow_nan=False),encoding='utf-8');temp.replace(path)
        finally:temp.unlink(missing_ok=True)
        files=sorted(self.cache_dir.glob('*.json'),key=lambda p:p.stat().st_mtime)
        for old in files[:-120]:old.unlink(missing_ok=True)

    def _align(self, job):
        import numpy as np
        job['stage']='local_audio'
        duration,track=self._probe(job)
        local=job['item']
        reference_key=hashlib.sha256(json.dumps([ALGORITHM,str(Path(local['path']).resolve()),local['size'],job['mtime'],track]).encode()).hexdigest()
        ref_path=self.cache_dir/(reference_key+'.json')
        result_key=hashlib.sha256(f'{reference_key}|{job["pair"]}'.encode()).hexdigest()
        result_path=self.cache_dir/(result_key+'.json')
        if not job['force'] and result_path.is_file() and time.time()-result_path.stat().st_mtime<86400:
            try:
                cached=json.loads(result_path.read_text())
                if cached.get('algorithm')==ALGORITHM and decide_alignment(cached['anchors'],duration)==cached['offset']:
                    return {k:v for k,v in cached.items() if k in ('status','offset','anchors','algorithm')}
            except Exception:pass
        reference=None
        if ref_path.is_file():
            try:reference=np.asarray(json.loads(ref_path.read_text())['fingerprint'],dtype=np.uint32)
            except Exception:pass
        if reference is None:
            pcm=self._run(job,[self.ffmpeg,'-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',job['item']['path'],
                               '-t',str(duration),'-map',f'0:{track}','-vn','-ac','1','-ar','11025','-f','s16le','pipe:1'])
            job['stage']='fingerprint'
            reference=self._fingerprint(job,pcm);del pcm
            with self.lock:
                self._check(job);self._cache_write(ref_path,{'fingerprint':reference.tolist()})
        job['stage']='source_info'
        url,headers,remote_duration,remote_id=self._source_audio(job)
        common=min(duration,remote_duration)
        starts=[common*x for x in (.22,.48,.75)]
        anchors=[]
        proxy=RangeProxy(url,headers,lambda:self._check(job))
        job['stage']='source_audio'
        with proxy as local_url:
            for start in starts:
                self._check(job)
                if start<120 or start+35>common-30:return {'status':'unreliable'}
                try:
                    pcm=self._run(job,[self.ffmpeg,'-nostdin','-v','error','-protocol_whitelist','http,tcp',
                                       '-rw_timeout','10000000','-ss',str(start),'-i',local_url,'-t','35',
                                       '-map','0:a:0','-vn','-ac','1','-ar','11025','-f','s16le','pipe:1'],cap=11025*2*36)
                except Unavailable as exc:
                    raise Unavailable(exc.reason or proxy.error_reason or 'source_audio') from None
                if len(pcm)<11025*2*30:raise Unavailable()
                job['stage']='fingerprint'
                anchor=locate_fingerprint(reference,self._fingerprint(job,pcm))
                job['stage']='source_audio'
                anchor['source_time']=start;anchors.append(anchor)
                if not anchor.get('reliable'):return {'status':'unreliable'}
            if proxy.failed:raise Unavailable(proxy.error_reason or 'source_audio')
        offset=decide_alignment(anchors,duration)
        if offset is None:return {'status':'unreliable'}
        result={'status':'matched','offset':offset,'anchors':anchors,'algorithm':ALGORITHM}
        with self.lock:
            self._check(job);self._cache_write(result_path,{**result,'remote_id':remote_id})
        return result

    def _work(self):
        while True:
            job=self.queue.get()
            try:
                with self.lock:
                    if job['stop'].is_set():continue
                    job['deadline']=time.monotonic()+120
                    job['status']='running';job['message']=MESSAGES['running']
                result=self._align(job)
                with self.lock:
                    self._check(job);job.update(result);job['message']=MESSAGES[job['status']]
            except Cancelled:pass
            except Exception as exc:
                with self.lock:
                    if not job['stop'].is_set():
                        reason=exc.reason if isinstance(exc,Unavailable) else None
                        job.update(failure(reason or job.get('stage')))
            finally:
                # Do not retain tool output, PCM, or URLs after this job.
                job.pop('url',None);job.pop('item',None)
                job['finished']=time.time()
                self.queue.task_done()
