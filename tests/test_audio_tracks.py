import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store
from anime.subtitles import Subtitles
from anime.audio_tracks import AudioTracks


@pytest.fixture
def audio(tmp_path, monkeypatch):
    db = Store(tmp_path / 'test.db')
    p = tmp_path / 'multi.mkv'; p.write_bytes(b'fixture')
    tools = Subtitles(db)
    monkeypatch.setattr(tools, 'tool', lambda name: name)
    streams = {'streams': [
        {'index': 0, 'codec_type': 'video', 'codec_name': 'hevc'},
        {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'ass'},
        {'index': 4, 'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
         'tags': {'language': 'cze'}, 'disposition': {'default': 1}},
        {'index': 7, 'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
         'tags': {'language': 'eng', 'title': '<English>'}},
        {'index': 8, 'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6},
        {'index': 9, 'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
         'tags': {'language': 'eng', 'title': 'Commentary'}},
    ], 'format': {'duration': '12'}}
    tools.run = Mock(return_value=json.dumps(streams).encode())
    item = {'path': str(p), 'size': p.stat().st_size}
    return AudioTracks(tools), item, db, streams


def test_audio_stream_ids_languages_and_defaults_are_not_assumed(audio):
    service, item, db, _ = audio
    before = db.rows('SELECT * FROM watches')
    d = service.probe(item)
    assert [t['index'] for t in d['tracks']] == [4, 7, 8, 9]
    assert d['tracks'][0]['default'] and '捷克语' in d['tracks'][0]['label']
    assert d['tracks'][1]['language'] == 'eng' and '英语' in d['tracks'][1]['label']
    assert '语言未标注' in d['tracks'][2]['label']
    assert 'Commentary' in d['tracks'][3]['label']
    assert service.probe(item) == d and service.tools.run.call_count == 1
    assert db.rows('SELECT * FROM watches') == before


@pytest.mark.parametrize('index', [0, 2, -1, 999])
def test_invalid_or_non_audio_index_cannot_prepare_or_stream(audio, index):
    service, item, _, _ = audio
    with pytest.raises(ValueError, match='不存在'):
        service.prepare(item, index)
    with pytest.raises(ValueError, match='不存在'):
        service.file(item, index)
    assert not service.cache.exists()


def test_source_changes_invalidate_probe_and_prepared_selection(audio):
    service, item, _, _ = audio
    key, _ = service.identity(item, 7)
    service.cache.mkdir(parents=True)
    (service.cache / f'{key}.mp4').write_bytes(b'cache')
    assert service.status(item, 7)['state'] == 'ready'
    stat = Path(item['path']).stat()
    os.utime(item['path'], ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert service.status(item, 7)['state'] == 'idle'
    Path(item['path']).write_bytes(b'changed size')
    with pytest.raises(ValueError, match='大小'):
        service.probe(item)


def test_evicted_ready_job_does_not_expose_missing_file(audio):
    service, item, _, _ = audio
    key, _ = service.identity(item, 7)
    service.jobs[key] = {'state': 'ready'}
    assert service.status(item, 7)['state'] == 'idle'
    with pytest.raises(ValueError, match='尚未'):
        service.file(item, 7)


def test_preparation_single_flight_and_error_retry(audio, monkeypatch):
    service, item, _, _ = audio
    monkeypatch.setattr('anime.audio_tracks.threading.Thread', Mock())
    assert service.prepare(item, 7)['state'] == 'preparing'
    assert service.prepare(item, 7)['state'] == 'preparing'
    with pytest.raises(ValueError, match='另一份'):
        service.prepare(item, 4)
    key, _ = service.identity(item, 7)
    service.jobs[key] = {'state': 'error'}
    assert service.prepare(item, 7)['state'] == 'preparing'


def test_cache_space_and_video_codec_gate_before_process(audio, monkeypatch):
    service, item, _, streams = audio
    monkeypatch.setattr(service, 'CACHE_LIMIT', 1)
    with pytest.raises(ValueError, match='上限'):
        service.prepare(item, 7)
    assert service.jobs == {}
    service.probes.clear(); streams['streams'][0]['codec_name'] = 'mpeg2video'
    service.tools.run.return_value = json.dumps(streams).encode()
    with pytest.raises(ValueError, match='编码'):
        service.prepare(item, 7)


@pytest.mark.parametrize('index,codec,bitrate', [(7, 'copy', None), (8, 'aac', '512k')])
def test_build_maps_only_requested_audio_and_copies_video(audio, monkeypatch, index, codec, bitrate):
    service, item, _, _ = audio
    key, data = service.identity(item, index)
    service.cache.mkdir(parents=True)
    commands = []
    def start(args, **kwargs):
        commands.append(args)
        Path(args[-1]).write_bytes(b'generated cache')
        return Mock(wait=Mock(return_value=0), poll=Mock(return_value=0))
    monkeypatch.setattr('anime.audio_tracks.subprocess.Popen', start)
    service.tools.run.return_value = json.dumps({'streams': [{'codec_type': 'audio'}, {'codec_type': 'video'}], 'format': {'duration': '12'}}).encode()
    service.build(item, index, key, data)
    args = commands[0]
    assert args[args.index('-c:v') + 1] == 'copy'
    assert args[args.index('-c:a') + 1] == codec
    assert args.count('-map') == 2 and f'0:{index}' in args and '0:v:0' in args
    assert '-tag:v' in args and 'hvc1' in args
    if bitrate:
        assert args[args.index('-b:a') + 1] == bitrate
    assert service.file(item, index).read_bytes() == b'generated cache'
    assert Path(item['path']).read_bytes() == b'fixture'


def test_failure_removes_partial_and_hides_tool_error(audio, monkeypatch):
    service, item, _, _ = audio
    key, data = service.identity(item, 7)
    service.cache.mkdir(parents=True)
    def start(args, **kwargs):
        Path(args[-1]).write_bytes(b'partial')
        raise OSError('private file path and token must not leak')
    monkeypatch.setattr('anime.audio_tracks.subprocess.Popen', start)
    service.build(item, 7, key, data)
    state = service.status(item, 7)
    assert state['state'] == 'error' and 'private' not in state['message']
    assert not list(service.cache.glob('*.mp4'))


def test_audio_api_auth_range_and_no_watching_mutation(audio, monkeypatch):
    service, item, db, _ = audio
    db.upsert_subject({'id': 1, 'name': 'Fixture'})
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(1,1,?,?,'complete')", (item['path'], item['size']))
    app = create_app(db, Mock(), start_worker=False)
    app.state.webplayer.audio = service
    client = TestClient(app, base_url='http://127.0.0.1:4871')
    key = hashlib.sha256(item['path'].lower().encode()).hexdigest()[:32]
    base = f'/api/web/media/{key}/audio-tracks'
    before = db.show(1)
    assert client.get(base).json()['tracks'][1]['language'] == 'eng'
    assert client.post(base + '/7/prepare', json={}).status_code == 403
    headers = {'X-Anime-Token': client.get('/api/bootstrap').json()['token'], 'Origin': 'http://127.0.0.1:4871'}
    assert client.post(base + '/0/prepare', json={}, headers=headers).status_code == 400
    target_key, _ = service.identity(item, 7)
    service.cache.mkdir(parents=True); (service.cache / f'{target_key}.mp4').write_bytes(b'0123456789')
    assert client.post(base + '/7/prepare', json={}, headers=headers).json()['state'] == 'ready'
    r = client.get(base + '/7/stream', headers={'Range': 'bytes=2-5'})
    assert r.status_code == 206 and r.content == b'2345'
    assert r.headers['content-type'] == 'video/mp4'
    assert client.get('/api/web/media/unregistered/audio-tracks').status_code == 404
    assert db.show(1) == before and db.rows('SELECT * FROM watches') == []
    assert db.rows('SELECT * FROM web_progress') == []
