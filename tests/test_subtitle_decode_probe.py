import json
from pathlib import Path
import subprocess

import pytest

from scripts import probe_subtitle_decode as probe


def test_decode_reuses_complete_bound_result_and_rechecks_scope(tmp_path, monkeypatch):
    video = tmp_path / 'source.mkv'
    video.write_bytes(b'bound episode fixture')
    target = (str(video), 'a' * 64, video.stat().st_size, video.stat().st_mtime_ns)
    protected = False
    decoded = []
    end_seconds = 30
    def bound(*args):
        if protected:
            raise ValueError('scope changed')
        return target, 'ffmpeg', 'ffprobe'
    def fake_process(cmd, **kwargs):
        decoded.append(cmd)
        progress = Path(cmd[cmd.index('-progress') + 1])
        progress.write_text(f'out_time_us={end_seconds * 1000000}\nprogress=end\n')
        class Completed:
            returncode = 0
            def poll(self):
                return self.returncode
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
        return Completed()
    monkeypatch.setattr(probe, 'bound_media', bound)
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: b'{"format":{"duration":"30"}}')
    monkeypatch.setattr(subprocess, 'Popen', fake_process)
    args = (tmp_path / 'anime.db', 42, 3, 'cpu')
    path = probe.probe(*args)
    report = json.loads(path.read_text())
    assert Path(report['evidence_dir']).joinpath('receipt.json').read_bytes() == path.read_bytes()
    assert probe.probe(*args) == path and len(decoded) == 1
    protected = True
    with pytest.raises(ValueError, match='scope changed'):
        probe.probe(*args)
    protected = False
    cutoff = report['checked_at'] + 86401
    monkeypatch.setattr(probe.time, 'time', lambda: cutoff)
    end_seconds = 20
    with pytest.raises(ValueError, match='Decoded 20.000 of 30.000'):
        probe.probe(*args)
    assert json.loads(path.read_text()) == report
    end_seconds = 30
    assert probe.probe(*args) == path and len(decoded) == 3
    assert json.loads(path.read_text())['checked_at'] == cutoff
