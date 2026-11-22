import contextlib
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts import mac_subtitle_media_worker as mac
from scripts import probe_subtitle_decode as decode
from scripts import subtitle_remote_media as client


def test_stage_reuses_verified_legacy_source_and_pins_active_job(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mac, 'BASE', tmp_path)
    monkeypatch.setattr(mac, 'locked', lambda p: contextlib.nullcontext())
    payload = b'same frozen source' * 12
    digest = hashlib.sha256(payload).hexdigest()
    legacy = tmp_path / ('render-' + '1'*32)
    legacy.mkdir()
    (legacy / 'source.media').write_bytes(payload)
    job = tmp_path / ('probe-' + '2'*32)
    job.mkdir()
    mac.stage(job, digest, len(payload), False)
    assert capsys.readouterr().out.strip() == 'linked'
    assert (job / 'source.media').samefile(legacy / 'source.media')
    (legacy / 'source.media').unlink()
    (tmp_path / 'source-cache' / digest).unlink()
    assert (job / 'source.media').read_bytes() == payload


def test_cached_episode_attaches_while_another_upload_holds_global_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mac, 'BASE', tmp_path)
    def busy(_):
        raise AssertionError('verified hit waited for an unrelated upload')
    monkeypatch.setattr(mac, 'locked', busy)
    cache = tmp_path / 'source-cache'
    cache.mkdir()
    payload = b'complete movie'
    expected = hashlib.sha256(payload).hexdigest()
    (cache / expected).write_bytes(payload)
    job = tmp_path / 'job'
    job.mkdir()
    mac.stage(job, expected, len(payload), False)
    assert capsys.readouterr().out.strip() == 'linked'
    assert (job / 'source.media').samefile(cache / expected)


def test_upload_corruption_and_partial_input_leave_no_cache_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(mac, 'BASE', tmp_path)
    monkeypatch.setattr(mac, 'locked', lambda p: contextlib.nullcontext())
    source = b'frozen source'
    digest = hashlib.sha256(source).hexdigest()
    job = tmp_path / ('probe-' + '2'*32)
    job.mkdir()
    for payload in (source[:-1], b'x' * len(source), source+b'x', source):
        stream = type('Input', (), {'buffer': io.BytesIO(payload)})()
        monkeypatch.setattr(mac.sys, 'stdin', stream)
        if payload == source:
            mac.stage(job, digest, len(source), True)
            assert (job/'source.media').read_bytes() == source
        else:
            with pytest.raises(ValueError):
                mac.stage(job, digest, len(source), True)
            assert not (job/'source.media').exists()
            assert list((tmp_path/'source-cache').iterdir()) == []


def test_configured_decode_uses_mac_even_for_legacy_cuda_call(tmp_path, monkeypatch):
    video = tmp_path/'source'
    video.write_bytes(b'bound')
    target = str(video), 'a'*64, 5, video.stat().st_mtime_ns
    monkeypatch.setattr(decode, 'bound_media', lambda *args: (target, 'ffmpeg', 'ffprobe'))
    monkeypatch.setattr(decode.subprocess, 'check_output', lambda *a,**k: b'{"format":{"duration":"7"}}')
    (tmp_path/'subtitle-render-worker.json').write_text('{"host":"test-mac"}')
    calls = []
    @contextlib.contextmanager
    def prepare(host, source, expected, data, spec, guard):
        guard()
        calls.append((host, spec['mode']))
        if len(calls) == 1:
            raise OSError('offline')
        progress = tmp_path/'remote.progress'
        progress.write_text('out_time_us=7000000\nprogress=end\n')
        yield progress, {'host': host}
    monkeypatch.setattr(client,'preparation',prepare)
    args = tmp_path/'anime.db', 9, 3, 'cuda'
    with pytest.raises(OSError, match='offline'):
        decode.probe(*args)
    receipt = decode.probe(*args)
    assert decode.probe(*args) == receipt
    assert json.loads(receipt.read_text())['worker']['host'] == 'test-mac'
    assert calls == [('test-mac','decode')]*2


@pytest.mark.parametrize('entry', ['../extra.jpg', '/outside', 'frame-0001.png'])
def test_frame_transport_rejects_unexpected_members_or_sha(tmp_path, entry):
    spec = {'mode':'frames', 'timestamps':[0], 'format':'png', 'width':640}
    payload = b'file'
    manifest = json.dumps({'request':spec,'images':{entry:'0'*64}}).encode()
    archive = tmp_path/'frames.tar'
    with tarfile.open(archive,'w') as tar:
        for name, content in [('frames.json',manifest),(entry,payload)]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    out = tmp_path/'out'
    out.mkdir()
    with pytest.raises(ValueError):
        client.unpack_frames(archive, out, {'request':spec})
    assert not (tmp_path/'extra.jpg').exists()


def test_repeated_frames_require_matching_settings_owner_target_and_images(tmp_path):
    from scripts.probe_subtitle_frames import verified_existing
    spec = dict(mode='frames', timestamps=[.5], width=640, format='jpg')
    identity = dict(owner='original', target=['source', 'a'*64, 20, 1234], input='frozen')
    frame = tmp_path/'frame-0001.jpg'
    frame.write_bytes(b'actual validated bytes')
    receipt = tmp_path/'frames.json'
    receipt.write_text(json.dumps({**identity, 'request': {**spec, 'source_sha256':'b'*64},
                                   'images': {frame.name:mac.digest(frame)}}))
    assert verified_existing(tmp_path, spec, identity) == receipt
    for changed in ({**identity,'owner':'other'},{**identity,'target':['different']}):
        with pytest.raises(ValueError,match='does not match'):
            verified_existing(tmp_path, spec, changed)
    with pytest.raises(ValueError,match='does not match'):
        verified_existing(tmp_path, {**spec,'width':1280}, identity)
    frame.write_bytes(b'different')
    with pytest.raises(ValueError,match='frame changed'):
        verified_existing(tmp_path, spec, identity)


def test_same_size_corrupt_cache_is_replaced_without_touching_running_job(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mac, 'BASE', tmp_path)
    monkeypatch.setattr(mac, 'locked', lambda p: contextlib.nullcontext())
    data = b'source'
    expected = hashlib.sha256(data).hexdigest()
    cache = tmp_path/'source-cache'
    cache.mkdir()
    damaged = cache/expected
    damaged.write_bytes(b'broken')
    active = tmp_path/'active-source'
    active.hardlink_to(damaged)
    job = tmp_path/('probe-'+'3'*32)
    job.mkdir()
    mac.stage(job, expected, len(data), False)
    assert capsys.readouterr().out.strip() == 'missing'
    assert not damaged.exists() and not (job/'source.media').exists()
    monkeypatch.setattr(mac.sys,'stdin',type('Input',(),{'buffer':io.BytesIO(data)})())
    mac.stage(job, expected, len(data), True)
    assert (job/'source.media').read_bytes() == data and active.read_bytes() == b'broken'
