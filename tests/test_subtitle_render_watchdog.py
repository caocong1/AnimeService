"""Synthetic progress and owned child processes; never encode production media."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import finish_subtitle_episode as finisher
from scripts import subtitle_render_watchdog as watchdog
from scripts.subtitle_output_lock import OutputBusyError, output_lock


def progress(path, frame, timestamp, end=False, extra=''):
    with path.open('a', encoding='ascii') as stream:
        stream.write(f'frame={frame}\nout_time_us={timestamp}\n{extra}'
                     f'progress={"end" if end else "continue"}\n')


class VirtualEncoder:
    """Advance virtual wall time on Popen waits, independently of output time."""
    def __init__(self, path, finish_at, on_wait):
        self.path, self.finish_at, self.on_wait = path, finish_at, on_wait
        self.now = 0
        self.returncode = None

    def poll(self):
        if self.now >= self.finish_at:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout):
        self.now += timeout
        self.on_wait(self)
        if self.poll() is None:
            raise subprocess.TimeoutExpired('synthetic encoder', timeout)
        return self.returncode


def virtual(tmp_path, monkeypatch, finish_at, on_wait):
    encoder = VirtualEncoder(tmp_path / 'progress.txt', finish_at, on_wait)
    monkeypatch.setattr(watchdog.time, 'monotonic', lambda: encoder.now)
    return encoder


@pytest.mark.parametrize('counter', ['frame', 'out_time_us'])
def test_real_encoded_progress_outlives_old_total_limit(tmp_path, monkeypatch, counter):
    def advance(p):
        progress(p.path, int(p.now) if counter == 'frame' else 0,
                 int(p.now * 1_000_000) if counter == 'out_time_us' else 0)
    encoder = virtual(tmp_path, monkeypatch, 7200, advance)
    outcome = {}
    assert watchdog.wait_for_render(encoder, encoder.path, outcome, poll_seconds=300) == 0
    assert encoder.now == 7200 > 1800
    assert outcome['watchdog']['total_runtime_limit'] is None
    assert 'timeout' not in outcome


@pytest.mark.parametrize('noise', ['no_output', 'repeated', 'regressing', 'incomplete'])
def test_log_growth_and_repeated_or_regressing_counters_do_not_renew(tmp_path, monkeypatch, noise):
    def advance(p):
        if noise == 'no_output':
            return
        if noise == 'incomplete':
            with p.path.open('a') as stream:
                stream.write(f'frame={int(p.now * 100)}\n')
        else:
            frame = 10 if noise == 'repeated' else max(0, 10 - int(p.now))
            progress(p.path, frame, frame * 1000,
                     extra=f'fps={p.now}\nspeed={p.now}x\ntotal_size={p.now * 1000}\n')
    encoder = virtual(tmp_path, monkeypatch, 100, advance)
    progress(encoder.path, 10, 10_000)
    outcome = {}
    with pytest.raises(watchdog.RenderStalledError, match='no_encoded_progress'):
        watchdog.wait_for_render(encoder, encoder.path, outcome,
                                 idle_seconds=10, poll_seconds=2)
    assert encoder.now == 10
    assert outcome['timeout'] is True
    assert outcome['watchdog']['frame'] == 10


def test_end_record_is_not_exit_and_repetition_does_not_extend_exit_bound(tmp_path, monkeypatch):
    encoder = virtual(tmp_path, monkeypatch, 100,
                      lambda p: progress(p.path, 20, 2000, end=True))
    progress(encoder.path, 20, 2000, end=True)
    outcome = {}
    with pytest.raises(watchdog.RenderStalledError, match='progress_end_without_exit'):
        watchdog.wait_for_render(encoder, encoder.path, outcome,
                                 idle_seconds=4, exit_seconds=12, poll_seconds=2)
    assert encoder.now == 12
    assert outcome['watchdog']['progress_end_seen']


def test_process_exit_without_end_record_preserves_actual_nonzero_status(tmp_path, monkeypatch):
    encoder = virtual(tmp_path, monkeypatch, 100, lambda p: None)
    encoder.returncode = 7
    outcome = {}
    assert watchdog.wait_for_render(encoder, encoder.path, outcome) == 7
    assert 'timeout' not in outcome


def test_protocol_reads_complete_records_crlf_fallback_and_bounded_tail(tmp_path):
    path = tmp_path / 'progress.txt'
    assert watchdog.read_progress(path) == {}
    path.write_bytes(b'frame=4\r\nout_time_us=N/A\r\nout_time=01:02:03.004500\r\nprogress=continue\r\n'
                     b'frame=999\nout_time_us=999999999999\nprogress=end')
    assert watchdog.read_progress(path) == {'frame': 4, 'out_time_us': 3_723_004_500, 'ended': False}
    with path.open('ab') as stream:
        stream.write(b'\n')
    assert watchdog.read_progress(path)['ended'] is True
    # Start the bounded tail midway through a very large invalid record.
    path.write_bytes(b'frame=' + b'9' * 100_000 + b'\nprogress=continue\n')
    progress(path, 8, 8000)
    assert watchdog.read_progress(path) == {'frame': 8, 'out_time_us': 8000, 'ended': False}
    path.write_bytes(b'frame=' + b'9' * 5000 + b'\nout_time_us=-1\nprogress=continue\n')
    assert watchdog.read_progress(path) == {'frame': None, 'out_time_us': None, 'ended': False}


@pytest.mark.parametrize('value', [0, -1, True, float('inf'), float('nan')])
def test_policy_rejects_unbounded_or_invalid_idle_limits(tmp_path, value):
    with pytest.raises(ValueError, match='bounds'):
        watchdog.wait_for_render(None, tmp_path / 'progress.txt', {}, idle_seconds=value)


CHILD = r'''
import sys, time
from pathlib import Path
mode, stop = sys.argv[1:]
Path('partial.mp4').write_bytes(b'synthetic partial output must survive')
with Path('render-progress.txt').open('w') as stream:
    stream.write('frame=1\nout_time_us=1000\nprogress=continue\n')
if mode == 'failure':
    sys.exit(7)
if mode == 'success':
    deadline = time.monotonic() + 12
    while not Path(stop).exists() and time.monotonic() < deadline:
        time.sleep(.02)
    with Path('render-progress.txt').open('a') as stream:
        stream.write('frame=2\nout_time_us=2000\nprogress=end\n')
else:
    time.sleep(30)
'''


@pytest.mark.parametrize('mode', ['success', 'stall', 'failure', 'monitor_error', 'receipt_error', 'spawn_error'])
def test_owned_child_cleanup_preserves_partial_and_releases_single_slot(tmp_path, monkeypatch, mode):
    slot, stop = tmp_path / 'slot.lock', tmp_path / 'finish-child'
    root = tmp_path / 'release'
    root.mkdir()
    real_wait = watchdog.wait_for_render
    real_write = finisher.write
    observed = []

    def monitor(process, path, outcome):
        # Actual child inherited the same lock, before any cleanup occurs.
        with pytest.raises(OutputBusyError):
            with output_lock(slot, 'contender', timeout=0):
                pytest.fail('Encoding slot was lost')
        observed.append(process.pid)
        if mode == 'monitor_error':
            raise OSError('synthetic progress read failure')
        stop.write_text('finish')
        return real_wait(process, path, outcome, idle_seconds=3, exit_seconds=3, poll_seconds=.02)

    def write(path, value):
        if mode == 'receipt_error' and path.name == 'process.json':
            raise OSError('synthetic process receipt failure')
        return real_write(path, value)

    monkeypatch.setattr(watchdog, 'wait_for_render', monitor)
    monkeypatch.setattr(finisher, 'write', write)
    args = ([str(tmp_path / 'missing-encoder.exe')] if mode == 'spawn_error'
            else [sys.executable, '-c', CHILD, mode, str(stop)])
    outcome = {}
    with output_lock(slot, 'synthetic-render') as lease:
        if mode == 'success':
            finisher.encode_release(args, root, lease, outcome)
        else:
            error_type = (watchdog.RenderStalledError if mode == 'stall' else
                          AssertionError if mode == 'failure' else OSError)
            with pytest.raises(error_type):
                finisher.encode_release(args, root, lease, outcome)
    saved = json.loads((root / 'render-outcome.json').read_text(encoding='utf-8'))
    assert saved['stopped'] is True
    assert saved['spawned'] == (mode != 'spawn_error')
    if mode in ('spawn_error', 'success', 'failure'):
        assert saved['exit_code'] == {'spawn_error': None, 'success': 0, 'failure': 7}[mode]
    if mode in ('stall', 'monitor_error', 'receipt_error'):
        assert saved['exit_code'] is not None and saved['exit_code'] != 0
    if mode in ('success', 'stall', 'failure'):
        assert observed
        assert (root / 'partial.mp4').read_bytes() == b'synthetic partial output must survive'
    if mode == 'stall':
        assert saved['timeout_reason'] == 'no_encoded_progress'
    assert not (root / 'release.json').exists()
    with output_lock(slot, 'after-owned-child-stopped', timeout=0):
        pass


def test_uncertain_child_stop_keeps_negative_fact_and_original_error(tmp_path, monkeypatch):
    class Child:
        pid = 42
        returncode = None
        def poll(self):
            return None
        def kill(self):
            pass
        def wait(self, timeout):
            raise subprocess.TimeoutExpired('synthetic unconfirmed stop', timeout)
    monkeypatch.setattr(finisher.subprocess, 'Popen', lambda *a, **kw: Child())
    def broken_monitor(*args):
        raise OSError('original monitor error')
    monkeypatch.setattr(watchdog, 'wait_for_render', broken_monitor)
    with output_lock(tmp_path / 'slot.lock', 'fixture') as lease:
        with pytest.raises(OSError, match='original monitor error'):
            finisher.encode_release(['synthetic'], tmp_path, lease, {})
    saved = json.loads((tmp_path / 'render-outcome.json').read_text(encoding='utf-8'))
    assert saved['stopped'] is False and saved['exit_code'] is None
    assert saved['stop_error']['type'] == 'TimeoutExpired'


@pytest.mark.parametrize('stopped', [True, False])
def test_failed_build_never_finalizes_and_unknown_stop_keeps_worker_marker(tmp_path, monkeypatch, stopped):
    # Exercise the real build wiring with synthetic frozen inputs and no encoder.
    ass = tmp_path / 'source.ass'
    ass.write_text('[Events]\nFormat: Layer, Start, End, Style, Name, Text\n'
                   'Dialogue: 1,0:00:00.00,0:00:03.00,ZH,fixture,测试\n', encoding='utf-8')
    source, coverage, config = (tmp_path / name for name in ('source.mkv', 'review.json', 'config.json'))
    for path in (source, coverage, config):
        path.write_text('synthetic')
    c = {'media': str(source), 'ass': str(ass), 'media_sha256': finisher.digest(source),
         'ass_sha256': finisher.digest(ass), 'ffmpeg': 'synthetic', 'ffprobe': 'synthetic',
         'title': 'fixture', 'show_id': 42, 'episode': 1, 'fonts': [],
         'chinese_coverage_review': str(coverage), 'filename': 'partial.mp4'}
    def fail(args, root, lease, outcome):
        (root / 'partial.mp4').write_bytes(b'keep original partial')
        outcome['stopped'] = stopped
        raise OSError('synthetic failed encode')
    monkeypatch.setattr(finisher, 'encode_release', fail)
    monkeypatch.setattr(finisher, 'finalize_rendered', lambda *a, **kw: pytest.fail('Failure was finalized'))
    root = tmp_path / 'release'
    with pytest.raises(OSError, match='failed encode'):
        finisher.build_release(config, c, root, lambda stage: None, lambda *a: None, None)
    assert (root / 'partial.mp4').read_bytes() == b'keep original partial'
    assert (root / 'worker.lock.json').exists() == (not stopped)
    with pytest.raises(FileExistsError):
        finisher.build_release(config, c, root, lambda stage: None, lambda *a: None, None)
    assert not (root / 'release.json').exists()
