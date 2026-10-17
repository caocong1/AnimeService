"""Real ffmpeg/fpcalc verification with generated audio and local HLS fixtures only."""
import json
import subprocess
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from anime import audio_alignment as audio
from anime import bahamut_alignment as baha
from anime.db import Store
from anime.subtitles import Subtitles


def main():
    root = Path(__file__).resolve().parents[1]
    out = root/'data/diagnostics/bahamut-alignment-20261016'
    out.mkdir(parents=True, exist_ok=True)
    db = Store(out/'fixture.db')
    ffmpeg = Subtitles(db).tool('ffmpeg')
    ffprobe = Subtitles(db).tool('ffprobe')
    fpcalc = root/'tools/chromaprint/fpcalc.exe'
    rate, duration, offset = 11025, 600, 12
    rng = np.random.default_rng(16)
    count = duration*rate
    t = np.arange(count)/rate
    notes = rng.integers(42, 87, duration*4+1)
    freq = 440*2**((notes[np.minimum((t*4).astype(int),len(notes)-1)]-69)/12)
    phase = np.cumsum(freq)*2*np.pi/rate
    signal = (.4*np.sin(phase)+.2*np.sin(2*phase)+.1*np.sin(3*phase))
    signal *= .6+.4*np.sin(2*np.pi*2*t)**2
    pcm = (signal*20000).astype('<i2').tobytes()
    def wav(path, data):
        with wave.open(str(path),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes(data)
    wav(out/'source.wav',pcm)
    wav(out/'local.wav',b'\0'*(offset*rate*2)+pcm)
    subprocess.run([ffmpeg,'-nostdin','-v','error','-y','-i',str(out/'source.wav'),
        '-c:a','aac','-b:a','64k','-f','hls','-hls_time','6','-hls_playlist_type','vod',
        '-hls_segment_filename',str(out/'part%03d.ts'),str(out/'index.m3u8')],
        check=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    original = baha.BahamutReference
    def reference(url, check):
        ref = original(url,check)
        ref.proxy_dns = False
        ref.api = lambda url: ({'deviceid':'synthetic'} if 'getdeviceid.php' in url else {'time':1} if 'token.php' in url else
            {'data':{'srcUseCases':[{'src':{'playlist':'https://bahamut.akamaized.net/fixture/index.m3u8'}}]}})
        def read(url,*args,**kwargs):
            check()
            name = Path(urlsplit(url).path).name
            assert name=='index.m3u8' or name.startswith('part') and name.endswith('.ts')
            data = (out/name).read_bytes();ref.downloaded+=len(data)
            if ref.downloaded>baha.LIMIT: raise audio.Unavailable('budget')
            return data
        ref.read = read
        return ref
    baha.BahamutReference = reference
    baha.validate_public_host = lambda _:None
    service=audio.AlignmentService(out/'cache',ffmpeg,ffprobe,fpcalc)
    local=out/'local.wav'
    result=service.start({'path':str(local),'size':local.stat().st_size},
                         'https://ani.gamer.com.tw/animeVideo.php?sn=1','synthetic',force=True)
    until=time.monotonic()+120
    while result.get('status') in audio.ACTIVE and time.monotonic()<until:
        time.sleep(.1);result=service.status(result['job_id'])
    receipt={'input':'generated audio; local HLS; no production/API writes or real source requests',
             'expected_offset':offset,'result':result}
    (out/'real-decode.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps(receipt))
    assert result['status']=='matched',result
    assert abs(result['offset']-offset)<.2,result


if __name__=='__main__':main()
