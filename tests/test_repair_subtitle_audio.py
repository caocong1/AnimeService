import json
import urllib.error

import pytest

from scripts import repair_subtitle_audio as audio


def private(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, 'ROOT', tmp_path)
    return tmp_path / 'data/subtitle-studio/audio'


def unavailable(_config):
    raise urllib.error.URLError('refused')


def test_healthy_service_never_spawns_or_validates_install(tmp_path, monkeypatch):
    out = private(tmp_path, monkeypatch)
    monkeypatch.setattr(audio, 'inspect', lambda c: {'capabilities': ['audio']})
    monkeypatch.setattr(audio, 'validate', lambda c: pytest.fail('healthy service must be left alone'))
    monkeypatch.setattr(audio.subprocess, 'Popen', lambda *a, **k: pytest.fail('duplicate runtime'))
    assert audio.repair({}, out, True)['started'] is False


def test_prior_live_unconfirmed_launch_is_not_retried(tmp_path, monkeypatch):
    out = private(tmp_path, monkeypatch)
    out.mkdir(parents=True)
    (out / 'start.json').write_text(json.dumps({'pid': 123, 'started_at': 100}))
    monkeypatch.setattr(audio, 'inspect', unavailable)
    class Process:
        def __init__(self, pid): assert pid == 123
        def create_time(self): return 99
    monkeypatch.setattr(audio.psutil, 'Process', Process)
    monkeypatch.setattr(audio.subprocess, 'Popen', lambda *a, **k: pytest.fail('duplicate runtime'))
    with pytest.raises(RuntimeError, match='still exists'):
        audio.repair({}, out, True)


def test_spawn_gap_intent_remains_blocked_without_receipt(tmp_path, monkeypatch):
    out = private(tmp_path, monkeypatch)
    out.mkdir(parents=True)
    (out / 'launch-intent.json').write_text('{"requested_at":100}')
    monkeypatch.setattr(audio, 'inspect', unavailable)
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def setsockopt(self, *a): pass
        def bind(self, addr): assert addr == ('127.0.0.1', 11436)
    monkeypatch.setattr(audio.socket, 'socket', Socket)
    monkeypatch.setattr(audio, 'validate', lambda c: (tmp_path / 'ollama.exe', tmp_path / 'models'))
    monkeypatch.setattr(audio.subprocess, 'Popen', lambda *a, **k: pytest.fail('unknown launch must stay held'))
    with pytest.raises(FileExistsError): audio.repair({}, out, True)


def test_changed_executable_is_not_launched(tmp_path):
    exe = tmp_path / 'ollama.exe'
    exe.write_bytes(b'changed')
    with pytest.raises(ValueError, match='runtime changed'):
        audio.validate({'executable': str(exe), 'models': str(tmp_path), 'executable_sha256': 'old'})


def test_nonprivate_output_is_rejected(tmp_path, monkeypatch):
    private(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='private subtitle archive'):
        audio.repair({}, tmp_path / 'outside', True)
