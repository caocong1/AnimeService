"""Archive failure contracts: synthetic credentials, temporary files/DB, no host/PowerShell."""
import errno
import json
import math
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import subtitle_delivery_queue as cli
from test_dispatch_caller_contract import caller  # noqa: F401
from test_subtitle_delivery import delivery, begin as begin_fixture  # noqa: F401


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Archive unit tests must never start a real subprocess')
    monkeypatch.setattr(subprocess, 'run', forbidden)


@pytest.fixture
def disk_archive(monkeypatch):
    monkeypatch.setattr(cli, 'protect_ticket_directory', lambda path: None)
    return cli


@pytest.fixture
def intent(delivery):
    db, scheduler, ready, now = delivery
    identity, result = begin_fixture(scheduler)
    return db, scheduler, identity, result


@pytest.mark.parametrize('kind', ['timeout', 'process_exit', 'launch'])
def test_acl_diagnostics_classify_failures_without_echoing_commands_or_credentials(tmp_path, monkeypatch, kind):
    command_secret = 'synthetic-dispatch-credential-in-command-DO-NOT-ECHO'
    output_secret = b'synthetic-credential-in-stdout-DO-NOT-ECHO'
    command = ['synthetic-powershell', '--credential', command_secret]
    stderr = b'synthetic ACL subsystem error\n'
    if kind == 'timeout':
        failure = subprocess.TimeoutExpired(command, 45, output=output_secret, stderr=stderr)
    elif kind == 'process_exit':
        failure = subprocess.CalledProcessError(7, command, output=output_secret, stderr=stderr)
    else:
        failure = OSError(errno.EACCES, command_secret)
        stderr = b''
    calls = []
    def fail(args, **kwargs):
        calls.append((args, kwargs))
        raise failure
    # Only this module sees a Windows-shaped environment; no global OS spoofing.
    monkeypatch.setattr(cli, 'os', SimpleNamespace(name='nt', environ={
        **os.environ, 'SystemRoot': str(tmp_path / 'synthetic-windows')}))
    monkeypatch.setattr(cli.subprocess, 'run', fail)
    with pytest.raises(cli.TicketArchiveError) as caught:
        cli.protect_ticket_directory(tmp_path)
    details = caught.value.diagnostics
    assert isinstance(caught.value, ValueError)
    assert details['phase'] == 'ticket_acl' and details['kind'] == kind
    assert details['exception_type'] == type(failure).__name__
    assert details['timeout_seconds'] == cli.ACL_TIMEOUT_SECONDS == 45
    assert math.isfinite(details['elapsed_seconds']) and details['elapsed_seconds'] >= 0
    assert details['stderr'] == stderr.decode('utf-8', errors='replace')
    assert details['stderr_bytes'] == len(stderr) and details['stderr_truncated'] is False
    allowed = {'phase', 'kind', 'elapsed_seconds', 'timeout_seconds', 'exception_type',
               'returncode', 'errno', 'stderr', 'stderr_bytes', 'stderr_truncated'}
    assert set(details) <= allowed
    if kind == 'process_exit':
        assert details['returncode'] == 7
    elif kind == 'launch':
        assert details['errno'] == errno.EACCES
    emitted = json.dumps(details) + str(caught.value)
    assert command_secret not in emitted and output_secret.decode() not in emitted
    assert 'synthetic-powershell' not in emitted and '--credential' not in emitted
    assert len(calls) == 1 and calls[0][1]['timeout'] == 45
    assert calls[0][1]['capture_output'] is True and calls[0][1]['check'] is True


