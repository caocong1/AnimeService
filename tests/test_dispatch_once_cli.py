"""Caller audit tests use fake children and private temporary files only."""
import errno
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from anime.dispatch_caller import DispatchFailure, dispatch_once
from scripts import subtitle_dispatch_once as caller


class FakeChild:
    def __init__(self, kwargs, waits, stdout=b'', stderr=b''):
        self.pid = 876543
        self.wait_results = list(waits)
        self.wait_timeouts = []
        self.kills = 0
        kwargs['stdout'].write(stdout)
        kwargs['stderr'].write(stderr)

    def wait(self, *, timeout):
        self.wait_timeouts.append(timeout)
        result = self.wait_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def kill(self):
        self.kills += 1

    def __enter__(self):
        pytest.fail('Popen context could perform an unbounded wait')

    def communicate(self, *args, **kwargs):
        pytest.fail('no pipe drain is allowed')


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('real subprocess calls are forbidden')
    monkeypatch.setattr(caller.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(caller.subprocess, 'run', forbidden)
    monkeypatch.setattr(caller, 'INTENT_MAY_EXIST', False)


def install_child(monkeypatch, waits, stdout=b'', stderr=b'', before_launch=None):
    launches, children = [], []

    def popen(command, **kwargs):
        if before_launch:
            before_launch(command, kwargs)
        launches.append(command)
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['stdout'] != subprocess.PIPE
        assert kwargs['stderr'] != subprocess.PIPE
        assert 'text' not in kwargs and 'encoding' not in kwargs
        child = FakeChild(kwargs, waits, stdout, stderr)
        children.append(child)
        return child

    monkeypatch.setattr(caller.subprocess, 'Popen', popen)
    return launches, children


def outcome(directory):
    return json.loads((directory / 'delivery-01.outcome.json').read_text(encoding='utf-8'))


def events(directory):
    return [json.loads(line) for line in
            (directory / 'delivery-01.events.jsonl').read_text(encoding='utf-8').splitlines()]


@pytest.mark.parametrize('operation,timeout', [('begin', 120), ('confirm', 30)])
def test_timeout_preserves_partial_output_without_command_or_retry(tmp_path, monkeypatch, operation, timeout):
    error = subprocess.TimeoutExpired(['never-expose-command-secret'], timeout,
                                     output=b'exception-buffer-secret', stderr=b'exception-stderr-secret')
    stdout = b'partial stdout\n\xff'
    stderr = '部分诊断'.encode() + b'\x00\xfe'
    launches, children = install_child(monkeypatch, [error, -9], stdout, stderr)
    with pytest.raises(DispatchFailure, match='partial private audit') as failure:
        caller.run_delivery_cli([operation, 'synthetic-job-secret'], tmp_path, 1)
    assert len(launches) == 1
    assert children[0].wait_timeouts == [timeout, 5]
    assert children[0].kills == 1
    assert (tmp_path / 'delivery-01.stdout').read_bytes() == stdout
    assert (tmp_path / 'delivery-01.stderr').read_bytes() == stderr
    audit = outcome(tmp_path)
    assert audit['status'] == 'timed_out'
    assert audit['partial_output'] and audit['reconcile_required']
    assert audit['direct_reaped'] and audit['descendants_unknown']
    assert audit['direct_child_pid'] == children[0].pid
    assert audit['external_host_call_started'] is False
    assert audit['host_called_by_this_helper'] is False
    assert audit['captured_so_far'] == {'stdout_bytes': len(stdout), 'stderr_bytes': len(stderr)}
    metadata = str(failure.value) + json.dumps(audit) + json.dumps(events(tmp_path))
    for forbidden in ('never-expose-command-secret', 'synthetic-job-secret', 'exception-buffer-secret',
                      'exception-stderr-secret', 'partial stdout', '部分诊断'):
        assert forbidden not in metadata


def test_confirm_timeout_preserves_callers_host_attempt(tmp_path, monkeypatch):
    install_child(monkeypatch, [subprocess.TimeoutExpired(['synthetic'], 30), -9])
    with pytest.raises(DispatchFailure, match='timed out'):
        caller.run_delivery_cli(['confirm', 'synthetic'], tmp_path, 1, host_call_started=True)
    audit = outcome(tmp_path)
    assert audit['external_host_call_started'] is True
    assert audit['host_called_by_this_helper'] is False
    assert audit['reconcile_required'] is True
    assert all(row['external_host_call_started'] is True for row in events(tmp_path))


def test_begin_timeout_never_calls_host_even_if_partial_stdout_looks_successful(tmp_path, monkeypatch):
    install_child(monkeypatch, [subprocess.TimeoutExpired(['synthetic'], 120), -9],
                  stdout=b'{"ticket_path":"looks-valid"}')
    host = SimpleNamespace(preflight=lambda: {'create': True, 'resume': True},
                           dispatch=lambda **kwargs: pytest.fail('host must not be called'))
    with pytest.raises(DispatchFailure, match='timed out'):
        dispatch_once(host=host, archive_preflight=lambda: None,
                      begin=lambda: caller.run_delivery_cli(['begin', 'synthetic'], tmp_path, 1),
                      confirm=lambda *args: pytest.fail('confirm must not be called'),
                      interrupted=lambda *args: pytest.fail('no original ticket'), journal=lambda row: None)


def test_nonzero_output_is_archived_but_never_returned_as_success(tmp_path, monkeypatch):
    launches, children = install_child(monkeypatch, [7], b'{}', b'archive failed\xff')
    with pytest.raises(DispatchFailure, match='command failed'):
        caller.run_delivery_cli(['begin', 'synthetic'], tmp_path, 1)
    assert len(launches) == 1
    assert children[0].wait_timeouts == [120] and children[0].kills == 0
    assert (tmp_path / 'delivery-01.stderr').read_bytes() == b'archive failed\xff'
    audit = outcome(tmp_path)
    assert audit['status'] == 'process_exit' and audit['returncode'] == 7
    assert audit['reconcile_required'] and audit['direct_reaped']


@pytest.mark.parametrize('suffix', ['stdout', 'stderr', 'events.jsonl', 'outcome.json'])
def test_preexisting_audit_file_prevents_launch_and_is_never_overwritten(tmp_path, suffix):
    original = tmp_path / ('delivery-01.' + suffix)
    original.write_bytes(b'prior-private-audit')
    with pytest.raises(DispatchFailure, match='command not started'):
        caller.run_delivery_cli(['begin', 'synthetic'], tmp_path, 1)
    assert original.read_bytes() == b'prior-private-audit'
    if suffix != 'outcome.json':
        audit = outcome(tmp_path)
        assert audit['status'] == 'audit_preopen_failed'
        assert audit['direct_child_pid'] is None and audit['reconcile_required'] is False


def test_prelaunch_fsync_failure_prevents_launch(tmp_path, monkeypatch):
    def fail_sync(fd):
        raise OSError(errno.EIO, 'private-fsync-detail')
    monkeypatch.setattr(caller.os, 'fsync', fail_sync)
    with pytest.raises(DispatchFailure, match='command not started'):
        caller.run_delivery_cli(['begin', 'synthetic'], tmp_path, 1)
    assert (tmp_path / 'delivery-01.outcome.json').exists()


def test_confirm_preopen_failure_retains_reconciliation_after_host_attempt(tmp_path):
    (tmp_path / 'delivery-01.stdout').write_bytes(b'prior-private-audit')
    with pytest.raises(DispatchFailure, match='command not started'):
        caller.run_delivery_cli(['confirm', 'synthetic'], tmp_path, 1, host_call_started=True)
    audit = outcome(tmp_path)
    assert audit['status'] == 'audit_preopen_failed'
    assert audit['direct_child_pid'] is None
    assert audit['external_host_call_started'] and audit['reconcile_required']


def test_launch_failure_has_private_safe_outcome(tmp_path, monkeypatch):
    launches = []
    def fail_launch(command, **kwargs):
        launches.append(command)
        raise OSError(errno.EACCES, 'launch-exception-command-secret')
    monkeypatch.setattr(caller.subprocess, 'Popen', fail_launch)
    with pytest.raises(DispatchFailure, match='launch failed') as failure:
        caller.run_delivery_cli(['begin', 'synthetic-job-secret'], tmp_path, 1)
    assert len(launches) == 1
    audit = outcome(tmp_path)
    assert audit['status'] == 'launch_failed' and audit['errno'] == errno.EACCES
    assert audit['direct_child_pid'] is None and audit['reconcile_required']
    assert audit['captured_so_far'] == {'stdout_bytes': 0, 'stderr_bytes': 0}
    metadata = str(failure.value) + json.dumps(audit) + json.dumps(events(tmp_path))
    assert 'launch-exception-command-secret' not in metadata
    assert 'synthetic-job-secret' not in metadata


def test_timeout_reap_is_bounded_even_if_direct_child_does_not_exit(tmp_path, monkeypatch):
    launches, children = install_child(monkeypatch,
        [subprocess.TimeoutExpired(['secret'], 120), subprocess.TimeoutExpired(['secret'], 5)],
        b'partial', b'partial diagnostic')
    with pytest.raises(DispatchFailure, match='timed out'):
        caller.run_delivery_cli(['begin', 'synthetic'], tmp_path, 1)
    assert len(launches) == 1
    assert children[0].wait_timeouts == [120, 5] and children[0].kills == 1
    audit = outcome(tmp_path)
    assert audit['direct_reaped'] is False and audit['descendants_unknown']
    assert audit['reap_error_type'] == 'TimeoutExpired' and audit['reconcile_required']
    assert audit['captured_so_far'] == {'stdout_bytes': 7, 'stderr_bytes': 18}


def test_success_durable_audit_precedes_launch_and_db_is_global_option(tmp_path, monkeypatch):
    synced = set()
    original_fsync = os.fsync
    def fsync(fd):
        synced.add(os.fstat(fd).st_ino)
        original_fsync(fd)
    monkeypatch.setattr(caller.os, 'fsync', fsync)
    database = tmp_path / 'never-created.db'
    def before_launch(command, kwargs):
        for suffix in ('stdout', 'stderr', 'events.jsonl', 'outcome.json'):
            path = tmp_path / ('delivery-01.' + suffix)
            assert path.exists() and path.stat().st_ino in synced
        assert outcome(tmp_path)['status'] == 'launching'
        assert [row['phase'] for row in events(tmp_path)] == ['audit_ready', 'launching']
        assert command[2:] == ['--db', str(database), 'begin', 'synthetic']
    stdout = '{"ok": true, "label": "完成"}\n'.encode()
    stderr = b'\xff\x00private raw diagnostics\r\n'
    launches, children = install_child(monkeypatch, [0], stdout, stderr, before_launch)
    assert caller.run_delivery_cli(['begin', 'synthetic'], tmp_path, 1, db_path=database) == {
        'ok': True, 'label': '完成'}
    assert len(launches) == 1 and not database.exists()
    assert children[0].wait_timeouts == [120] and children[0].kills == 0
    assert (tmp_path / 'delivery-01.stdout').read_bytes() == stdout
    assert (tmp_path / 'delivery-01.stderr').read_bytes() == stderr
    audit, rows = outcome(tmp_path), events(tmp_path)
    assert audit['status'] == 'succeeded' and audit['direct_reaped']
    assert audit['reconcile_required'] is False
    assert [row['phase'] for row in rows] == ['audit_ready', 'launching', 'started', 'succeeded']
    assert all(row['helper_pid'] == os.getpid() and row['operation'] == 'begin' for row in rows)
    assert all(row['started_at'] == audit['started_at'] for row in rows)
    assert all(row['started_monotonic'] == audit['started_monotonic'] for row in rows)
    assert rows[2]['direct_child_pid'] == children[0].pid


def test_growing_stdout_rejects_success_without_moving_writer(tmp_path, monkeypatch):
    stdout = b'{"ok":true}'
    writer = []
    reads = []
    original_open = Path.open

    class SnapshotReader:
        def __init__(self, reader):
            self.reader = reader
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.reader.close()
        def fileno(self):
            return self.reader.fileno()
        def read(self, size):
            reads.append(size)
            assert size == len(stdout)
            assert writer[0].tell() == len(stdout)
            writer[0].write(b'late descendant bytes')
            return self.reader.read(size)

    def open_file(path, mode='r', *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path.name == 'delivery-01.stdout' and mode == 'rb':
            return SnapshotReader(handle)
        return handle

    monkeypatch.setattr(Path, 'open', open_file)
    install_child(monkeypatch, [0], stdout, before_launch=lambda cmd, kw: writer.append(kw['stdout']))
    with pytest.raises(DispatchFailure, match='invalid JSON'):
        caller.run_delivery_cli(['status'], tmp_path, 1)
    assert reads == [len(stdout)]
    with original_open(tmp_path / 'delivery-01.stdout', 'rb') as raw:
        assert raw.read() == stdout + b'late descendant bytes'
    audit = outcome(tmp_path)
    assert audit['status'] == 'invalid_output' and audit['reconcile_required']
    assert audit['descendants_unknown'] is True
    assert audit['captured_so_far']['stdout_bytes'] == len(stdout + b'late descendant bytes')


@pytest.mark.parametrize('stdout', [b'{not valid json', b'\xff\x00not utf8'])
def test_invalid_json_never_returns_partial_success(tmp_path, monkeypatch, stdout):
    launches, children = install_child(monkeypatch, [0], stdout, b'raw stderr')
    with pytest.raises(DispatchFailure, match='invalid JSON'):
        caller.run_delivery_cli(['status'], tmp_path, 1)
    assert len(launches) == 1 and children[0].wait_timeouts == [30]
    assert children[0].kills == 0
    assert (tmp_path / 'delivery-01.stdout').read_bytes() == stdout
    audit = outcome(tmp_path)
    assert audit['status'] == 'invalid_output' and audit['direct_reaped']
    assert audit['reconcile_required'] and audit['descendants_unknown']
