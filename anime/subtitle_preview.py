"""Isolated loopback ASS QA preview, never an AnimeService viewing session.

Files are explicitly allowlisted, media is rechecked against the original host
gates and manifest, and the random route is kept out of logs. This service has
no Engine, downloader, playback history, quality acceptance or default-track
write path. Machine frame advancement is not a human/model audiovisual review.
"""
import hashlib
import json
import mimetypes
import secrets
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .fidelity import fidelity_blocker
from .viewing_workflow import lane, scope_blocker


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path):
    p = Path(path).resolve(); st = p.stat()
    return hashlib.sha256(f'{p}|{st.st_size}|{st.st_mtime_ns}'.encode()).hexdigest()


def verified_target(db, sid, ep, task_hash):
    if lane(db, sid) != 'watching' or scope_blocker(db, sid, ep):
        raise ValueError('Preview requires current watching selection and original episode authorization')
    task = db.one('SELECT * FROM tasks WHERE hash=? AND show_id=?', (task_hash, sid))
    if not task or task['status'] not in ('complete', 'linked'):
        raise ValueError('Preview media completion/ownership is unverified')
    selection = json.loads(task['selection'])
    selected = selection.get('selected', {}).get(str(ep))
    if (not selected or selected.get('index') not in selection.get('ids', [])
            or not any(f == selected for f in selection.get('files', []))):
        raise ValueError('Exact episode manifest/file selection is required')
    if fidelity_blocker(db, task['title'], selection):
        raise ValueError('Preview source is excluded')
    if db.get('thunder_dialog_hold_' + task_hash, {}).get('unconfirmed'):
        raise ValueError('Unconfirmed submission/dialog remains protected')
    queue = db.one('SELECT committing,lease,state FROM thunder_queue WHERE hash=?', (task_hash,))
    if queue and (queue['committing'] or queue['lease'] or queue['state'] in ('claimed', 'review')):
        raise ValueError('Media task has an unresolved downloader lease or receipt')
    root = Path(task['save_path']).resolve()
    media = (root / selected['name']).resolve()
    if not media.is_relative_to(root) or not media.is_file() or media.stat().st_size != selected['size']:
        raise ValueError('Preview media path/size differs from its completed manifest')
    return {'show_id': sid, 'episode': ep, 'task_hash': task_hash,
            'path': str(media), 'size': selected['size'], 'fingerprint': fingerprint(media)}


def byte_range(header, size):
    """One RFC byte range, with suffix/open-end support; reject invalid/multi ranges."""
    if header is None:
        return 0, size - 1, False
    if not header.startswith('bytes=') or ',' in header:
        raise ValueError('Unsupported range')
    try:
        a, b = header[6:].split('-', 1)
        if not a:
            length = int(b)
            if length <= 0:
                raise ValueError('Empty suffix')
            first, last = max(0, size - length), size - 1
        else:
            first, last = int(a), int(b) if b else size - 1
        if first < 0 or first >= size or last < first:
            raise ValueError('Invalid range')
        return first, min(last, size - 1), True
    except (TypeError, ValueError):
        raise ValueError('Invalid range') from None