@pytest.mark.parametrize('size', [0, 4096, 4097, 20000])
def test_acl_stderr_is_bounded_by_raw_bytes_and_marks_truncation(tmp_path, monkeypatch, size):
    # Deliberately invalid UTF-8 verifies replacement rather than decoding failure.
    stderr = (b'\xff' + b'x' * (size - 1)) if size else b''
    failure = subprocess.CalledProcessError(1, ['hidden-command-credential'],
                                           output=b'hidden-stdout-credential', stderr=stderr)
    monkeypatch.setattr(cli, 'os', SimpleNamespace(name='nt', environ={
        **os.environ, 'SystemRoot': str(tmp_path / 'synthetic-windows')}))
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(cli.subprocess, 'run', fail)
    with pytest.raises(cli.TicketArchiveError) as caught:
        cli.protect_ticket_directory(tmp_path)
    details = caught.value.diagnostics
    assert details['stderr'] == stderr[:4096].decode('utf-8', errors='replace')
    assert details['stderr_bytes'] == size
    assert details['stderr_truncated'] is (size > 4096)
    assert 'hidden-command-credential' not in str(caught.value)
    assert 'hidden-stdout-credential' not in json.dumps(details)


def test_preflight_writes_flushes_syncs_closes_reads_and_cleans_probe(tmp_path, monkeypatch, disk_archive):
    captured = []
    opened_streams = []
    synced = []
    readbacks = []
    write_archive = cli._write_verified_archive
    real_fdopen, real_fsync, real_open = cli.os.fdopen, cli.os.fsync, Path.open
    def archive(path, raw):
        captured.append((Path(path), raw))
        return write_archive(path, raw)
    def fdopen(*args, **kwargs):
        stream = real_fdopen(*args, **kwargs)
        opened_streams.append(stream)
        return stream
    def fsync(fd):
        path, raw = captured[-1]
        # Bytes must have reached the file before durability is requested.
        with real_open(path, 'rb') as check:
            assert check.read() == raw
        synced.append(fd)
        return real_fsync(fd)
    def readback(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if mode == 'rb' and captured and Path(path) == captured[-1][0]:
            assert synced and all(stream.closed for stream in opened_streams)
            readbacks.append(Path(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(cli, '_write_verified_archive', archive)
    monkeypatch.setattr(cli.os, 'fdopen', fdopen)
    monkeypatch.setattr(cli.os, 'fsync', fsync)
    monkeypatch.setattr(Path, 'open', readback)
    cli.preflight_ticket_archive(tmp_path / 'anime.db')
    assert len(captured) == 1 and synced and readbacks == [captured[0][0]]
    path, raw = captured[0]
    assert isinstance(raw, bytes) and raw
    assert b'dispatch_token' not in raw and b'lease_token' not in raw
    assert path.parent == tmp_path / 'subtitle-delivery-tickets'
    assert not path.exists() and list(path.parent.iterdir()) == []
    assert not (tmp_path / 'anime.db').exists()


def test_preflight_collision_never_deletes_the_existing_file(tmp_path, monkeypatch, disk_archive):
    sentinel = b'preserve pre-existing synthetic probe evidence'
    collision = []
    write_archive = cli._write_verified_archive
    def collide(path, raw):
        path = Path(path)
        path.write_bytes(sentinel)
        collision.append(path)
        return write_archive(path, raw)
    monkeypatch.setattr(cli, '_write_verified_archive', collide)
    with pytest.raises((OSError, ValueError)):
        cli.preflight_ticket_archive(tmp_path / 'anime.db')
    assert len(collision) == 1 and collision[0].read_bytes() == sentinel
    assert list(collision[0].parent.iterdir()) == collision


def test_preflight_fsync_failure_keeps_failed_probe_for_diagnosis(tmp_path, monkeypatch, disk_archive):
    captured = []
    write_archive = cli._write_verified_archive
    def archive(path, raw):
        captured.append((Path(path), raw))
        return write_archive(path, raw)
    def broken_fsync(fd):
        raise OSError(errno.EIO, 'synthetic fsync failure')
    monkeypatch.setattr(cli, '_write_verified_archive', archive)
    monkeypatch.setattr(cli.os, 'fsync', broken_fsync)
    with pytest.raises((OSError, ValueError)):
        cli.preflight_ticket_archive(tmp_path / 'anime.db')
    assert len(captured) == 1
    path, raw = captured[0]
    assert path.is_file() and path.read_bytes() == raw
    assert b'dispatch_token' not in raw and b'lease_token' not in raw
    assert not (tmp_path / 'anime.db').exists()


def test_save_is_exclusive_and_validates_persisted_ticket(intent, monkeypatch, disk_archive):
    db, scheduler, identity, result = intent
    before = scheduler.get(identity)
    validated = []
    original_validate = cli.validate_ticket
    def validate(value):
        validated.append(value)
        return original_validate(value)
    monkeypatch.setattr(cli, 'validate_ticket', validate)
    path = Path(cli.save_ticket(db.path, result))
    original = path.read_bytes()
    assert json.loads(original) == result['ticket']
    assert validated and validated[-1] == result['ticket']
    with pytest.raises((OSError, ValueError)):
        cli.save_ticket(db.path, result)
    assert path.read_bytes() == original and scheduler.get(identity) == before
    assert len(list(path.parent.iterdir())) == 1


def test_save_fsync_failure_never_returns_path_or_erases_original_bytes(intent, monkeypatch, disk_archive):
    db, scheduler, identity, result = intent
    before = scheduler.get(identity)
    def broken_fsync(fd):
        raise OSError(errno.EIO, 'synthetic fsync failure')
    monkeypatch.setattr(cli.os, 'fsync', broken_fsync)
    with pytest.raises((OSError, ValueError)):
        cli.save_ticket(db.path, result)
    files = list((db.path.parent / 'subtitle-delivery-tickets').iterdir())
    assert len(files) == 1 and json.loads(files[0].read_bytes()) == result['ticket']
    assert scheduler.get(identity) == before and before['dispatch_state'] == 'committing'


def test_save_rejects_readback_mismatch_without_returning_path(intent, monkeypatch, disk_archive):
    db, scheduler, identity, result = intent
    before = scheduler.get(identity)
    real_open = Path.open
    reads = []
    directory = db.path.parent / 'subtitle-delivery-tickets'
    class MismatchedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, *args):
            return b'synthetic corrupted readback'
    def mismatch(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        stream = real_open(path, *args, **kwargs)
        if mode == 'rb' and Path(path).parent == directory:
            reads.append(Path(path))
            return MismatchedReader(stream)
        return stream
    monkeypatch.setattr(Path, 'open', mismatch)
    with pytest.raises((OSError, ValueError)):
        cli.save_ticket(db.path, result)
    files = list(directory.iterdir())
    assert reads and len(files) == 1
    with real_open(files[0], 'rb') as stream:
        assert json.loads(stream.read()) == result['ticket']
    assert scheduler.get(identity) == before


def test_save_rejects_persisted_ticket_validation_failure(intent, monkeypatch, disk_archive):
    db, scheduler, identity, result = intent
    before = scheduler.get(identity)
    validations = []
    original_validate = cli.validate_ticket
    def invalid(value):
        validations.append(value)
        if len(validations) == 1:
            return original_validate(value)
        raise ValueError('synthetic persisted-ticket schema failure')
    monkeypatch.setattr(cli, 'validate_ticket', invalid)
    with pytest.raises(ValueError):
        cli.save_ticket(db.path, result)
    assert len(validations) == 2 and scheduler.get(identity) == before
    files = list((db.path.parent / 'subtitle-delivery-tickets').iterdir())
    assert len(files) == 1 and json.loads(files[0].read_bytes()) == result['ticket']


def test_preflight_io_failure_stops_before_lease_and_external_host(caller, monkeypatch):
    db, scheduler, identity, caller_cli, invoke, begin, host, calls = caller
    before = scheduler.get(identity)
    monkeypatch.setattr(caller_cli, 'protect_ticket_directory', lambda path: None)
    def broken_fsync(fd):
        raise OSError(errno.EIO, 'synthetic preflight durability failure')
    monkeypatch.setattr(caller_cli.os, 'fsync', broken_fsync)
    with pytest.raises((OSError, ValueError)):
        result, path, ticket = begin()
        host(result)
    assert calls == [] and scheduler.get(identity) == before
    assert db.rows("SELECT * FROM subtitle_delivery_events WHERE kind IN ('reserved','dispatch-started')") == []


def test_second_acl_failure_keeps_committing_and_prevents_host(caller, monkeypatch):
    db, scheduler, identity, caller_cli, invoke, begin, host, calls = caller
    checked = []
    def acl(path):
        checked.append(Path(path))
        if len(checked) == 2:
            raise ValueError('synthetic second ACL verification failed')
    monkeypatch.setattr(caller_cli, 'protect_ticket_directory', acl)
    with pytest.raises(ValueError, match='second ACL verification'):
        result, path, ticket = begin()
        host(result)
    row = scheduler.get(identity)
    assert len(checked) == 2 and calls == []
    assert row['dispatch_state'] == 'committing' and not row['thread_id']
    assert row['lease_owner'] and row['dispatch_token']
    assert not list((db.path.parent / 'subtitle-delivery-tickets').iterdir())


def test_canary_cleanup_failure_stops_before_lease_and_external_host(caller, monkeypatch):
    db, scheduler, identity, caller_cli, invoke, begin, host, calls = caller
    before = scheduler.get(identity)
    directory = db.path.parent / 'subtitle-delivery-tickets'
    failed_cleanup = []
    real_unlink = Path.unlink
    monkeypatch.setattr(caller_cli, 'protect_ticket_directory', lambda path: None)
    def deny_probe_cleanup(path, *args, **kwargs):
        if Path(path).parent == directory and path.name.startswith('.archive-probe-'):
            failed_cleanup.append(Path(path))
            raise PermissionError(errno.EACCES, 'synthetic canary cleanup failure')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', deny_probe_cleanup)
    with pytest.raises(PermissionError, match='synthetic canary cleanup failure'):
        result, path, ticket = begin()
        host(result)
    assert calls == [] and scheduler.get(identity) == before
    assert db.rows("SELECT * FROM subtitle_delivery_events WHERE kind IN ('reserved','dispatch-started')") == []
    assert len(failed_cleanup) == 1 and list(directory.iterdir()) == failed_cleanup
    raw = failed_cleanup[0].read_bytes()
    assert raw and b'dispatch_token' not in raw and b'lease_token' not in raw


def test_missing_template_stops_before_lease_after_real_preflight(caller, tmp_path, monkeypatch):
    db, scheduler, identity, caller_cli, invoke, begin, host, calls = caller
    before = scheduler.get(identity)
    missing_root = tmp_path / 'without-session-template'
    monkeypatch.setattr(caller_cli, 'ROOT', missing_root)
    monkeypatch.setattr(caller_cli, 'protect_ticket_directory', lambda path: None)
    completed_preflights = []
    real_preflight = caller_cli.preflight_ticket_archive
    def observe_preflight(path):
        directory = real_preflight(path)
        completed_preflights.append(directory)
        return directory
    monkeypatch.setattr(caller_cli, 'preflight_ticket_archive', observe_preflight)
    with pytest.raises(FileNotFoundError):
        result, path, ticket = begin()
        host(result)
    assert calls == [] and scheduler.get(identity) == before
    assert db.rows("SELECT * FROM subtitle_delivery_events WHERE kind IN ('reserved','dispatch-started')") == []
    assert completed_preflights == [db.path.parent / 'subtitle-delivery-tickets']
    assert list(completed_preflights[0].iterdir()) == []
    assert not (missing_root / 'docs/SUBTITLE-SESSION-PROMPT.txt').exists()
