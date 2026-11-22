import hashlib
import json
from unittest.mock import Mock

import pytest

from anime.db import Store
from anime.playback_subtitle_selection import INDEX, fingerprint, media_key, register, selected_sidecars
from anime.subtitles import Subtitles


ASS = '[Script Info]\nScriptType: v4.00+\n[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,这是实际的中文字幕，不是英文轨道。\n'.encode()


@pytest.fixture
def selection(tmp_path):
    db = Store(tmp_path / 'test.db')
    video = tmp_path / 'Episode05.mkv'
    video.write_bytes(b'unchanged media')
    source = tmp_path / 'Nix-E05.sc.ass'
    source.write_bytes(ASS)
    item = {'path': str(video), 'size': video.stat().st_size, 'show_id': 1, 'episode': 5}
    args = {'expected_fingerprint': fingerprint(item), 'expected_subtitle_sha256': hashlib.sha256(ASS).hexdigest(),
            'label': '简体中文 · Nix-Raws', 'source_url': 'https://mikanani.me/Home/Episode/source',
            'evidence': '同集文件清单及开头、中段、结尾的实际字幕/画面同步核对'}
    tools = Subtitles(db)
    tools.run = Mock(return_value=b'{"streams":[{"index":2,"codec_name":"ass","tags":{"language":"eng"}}]}')
    tools.tool = Mock(return_value='ffmpeg')
    return db, item, source, args, tools


def test_plan_is_read_only_and_apply_only_adds_verified_playback_source(selection):
    db, item, source, args, tools = selection
    before = db.rows('SELECT * FROM watches'), db.rows('SELECT * FROM episodes')
    assert register(db, item, source, **args)['state'] == 'planned'
    assert not (db.path.parent / 'playback-subtitles').exists()
    assert register(db, item, source, **args, apply=True)['state'] == 'selected'
    tracks = tools.tracks(item)
    assert [t['index'] for t in tracks] == [INDEX, 2]
    assert tracks[0]['origin'] == 'selected' and '_path' not in tracks[0]
    assert tools.extract(item, INDEX, 'ass').read_bytes() == ASS
    assert (db.rows('SELECT * FROM watches'), db.rows('SELECT * FROM episodes')) == before


def test_selected_subtitle_cannot_cross_video_or_episode(selection):
    db, item, source, args, _ = selection
    register(db, item, source, **args, apply=True)
    with pytest.raises(ValueError, match='集号'):
        selected_sidecars(db, {**item, 'episode': 6})
    video = source.with_name('Different.mkv')
    video.write_bytes(b'unchanged media')
    assert selected_sidecars(db, {**item, 'path': str(video)}) == []
    original = source.with_name('Episode05.mkv')
    original.write_bytes(b'changed content')
    assert selected_sidecars(db, {**item, 'size': original.stat().st_size}) == []


def test_tampered_subtitle_rejected_even_after_cache_created(selection):
    db, item, source, args, tools = selection
    register(db, item, source, **args, apply=True)
    tools.extract(item, INDEX, 'ass')
    stored = selected_sidecars(db, item)[0]['_path']
    from pathlib import Path
    Path(stored).write_bytes(ASS.replace('实际'.encode(), '篡改'.encode()))
    with pytest.raises(ValueError, match='校验失败'):
        tools.extract(item, INDEX, 'ass')


def test_changed_manifest_rejected_instead_of_silently_loading_english(selection):
    db, item, source, args, tools = selection
    register(db, item, source, **args, apply=True)
    path = db.path.parent / 'playback-subtitles/selections' / (media_key(item) + '.json')
    record = json.loads(path.read_text(encoding='utf-8'))
    record['subtitle_sha256'] = '../../outside'
    path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='登记无效'):
        tools.tracks(item)


def test_conflicting_selection_requires_expected_sha_and_preserves_history(selection):
    db, item, source, args, _ = selection
    register(db, item, source, **args, apply=True)
    manifest = db.path.parent / 'playback-subtitles/selections' / (media_key(item) + '.json')
    previous = manifest.read_bytes()
    source.write_bytes(ASS.replace('实际'.encode(), '新的'.encode()))
    args['expected_subtitle_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='已有另一'):
        register(db, item, source, **args, apply=True)
    result = register(db, item, source, **args, apply=True,
                      expected_selection_sha256=hashlib.sha256(previous).hexdigest())
    assert result['state'] == 'selected'
    archive = db.path.parent / 'playback-subtitles/history'
    assert (archive / (hashlib.sha256(previous).hexdigest() + '.json')).read_bytes() == previous


def test_same_selection_is_idempotent_and_keeps_original_timestamp(selection):
    db, item, source, args, _ = selection
    first = register(db, item, source, **args, apply=True)
    second = register(db, item, source, **args, apply=True)
    assert second['state'] == 'already_selected'
    assert second['selection'] == first['selection']


@pytest.mark.parametrize('failure', ['source_sha', 'media_fingerprint', 'no_chinese', 'untimed', 'existing_lock'])
def test_invalid_source_or_concurrent_registration_does_not_activate(selection, failure):
    db, item, source, args, _ = selection
    if failure == 'source_sha':
        args['expected_subtitle_sha256'] = '0' * 64
    elif failure == 'media_fingerprint':
        args['expected_fingerprint'] = '0' * 64
    elif failure in ('no_chinese', 'untimed'):
        raw = b'[Script Info]\n[Events]\nDialogue: English only' if failure == 'no_chinese' else '只有中文没有任何字幕定时事件的文件。'.encode()
        source.write_bytes(raw)
        args['expected_subtitle_sha256'] = hashlib.sha256(raw).hexdigest()
    else:
        root = db.path.parent / 'playback-subtitles'
        root.mkdir()
        (root / 'selection.lock').write_text('another writer')
    with pytest.raises((ValueError, FileExistsError)):
        register(db, item, source, **args, apply=True)
    assert selected_sidecars(db, item) == []


def test_srt_fallback_reads_imported_text_and_keeps_embedded_track(selection):
    db, item, source, args, tools = selection
    source = source.with_suffix('.srt')
    source.write_text('1\n00:00:01,000 --> 00:00:03,000\n这是实际的中文字幕，不是英文轨道。\n', encoding='utf-8')
    args['expected_subtitle_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    register(db, item, source, **args, apply=True)
    tools.run.return_value = b'WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nChinese\n'
    tools.embedded_tracks = Mock(return_value=[{'index': 2, 'supported': True, 'rank': 2}])
    assert tools.extract(item, INDEX).read_bytes().startswith(b'WEBVTT')
    command = tools.run.call_args.args[0]
    assert command[command.index('-i') + 1].endswith('.srt')
    assert command[command.index('-map') + 1] == '0:0'
