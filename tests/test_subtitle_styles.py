import hashlib
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store
from anime.subtitles import Subtitles


@pytest.fixture
def styled(tmp_path, monkeypatch):
    db = Store(tmp_path / 'test.db')
    media = tmp_path / 'styled.mkv'
    media.write_bytes(b'fake media')
    db.execute("INSERT INTO shows(id,title) VALUES(1,'Test')")
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(1,1,?,'complete',10)", (str(media),))
    key = hashlib.sha256(str(media).lower().encode()).hexdigest()[:32]
    ass = b'[Script Info]\nPlayResX: 1920\n[V4+ Styles]\n[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\pos(500,80)\\fad(100,200)\\b1}Styled\n'
    font = b'OTTO' + b'font data'
    calls = []
    def run(self, args, timeout):
        calls.append(args)
        if args[0] == 'ffprobe':
            if args[args.index('-select_streams') + 1] == 't':
                return json.dumps({'streams': [
                    {'index': 8, 'codec_name': 'otf', 'extradata_size': len(font), 'tags': {'filename': '../../bad.otf'}},
                    {'index': 9, 'codec_name': 'png', 'extradata_size': 20}]}).encode()
            return json.dumps({'streams': [
                {'index': 2, 'codec_name': 'ass', 'tags': {'language': 'chi'}},
                {'index': 3, 'codec_name': 'subrip', 'tags': {'language': 'eng'}}]}).encode()
        if any(a.startswith('-dump_attachment:') for a in args):
            return font
        return ass if args[args.index('-f') + 1] == 'ass' else b'WEBVTT\n\n00:01.000 --> 00:03.000\n<b>Styled</b>\n'
    monkeypatch.setattr(Subtitles, 'tool', lambda self, name: name)
    monkeypatch.setattr(Subtitles, 'run', run)
    engine = Mock()
    client = TestClient(create_app(db, engine, start_worker=False), base_url='http://127.0.0.1:4871')
    return db, client, key, ass, font, calls, engine


def test_original_ass_and_fonts_are_scoped_and_read_only(styled):
    db, client, key, ass, font, calls, engine = styled
    base = f'/api/web/media/{key}'
    tracks = client.get(base + '/subtitles').json()['tracks']
    assert tracks[0]['ass_url'].endswith('/2.ass') and tracks[1]['ass_url'] is None
    assert client.get(tracks[0]['ass_url']).content == ass
    assert client.get(tracks[0]['url']).content.startswith(b'WEBVTT')
    assert client.get(base + '/subtitles/3.ass').status_code == 400
    assert client.get(base + '/subtitles/999.ass').status_code == 400
    fonts = client.get(base + '/subtitle-fonts').json()['fonts']
    assert fonts == [base + '/subtitle-fonts/8']
    assert client.get(fonts[0]).content == font
    assert client.get(fonts[0]).content == font
    assert len([a for a in calls if '-dump_attachment:8' in a]) == 1
    assert client.get(base + '/subtitle-fonts/9').status_code == 400
    assert client.get('/api/web/media/unregistered/subtitle-fonts/8').status_code == 404
    assert all('../../bad.otf' not in a for a in calls)
    assert not db.rows('SELECT * FROM watches') and not db.rows('SELECT * FROM web_progress')
    assert not engine.mock_calls


def test_wasm_compilation_is_scoped_to_local_subtitle_assets(styled):
    _, client, _, _, _, _, _ = styled
    assert 'wasm-unsafe-eval' not in client.get('/watch').headers['content-security-policy']
    csp = client.get('/static/vendor/libass/subtitles-octopus-worker.js').headers['content-security-policy']
    assert "'wasm-unsafe-eval'" in csp and "'unsafe-eval'" not in csp
