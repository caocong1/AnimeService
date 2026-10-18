"""Copy-only container normalization for MP4 layouts that stall browser metadata loading."""
import json,os,secrets,struct,threading
from pathlib import Path


def needs_normalization(path):
    if Path(path).suffix.lower()!='.mp4':return False
    size=Path(path).stat().st_size;offset=0;mdats=0
    with Path(path).open('rb') as source:
        for _ in range(32):
            if offset+8>size:return False
            source.seek(offset);length,kind=struct.unpack('>I4s',source.read(8))
            if length==1:
                raw=source.read(8)
                if len(raw)!=8:return False
                length=struct.unpack('>Q',raw)[0]
            if length==0:length=size-offset
            if length<8 or length>size-offset:return False
            if kind==b'moof':return False
            if kind==b'mdat':mdats+=1
            if mdats>=4:return True
            offset+=length
    return False


class BrowserMedia:
    LIMIT=8*1024**3  # bytes of packet copies kept; oldest copies go first, never the originals
    def __init__(self,subtitles):
        self.sub=subtitles;self.lock=threading.Lock();self.jobs={}
        self.root=subtitles.db.path.parent/'cache/browser-media'
    def trim(self,keep,limit=None):
        """Evict least recently used copies until the cache fits; the copy just made and any job in progress stay."""
        if limit is None:
            limit=self.sub.db.get('browser_media_cache_bytes',self.LIMIT)
            if type(limit) is not int or limit<=0:limit=self.LIMIT
        with self.lock:
            copies=[]
            for p in self.root.glob('*.mp4'):
                if p==keep or p.name.endswith('.partial.mp4'):continue
                try:copies.append((p,p.stat()))
                except OSError:continue
            total=sum(s.st_size for _,s in copies)+(keep.stat().st_size if keep.exists() else 0)
            for p,s in sorted(copies,key=lambda item:item[1].st_mtime_ns):
                if total<=limit:break
                if p.stem in self.jobs and self.jobs[p.stem].locked():continue
                try:p.unlink(missing_ok=True)
                except OSError:continue  # An active Windows reader is not a playback failure.
                total-=s.st_size
                try:p.with_suffix('.json').unlink(missing_ok=True)
                except OSError:pass
    def cached(self,item):
        fingerprint=self.sub.fingerprint(item);target=self.root/(fingerprint+'.mp4');proof=self.root/(fingerprint+'.json')
        try:
            record=json.loads(proof.read_text(encoding='utf-8'))
            if record.get('fingerprint')==fingerprint and record.get('mode')=='packet_copy' and target.is_file() and target.stat().st_size==record.get('size'):
                try:os.utime(target,None)
                except OSError:pass
                return target
        except (ValueError,OSError):pass
        return None
    def file(self,item):
        if not needs_normalization(item['path']):return Path(item['path'])
        fingerprint=self.sub.fingerprint(item)
        with self.lock:job=self.jobs.setdefault(fingerprint,threading.Lock())
        with job:
            target=self.root/(fingerprint+'.mp4');proof=self.root/(fingerprint+'.json')
            cached=self.cached(item)
            if cached:return cached
            self.root.mkdir(parents=True,exist_ok=True)
            partial=self.root/(fingerprint+'-'+secrets.token_hex(8)+'.partial.mp4')
            try:
                # Packet copies preserve encoded picture and sound. Originals are never rewritten.
                try:
                    self.sub.run([self.sub.tool('ffmpeg'),'-nostdin','-v','error','-protocol_whitelist','file,pipe',
                                  '-i',item['path'],'-map','0','-c','copy','-movflags','+faststart',str(partial)],120)
                except Exception as error:
                    raise ValueError('浏览器兼容转封装失败，原文件未改动；可改用本机播放器') from error
                if fingerprint!=self.sub.fingerprint(item):raise ValueError('转封装期间原文件变化，请重新核查')
                os.replace(partial,target)
            finally:
                partial.unlink(missing_ok=True)  # a failed or interrupted copy leaves nothing behind
            proof.write_text(json.dumps({'fingerprint':fingerprint,'size':target.stat().st_size,'mode':'packet_copy'},ensure_ascii=False),encoding='utf-8')
            self.trim(target)
            return target
