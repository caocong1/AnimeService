import hashlib
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from anime.app import create_app
from anime.clips import Clips, command, options
from anime.db import Store
from anime.subtitles import Subtitles
from anime.webplayer import WebPlayer


INFO = dict(duration=120, width=1920, height=1080, video_index=0,
            audio_tracks=[dict(index=1, default=True), dict(index=2, default=False)])


@pytest.mark.parametrize('patch', [dict(start=-1), dict(end=121), dict(end=0), dict(start=float('nan')),
    dict(start=True), dict(width=63), dict(width=640.5), dict(fps=61), dict(audio='yes'),
    dict(audio_index=99), dict(format='webm'), dict(quality='bad'), dict(path='C:/secret'),
    dict(crop={'x':1800,'y':0,'width':200,'height':300}),
    dict(crop={'x':0,'y':0,'width':10.5,'height':30}), dict(format='gif',end=31,fps=15,width=640)])
def test_reject_invalid_bounds_and_untrusted_options(patch):
    with pytest.raises(ValueError):
        options(dict(start=0, end=10, **patch) if 'start' not in patch and 'end' not in patch else {'start':0,'end':10,**patch}, INFO)


def test_crop_even_dimensions_default_selected_audio_and_gif():
    p = options(dict(start=1.125,end=11.125,crop=dict(x=1,y=2,width=301,height=101),width=201,audio_index=2),INFO)
    assert (p['width'],p['height']) == (200,66)
    args = command('ffmpeg',dict(path='a file.mkv'),INFO,p,Path('out.mp4'))
    assert args[args.index('-ss')+1] == '1.125'
    # Palette generation must stop at the requested input boundary rather than
    # buffering / decoding the rest of a full episode before it can emit GIF.
    assert args.index('-t') < args.index('-i')
    assert '0:2' in args and 'crop=301:101:1:2:exact=1' in args[args.index('-vf')+1]
    assert '-map_metadata' in args and args[-1] == 'out.mp4'
    gif = options(dict(start=0,end=10,format='gif',width=640,fps=15,audio=True),INFO)
    assert not gif['audio']
    args = command('ffmpeg',dict(path='a.mkv'),INFO,gif,Path('out.gif'))
    assert 'palettegen' in args[args.index('-filter_complex')+1] and '-an' in args
    gif['loop']=False
    args=command('ffmpeg',dict(path='a.mkv'),INFO,gif,Path('out.gif'))
    assert args[args.index('-loop')+1]=='-1'


@pytest.fixture
def movie(tmp_path):
    db = Store(tmp_path/'test.db'); tool = Subtitles(db)
    ffmpeg = tool.tool('ffmpeg'); source = tmp_path/'sample.mp4'
    subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3',
                    '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True,
                   capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    db.execute("INSERT INTO shows(id,title) VALUES(1,'Clip fixture')")
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(1,1,?,'complete',?)",(str(source),source.stat().st_size))
    key = hashlib.sha256(str(source).lower().encode()).hexdigest()[:32]
    return db, key, source


def wait(clips,key,jid):
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        job = clips.status(key,jid)
        if job['state'] not in ('queued','running','cancelling'):
            return job
        time.sleep(.05)
    raise AssertionError('clip export timed out')


@pytest.mark.parametrize('fmt,audio', [('mp4',True),('mp4',False),('gif',True)])
def test_real_export_bounds_resize_audio_and_original_preserved(movie,fmt,audio):
    db,key,source = movie; before = hashlib.sha256(source.read_bytes()).hexdigest()
    player = WebPlayer(db); clips = Clips(player)
    job = clips.start(key,dict(start=.5,end=1.5,format=fmt,audio=audio,width=160,fps=10,
                               crop=dict(x=20,y=30,width=200,height=100)))
    result = wait(clips,key,job['id']); assert result['state'] == 'complete'
    target,_ = clips.file(key,job['id'])
    probe = json.loads(player.subtitles.run([player.subtitles.tool('ffprobe'),'-v','error','-show_streams','-show_format','-of','json',str(target)],10))
    stream = next(s for s in probe['streams'] if s['codec_type']=='video')
    assert (stream['width'],stream['height']) == (160,80)
    assert float(probe['format']['duration']) == pytest.approx(1,abs=.12)
    assert any(s['codec_type']=='audio' for s in probe['streams']) == (fmt=='mp4' and audio)
    if fmt=='mp4': assert stream['codec_name']=='h264'
    # Compare first exported pixels with the requested source timestamp and region,
    # allowing the extra lossy encode / GIF palette quantization.
    tool=player.subtitles.tool('ffmpeg')
    raw_args=['-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1']
    expected=player.subtitles.run([tool,'-v','error','-ss','0.5','-i',str(source),'-vf','crop=200:100:20:30,scale=160:80:flags=lanczos,fps=10']+raw_args,10)
    actual=player.subtitles.run([tool,'-v','error','-i',str(target)]+raw_args,10)
    assert len(expected)==len(actual)==160*80*3
    assert sum(abs(a-b) for a,b in zip(actual,expected))/len(actual)<10
    if fmt=='gif': assert b'NETSCAPE2.0' in target.read_bytes()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert db.rows('SELECT * FROM watches') == db.rows('SELECT * FROM web_progress') == []
    assert not list(clips.root.glob('*.partial.*'))
    restored = Clips(player); assert restored.status(key,job['id'])['state']=='complete'
    assert restored.file(key,job['id'])[0] == target
    with pytest.raises(HTTPException): clips.file('other-media',job['id'])
    clips.close()


