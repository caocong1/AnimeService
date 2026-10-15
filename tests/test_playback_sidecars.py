import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from anime.db import Store
from anime.releases import bdecode
from anime.subtitles import Subtitles
from test_reliability import encode


@pytest.fixture
def package(tmp_path):
    db = Store(tmp_path / 'test.db')
    video = tmp_path / 'Example' / 'Example S01E02.mkv'
    video.parent.mkdir()
    video.write_bytes(b'media')
    sidecar = video.with_suffix('.sc.ass')
    body = b'[Script Info]\nScriptType: v4.00+\n[Events]\n'
    sidecar.write_bytes(body)
    raw = encode({b'info': {b'name': b'Example', b'piece length': 16384, b'pieces': b'x'*20,
        b'files': [{b'path': [video.name.encode()], b'length': 5},
                   {b'path': [sidecar.name.encode()], b'length': len(body)}]}})
    h, files = bdecode(raw)
    (tmp_path / 'torrents').mkdir()
    (tmp_path / 'torrents' / (h+'.torrent')).write_bytes(raw)
    selection = {'files': files, 'ids': [0, 1], 'selected': {'2': files[0]}}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose) VALUES(?,1,'AI翻译简中','complete',?,?,'episode')",
               (h, json.dumps(selection), str(tmp_path)))
    db.execute("INSERT INTO episodes(show_id,episode,hash,path,status,size) VALUES(1,2,?,?,'complete',5)", (h, str(video)))
    subtitles = Subtitles(db)
    subtitles.run = Mock(return_value=b'{"streams":[]}')
    subtitles.tool = Mock(return_value='fake-ffmpeg')
    return db, subtitles, {'path': str(video), 'size': 5, 'show_id': 1, 'episode': 2}, sidecar


def test_manifest_bound_sidecar_extract_and_cache_invalidates(package):
    db, subtitles, item, sidecar = package
    track = subtitles.tracks(item)[0]
    assert track['origin'] == 'sidecar' and track['rank'] == 0
    assert track['label'] == '简体中文 · AI翻译' and '_path' not in track
    first = subtitles.extract(item, track['index'], 'ass')
    assert first.read_bytes() == sidecar.read_bytes()
    body = sidecar.read_bytes().replace(b'v4.00+', b'v4.01+')
    sidecar.write_bytes(body)
    second = subtitles.extract(item, track['index'], 'ass')
    assert first != second and second.read_bytes() == body
    assert not db.rows('SELECT * FROM watches')


@pytest.mark.parametrize('failure', ['missing', 'size', 'unselected', 'wrong_media', 'foreign_manifest', 'incomplete', 'other_episode'])
def test_unbound_or_changed_sidecar_never_exposed(package, failure):
    db, subtitles, item, sidecar = package
    if failure == 'missing': sidecar.unlink()
    elif failure == 'size': sidecar.write_bytes(b'partial')
    elif failure == 'wrong_media': item['path'] = str(sidecar)
    elif failure == 'other_episode': item['episode'] = 1
    elif failure == 'incomplete': db.execute("UPDATE tasks SET status='downloading'")
    elif failure == 'foreign_manifest': next((db.path.parent/'torrents').iterdir()).write_bytes(b'foreign')
    else:
        row = db.one('SELECT selection FROM tasks'); selection = json.loads(row['selection'])
        selection['ids'] = [0]; db.execute('UPDATE tasks SET selection=?', (json.dumps(selection),))
    assert not subtitles.sidecars(item)


def test_sidecar_vtt_uses_sidecar_input_without_video_mapping(package):
    _, subtitles, item, sidecar = package
    track = subtitles.tracks(item)[0]
    subtitles.run.return_value = b'WEBVTT\n\n00:00.000 --> 00:01.000\nChinese\n'
    target = subtitles.extract(item, track['index'])
    args = subtitles.run.call_args.args[0]
    assert args[args.index('-i')+1] == str(sidecar)
    assert args[args.index('-map')+1] == '0:0'
    assert target.read_bytes().startswith(b'WEBVTT')