PLAYER = r'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>私有字幕 QA</title>
<style>body{margin:12px;background:#17191d;color:#eee;font:16px sans-serif}#stage{position:relative;width:640px;max-width:100%}video{display:block;width:100%}canvas{position:absolute;inset:0;pointer-events:none;width:100%;height:100%}button,input{margin:4px;padding:7px}pre{font-size:12px;max-height:160px;overflow:auto;white-space:pre-wrap}</style>
<p>仅本集非默认 QA。诊断双语字是渲染测试，原中文字幕是研究参考；未做整集验收，不写观看。</p>
<button id="play">播放（静音机器测试）</button><button id="audible">开启原声并播放</button><button id="pause">暂停</button><button id="small">640px</button><button id="large">1920px</button><button id="controls">控制条显隐</button>
<input id="time" type="number" value="1" min="0" step="0.001"><button id="seek">暂停定位</button><button id="diagnostic">诊断双语</button><button id="reference">原中文参考</button>
<div id="stage"><video id="video" src="video.mp4" muted playsinline controls preload="auto"></video><canvas id="subtitle"></canvas></div><pre id="status"></pre>
<script src="vendor/subtitles-octopus.js"></script><script>
const video=document.getElementById('video'),canvas=document.getElementById('subtitle'),stage=document.getElementById('stage');
const BASE=new URL('.',location.href).href,referenceOffset=__REFERENCE_OFFSET__;let renderer=null,track='diagnostic.ass',generation=0;
window.qa={ready:false,rendererReady:false,initialDecodeReady:false,errors:[],frames:[],events:[],worker:[],rendererGeneration:0,actual_listening:false,continuous_picture_review:false,accepted:false};
const WorkerOriginal=window.Worker;
window.Worker=class extends WorkerOriginal{constructor(...args){super(...args);this.addEventListener('error',e=>qa.errors.push('worker:'+e.message));this.addEventListener('message',e=>{if(e.data&&(['stderr','stdout','error'].includes(e.data.target))){qa.worker.push(e.data);if(qa.worker.length>80)qa.worker.shift();}})}};
function sync(time=video.currentTime){if(renderer&&qa.rendererReady)renderer.setCurrentTime(time+(track==='reference.ass'?referenceOffset:0));}
async function rebuild(width){stage.style.width=width+'px';if(renderer)renderer.dispose();qa.ready=false;qa.rendererReady=false;generation++;qa.rendererGeneration=generation;
 const size=stage.getBoundingClientRect();canvas.width=Math.round(size.width);canvas.height=Math.round(size.width*9/16);
 renderer=new SubtitlesOctopus({canvas,subUrl:BASE+track,fonts:[BASE+'font-sc.otf',BASE+'font-jp.otf'],availableFonts:{},fallbackFont:BASE+'font-sc.otf',workerUrl:BASE+'vendor/subtitles-octopus-worker.js',legacyWorkerUrl:BASE+'vendor/subtitles-octopus-worker-legacy.js',renderMode:'js-blend',targetFps:30,debug:true,onReady:()=>{qa.rendererReady=true;qa.ready=qa.initialDecodeReady;renderer.resize(canvas.width,canvas.height);sync();},onError:e=>qa.errors.push(String(e))});
 window.renderer=renderer;
}
function onFrame(now,meta){qa.frames.push({mediaTime:meta.mediaTime,presentedFrames:meta.presentedFrames,displayTime:meta.expectedDisplayTime,width:meta.width,height:meta.height});if(qa.frames.length>400)qa.frames.shift();sync(meta.mediaTime);video.requestVideoFrameCallback(onFrame);}
if(video.requestVideoFrameCallback)video.requestVideoFrameCallback(onFrame);else qa.errors.push('requestVideoFrameCallback unavailable');
for(const event of ['loadedmetadata','playing','pause','seeked','waiting','error'])video.addEventListener(event,()=>{qa.events.push({event,time:video.currentTime});sync();});
window.seek=async(t)=>{video.pause();if(Math.abs(video.currentTime-t)>.0001){await new Promise((resolve,reject)=>{let timeout=setTimeout(()=>reject(Error('seek timeout')),10000);video.addEventListener('seeked',()=>{clearTimeout(timeout);resolve();},{once:true});video.currentTime=t;});}sync(t);};
let primeStarted=false;
async function primeDecode(){if(primeStarted)return;primeStarted=true;try{video.muted=true;await video.play();await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(Error('decode priming timeout')),10000);function frame(now,meta){if(meta.presentedFrames>=2){clearTimeout(timeout);resolve();}else video.requestVideoFrameCallback(frame);}video.requestVideoFrameCallback(frame);});video.pause();await seek(0);qa.initialDecodeReady=true;qa.ready=qa.rendererReady;qa.events.push({event:'qa-only-muted-decode-primed',time:video.currentTime});sync(0);}catch(e){video.pause();qa.errors.push(String(e));}}
if(video.readyState>=1)primeDecode();else video.addEventListener('loadedmetadata',primeDecode,{once:true});
window.resize=async(w)=>{await rebuild(w);};
window.setReference=async(useReference)=>{track=useReference?'reference.ass':'diagnostic.ass';await rebuild(parseInt(stage.style.width)||640);};
window.snapshot=()=>{let alpha=0,top=0,bottom=0;const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;for(let i=3;i<data.length;i+=4)if(data[i]){alpha++;const y=Math.floor((i/4)/canvas.width);if(y<canvas.height*.88)top++;else bottom++;}return {...qa,currentTime:video.currentTime,duration:video.duration,paused:video.paused,muted:video.muted,videoWidth:video.videoWidth,videoHeight:video.videoHeight,canvas:{width:canvas.width,height:canvas.height,cssWidth:canvas.clientWidth,cssHeight:canvas.clientHeight,alphaPixels:alpha,topPixels:top,bottomPixels:bottom},controls:video.controls};};
document.getElementById('play').onclick=()=>{video.muted=true;video.play().catch(e=>qa.errors.push(String(e)));};
document.getElementById('audible').onclick=()=>{video.muted=false;video.play().catch(e=>qa.errors.push(String(e)));};
document.getElementById('pause').onclick=()=>video.pause();document.getElementById('small').onclick=()=>resize(640);document.getElementById('large').onclick=()=>resize(1920);
document.getElementById('seek').onclick=()=>seek(Number(document.getElementById('time').value));document.getElementById('controls').onclick=()=>video.controls=!video.controls;
document.getElementById('diagnostic').onclick=()=>setReference(false);document.getElementById('reference').onclick=()=>setReference(true);
setInterval(()=>document.getElementById('status').textContent=JSON.stringify({ready:qa.ready,time:video.currentTime,paused:video.paused,errors:qa.errors,decodedFrames:qa.frames.length},null,2),500);rebuild(640);
</script></html>'''


class PreviewServer:
    def __init__(self, db, target, assets, port=0):
        self.db, self.target = db, dict(target)
        self.token = secrets.token_urlsafe(32)
        self.assets = {}
        private_root=(db.path.parent/'subtitle-studio').resolve()
        vendor_root=(Path(__file__).resolve().parents[1]/'static/vendor/libass').resolve()
        names={'video.mp4','diagnostic.ass','reference.ass','font-sc.otf','font-jp.otf',
               'vendor/subtitles-octopus.js','vendor/subtitles-octopus-worker.js',
               'vendor/subtitles-octopus-worker.wasm','vendor/subtitles-octopus-worker-legacy.js'}
        if not {'video.mp4','diagnostic.ass','reference.ass','font-sc.otf','font-jp.otf'}.issubset(assets):
            raise ValueError('Explicit media/subtitle/font preview assets are required')
        for name, path in assets.items():
            p=Path(path).resolve()
            if (name not in names or not p.is_file()
                    or not (p.is_relative_to(private_root) or name.startswith('vendor/') and p.is_relative_to(vendor_root))):
                raise ValueError('Preview assets must be explicit existing allowlisted files')
            p = Path(path).resolve(); st = p.stat()
            self.assets[name] = (p, st.st_size, st.st_mtime_ns, digest(p))
        clip=Path(assets['video.mp4']).parent/'clip-manifest.json'
        self.clip=json.loads(clip.read_bytes())
        if self.clip.get('target')!=self.target or self.clip.get('video_sha256')!=self.assets['video.mp4'][3]:
            raise ValueError('Preview derivative does not match the exact target manifest')
        self.guard()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log random route permission or private URLs.
            def do_GET(self): self.serve(False)
            def do_HEAD(self): self.serve(True)
            def serve(self, head_only):
                expected_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
                if self.headers.get('Host') not in expected_hosts or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    self.send_error(403); return
                request = urlsplit(self.path)
                prefix = '/' + outer.token + '/'
                if request.query or not request.path.startswith(prefix):
                    self.send_error(404); return
                name = request.path[len(prefix):]
                try:
                    outer.guard()
                    if name in ('', 'index.html'):
                        data = PLAYER.replace('__REFERENCE_OFFSET__',str(float(outer.clip['requested_start_seconds']))).encode(); self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(data)))
                        self.security_headers(); self.end_headers()
                        if not head_only: self.wfile.write(data)
                        return
                    entry = outer.assets.get(name)
                    if not entry: self.send_error(404); return
                    p, size, modified, _ = entry
                    stat = p.stat()
                    if stat.st_size != size or stat.st_mtime_ns != modified:
                        self.send_error(409); return
                    try: first,last,ranged = byte_range(self.headers.get('Range'), size)
                    except ValueError:
                        self.send_response(416); self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
                    self.send_response(206 if ranged else 200)
                    self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
                    self.send_header('Accept-Ranges','bytes'); self.send_header('Content-Length',str(last-first+1))
                    if ranged:self.send_header('Content-Range',f'bytes {first}-{last}/{size}')
                    self.security_headers();self.end_headers()
                    if not head_only:
                        with p.open('rb') as source:
                            source.seek(first); remaining=last-first+1
                            while remaining:
                                data=source.read(min(1024*1024,remaining))
                                if not data:break
                                self.wfile.write(data);remaining-=len(data)
                except (ValueError,OSError):
                    self.send_error(403)
            def security_headers(self):
                self.send_header('Cache-Control','no-store')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('X-Content-Type-Options','nosniff')
        self.http = ThreadingHTTPServer(('127.0.0.1',port),Handler)
        self.thread = None

    @property
    def url(self):
        return f'http://127.0.0.1:{self.http.server_port}/{self.token}/'

    def guard(self):
        actual = verified_target(self.db, self.target['show_id'], self.target['episode'], self.target['task_hash'])
        if actual != self.target:
            raise ValueError('Exact preview target changed')

    def start(self):
        self.thread=threading.Thread(target=self.http.serve_forever,name='private-subtitle-preview',daemon=True)
        self.thread.start();return self

    def close(self):
        if self.thread:
            self.http.shutdown();self.thread.join(timeout=5)
        self.http.server_close()

    def manifest(self):
        return {'target':self.target,'assets':{name:{'path':str(entry[0]),'size':entry[1],'sha256':entry[3]} for name,entry in self.assets.items()},
                'private_loopback':True,'default_changed':False,'actual_listening':False,
                'continuous_picture_review':False,'accepted':False}


def prepare_clip(db,sid,ep,task_hash,ffmpeg,ffprobe,output,start=30,duration=10):
    """Generate a QA-only decode/reencode clip with retained original subtitle bytes.

    Requested clip offset is a localization candidate, not sound/boundary evidence.
    The original media and original encoding are preserved; derivatives cannot be
    selected as viewing mothers through this module.
    """
    target=verified_target(db,sid,ep,task_hash)
    if start<0 or not 1<=duration<=120:raise ValueError('Invalid bounded preview interval')
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    def run(args):
        result=subprocess.run(args,capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        logs=list(output.glob('tool-*.stderr.log'))
        (output/f'tool-{len(logs)+1:02d}.stderr.log').write_bytes(result.stderr)
        if result.returncode:raise ValueError('Preview tool failed; diagnostic stderr retained locally')
        return result.stdout
    probe=json.loads(run([str(ffprobe),'-v','error','-show_streams','-show_format','-of','json',target['path']]))
    if start+duration>float(probe.get('format',{}).get('duration') or 0):raise ValueError('Preview interval exceeds actual media duration')
    japanese=[s for s in probe['streams'] if s['codec_type']=='audio' and s.get('tags',{}).get('language') in ('jpn','ja')]
    chinese=[s for s in probe['streams'] if s['codec_type']=='subtitle' and s.get('tags',{}).get('language') in ('chi','zho','zh') and s.get('codec_name') in ('ass','ssa')]
    if len(japanese)!=1 or not chinese:raise ValueError('Unique labelled Japanese audio and actual Chinese ASS are required')
    original=output/'original-chinese.ass'; video=output/'video.mp4'
    if original.exists() or video.exists():raise ValueError('Preview artifacts are immutable; use a new run directory')
    run([str(ffmpeg),'-nostdin','-v','error','-i',target['path'],'-map',f"0:{chinese[0]['index']}",'-c:s','copy',str(original)])
    run([str(ffmpeg),'-nostdin','-v','error','-ss',str(start),'-i',target['path'],'-t',str(duration),'-map','0:v:0','-map',f"0:{japanese[0]['index']}",'-sn','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(video)])
    result={'target':target,'requested_start_seconds':start,'requested_duration_seconds':duration,
            'audio_index':japanese[0]['index'],'audio_language_label':'jpn','actual_audio_language_listened':False,
            'chinese_index':chinese[0]['index'],'original_ass_sha256':digest(original),'video_sha256':digest(video),
            'encoding':'QA-only H264/AAC derivative, original file untouched','timeline_mapping':'requested offset only, unaccepted',
            'actual_listening':False,'continuous_picture_review':False,'accepted':False}
    if verified_target(db,sid,ep,task_hash)!=target:raise ValueError('Media changed during clip preparation')
    (output/'clip-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