def test_cancel_stalled_process_and_no_partial_delivered(movie,monkeypatch):
    db,key,_ = movie; player = WebPlayer(db); clips = Clips(player)
    clips.probe(player.item(key))
    # Real long-lived process writes no output: cancellation must unblock reader.
    import sys
    real_popen = subprocess.Popen
    def spawn(args, **kwargs):
        Path(args[-1]).write_bytes(b'partial')
        return real_popen([sys.executable,'-c','import time; time.sleep(60)'],**kwargs)
    monkeypatch.setattr('anime.clips.subprocess.Popen',spawn)
    job = clips.start(key,dict(start=0,end=1))
    with pytest.raises(HTTPException): clips.start(key,dict(start=0,end=1))
    clips.cancel(key,job['id']); assert wait(clips,key,job['id'])['state']=='cancelled'
    with pytest.raises(HTTPException): clips.file(key,job['id'])
    assert not list(clips.root.glob('*.partial.*'))
    clips.close()


def test_api_auth_media_binding_blacklist_and_readonly(movie,monkeypatch):
    db,key,source = movie; engine = Mock(); app = create_app(db,engine,start_worker=False)
    with TestClient(app,base_url='http://127.0.0.1:4871') as client:
        assert client.post(f'/api/web/media/{key}/clips',json={'start':0,'end':1}).status_code == 403
        token = client.get('/api/bootstrap').json()['token']
        headers={'X-Anime-Token':token,'Origin':'http://127.0.0.1:4871'}
        assert client.get('/api/web/media/not-registered/clips').status_code==404
        assert client.get(f'/api/web/media/{key}/clips').json()['duration']==3
        result=client.post(f'/api/web/media/{key}/clips',headers=headers,json={'start':.2,'end':1,'audio':False})
        assert result.status_code==200
        jid=result.json()['id']; assert 'path' not in result.text
        assert wait(app.state.webplayer.clips,key,jid)['state']=='complete'
        route=f'/api/web/media/{key}/clips/{jid}/file'
        assert client.get(route).headers['content-type']=='video/mp4'
        assert client.get(route+'?download=true').headers['content-disposition'].startswith('attachment')
        assert client.get(f'/api/web/media/{key}/clips/invalid').status_code==404
        monkeypatch.setattr('anime.webplayer.media_filter',lambda db: lambda row: True)
        assert client.get(route).status_code==404
        assert client.get(f'/api/web/media/{key}/clips/{jid}').status_code==404
    assert db.rows('SELECT * FROM watches') == db.rows('SELECT * FROM web_progress') == []
    assert source.is_file(); engine.submit.assert_not_called()


def test_restart_interrupts_unfinished_and_rejects_changed_source(movie):
    db,key,source=movie; clips=Clips(WebPlayer(db)); clips.root.mkdir(parents=True)
    jid='a'*32
    record=dict(id=jid,media_id=key,state='running',progress=42,name='test.mp4',created=0,options={})
    (clips.root/(jid+'.json')).write_text(json.dumps(record))
    assert clips.status(key,jid)['state']=='interrupted'
    with pytest.raises(HTTPException): clips.file(key,jid)
    source.write_bytes(b'changed')
    with pytest.raises(ValueError): clips.start(key,dict(start=0,end=1))
