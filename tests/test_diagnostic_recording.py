"""Diagnostic failures cannot replace business failures or kill their boundaries.

All workers use synthetic dependencies and synchronous thread doubles. The API
handler is extracted from its source to avoid constructing the module-level app.
"""
import ast
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from anime import diagnostics
from anime.engine import Engine
from anime.net import safe_error
from anime.subtitle_studio import SubtitleStudio


def io_error(code=sqlite3.SQLITE_IOERR_READ, name='SQLITE_IOERR_READ'):
    error = sqlite3.OperationalError('disk I/O error')
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


@pytest.fixture(autouse=True)
def forbid_real_engine(monkeypatch):
    monkeypatch.setattr(Engine, '__init__', Mock(side_effect=AssertionError('no real Engine in diagnostics tests')))


def test_event_success_is_one_original_write_without_fallback(monkeypatch):
    db = Mock()
    fallback = Mock()
    monkeypatch.setattr(diagnostics._LOG, 'error', fallback)
    assert diagnostics.record_event(db, 'service', 'startup') is True
    db.event.assert_called_once_with('service', 'startup', 'info')
    db.set.assert_not_called()
    fallback.assert_not_called()


def test_event_failure_retains_both_sqlite_codes_without_raw_credentials(caplog):
    original = io_error()
    recording = io_error(sqlite3.SQLITE_IOERR_FSYNC, 'SQLITE_IOERR_FSYNC')
    db = Mock()
    db.event.side_effect = recording
    with caplog.at_level(logging.ERROR, logger='anime.diagnostics'):
        assert diagnostics.record_event(db, 'service', safe_error(original), 'error', error=original) is False
    db.event.assert_called_once()
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert 'operation_error=' in message and 'recording_error=' in message
    assert 'SQLITE_IOERR_READ' in message and 'SQLITE_IOERR_FSYNC' in message
    assert str(sqlite3.SQLITE_IOERR_READ) in message and str(sqlite3.SQLITE_IOERR_FSYNC) in message
    assert caplog.records[0].exc_info is None

    caplog.clear()
    secret = RuntimeError('https://user:password@example.test/?token=secret')
    db.event.side_effect = secret
    diagnostics.record_event(db, 'api', safe_error(secret), 'error', error=secret)
    assert 'RuntimeError' in caplog.text
    assert all(text not in caplog.text for text in ('password', 'token=secret', 'example.test'))


def test_logging_failure_never_retries_or_escapes(monkeypatch):
    db = Mock()
    db.event.side_effect = io_error()
    db.set.side_effect = io_error()
    fallback = Mock(side_effect=OSError('log unavailable'))
    monkeypatch.setattr(diagnostics._LOG, 'error', fallback)
    assert diagnostics.record_event(db, 'service', 'safe', error=io_error()) is False
    health = {'ok': False, 'error': 'safe'}
    assert diagnostics.record_subtitle_health(db, health, error=io_error()) is False
    assert db.event.call_count == 1 and fallback.call_count == 2
    db.set.assert_called_once_with('subtitle_studio_health', health)


@pytest.mark.parametrize('exit_error', [KeyboardInterrupt(), SystemExit()])
def test_process_control_is_not_swallowed(exit_error):
    db = Mock()
    db.event.side_effect = exit_error
    with pytest.raises(type(exit_error)):
        diagnostics.record_event(db, 'service', 'safe')


def run_worker(monkeypatch, failures, *, startup_fails=False, logging_fails=False):
    db = Mock()
    db.event.side_effect = io_error() if startup_fails else None
    stop = threading.Event()
    wake = Mock()
    worker = SimpleNamespace(db=db, stop_event=stop, wake=wake, maintain_loop=Mock())
    timeline = []
    def cycle(force):
        timeline.append(('cycle', force))
        if failures[len([item for item in timeline if item[0] == 'cycle']) - 1]:
            raise io_error()
    def wait(seconds):
        timeline.append(('wait', seconds))
        if len([item for item in timeline if item[0] == 'wait']) == len(failures):
            stop.set()
    worker.cycle = Mock(side_effect=cycle)
    wake.wait.side_effect = wait
    thread = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr('anime.engine.threading.Thread', constructor)
    if logging_fails:
        monkeypatch.setattr(diagnostics._LOG, 'error', Mock(side_effect=OSError('log unavailable')))
    Engine.run(worker)
    constructor.assert_called_once_with(target=worker.maintain_loop, daemon=True, name='download-maintenance')
    thread.start.assert_called_once_with()
    assert wake.clear.call_count == len(failures)
    db.set.assert_not_called()  # No synthetic heartbeat, maintenance or cycle success.
    return db, timeline


@pytest.mark.parametrize('logging_fails', [False, True])
def test_startup_and_cycle_recording_failure_still_wait_then_stop(monkeypatch, logging_fails):
    db, timeline = run_worker(monkeypatch, [True, True], startup_fails=True, logging_fails=logging_fails)
    assert timeline == [('cycle', True), ('wait', 30), ('cycle', False), ('wait', 30)]
    assert db.event.call_count == 3  # One startup and one per failed original cycle.


def test_normal_cycle_and_recovery_preserve_force_and_wait(monkeypatch):
    db, timeline = run_worker(monkeypatch, [True, False])
    assert timeline == [('cycle', True), ('wait', 30), ('cycle', False), ('wait', 30)]
    assert db.event.call_count == 2


def test_maintenance_failure_waits_before_each_attempt_and_obeys_stop(monkeypatch):
    db = Mock()
    db.event.side_effect = io_error()
    stop = Mock()
    stop.wait.side_effect = [False, False, True]
    worker = SimpleNamespace(db=db, stop_event=stop, maintenance=Mock(side_effect=io_error()), step=Mock())
    monkeypatch.setattr(diagnostics._LOG, 'error', Mock(side_effect=OSError('log unavailable')))
    Engine.maintain_loop(worker)
    assert stop.wait.call_args_list == [call(30), call(30), call(30)]
    assert worker.maintenance.call_count == 2 and db.event.call_count == 2
    db.set.assert_not_called()


def test_already_stopped_maintenance_does_no_business_work():
    worker = SimpleNamespace(db=Mock(), stop_event=Mock(), maintenance=Mock())
    worker.stop_event.wait.return_value = True
    Engine.maintain_loop(worker)
    worker.maintenance.assert_not_called()
    worker.db.event.assert_not_called()


def api_handler(db):
    path = Path(__file__).resolve().parents[1] / 'anime' / 'app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'create_app')
    handler = next(node for node in factory.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'unexpected')
    handler.decorator_list = []
    from starlette.concurrency import run_in_threadpool
    namespace = {'db': db, 'safe_error': safe_error, 'record_event': diagnostics.record_event,
                 'run_in_threadpool': run_in_threadpool, 'JSONResponse': JSONResponse}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['unexpected']


@pytest.mark.parametrize('logging_fails', [False, True])
def test_api_keeps_original_503_when_error_event_also_fails(monkeypatch, logging_fails):
    db = Mock()
    db.event.side_effect = io_error()
    if logging_fails:
        monkeypatch.setattr(diagnostics._LOG, 'error', Mock(side_effect=OSError('log unavailable')))
    api = FastAPI()
    api.add_exception_handler(Exception, api_handler(db))
    operation = Mock(side_effect=io_error())
    @api.get('/fault')
    def fault():
        operation()
    response = TestClient(api, raise_server_exceptions=False).get('/fault')
    assert response.status_code == 503
    assert response.json()['error'].startswith('OperationalError：')
    operation.assert_called_once_with()
    db.event.assert_called_once()
    db.set.assert_not_called()


def archive_worker(monkeypatch):
    db = Mock()
    db.get.side_effect = lambda key, default=None: {'subtitle_studio_enabled': True, 'subtitle_delivery_policy': {'enabled': True}}.get(key, default)
    db.one.return_value = None
    worker = SimpleNamespace(db=db, lock=threading.Lock(), engine=SimpleNamespace(stop_event=threading.Event()),
                             media=Mock(return_value=[]), collect=Mock(), delivery=SimpleNamespace(refresh=Mock()))
    class SynchronousThread:
        def __init__(self, *, target, name, daemon):
            assert name == 'subtitle-studio-archive' and daemon is True
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr('anime.subtitle_studio.threading.Thread', SynchronousThread)
    return worker


@pytest.mark.parametrize('health_fails', [False, True])
def test_archive_refresh_failure_releases_lock_and_never_reports_success(monkeypatch, health_fails):
    worker = archive_worker(monkeypatch)
    worker.delivery.refresh.side_effect = io_error()
    worker.db.event.side_effect = io_error()
    if health_fails:
        worker.db.set.side_effect = io_error()
        monkeypatch.setattr(diagnostics._LOG, 'error', Mock(side_effect=OSError('log unavailable')))
    SubtitleStudio.tick(worker)
    assert not worker.lock.locked()
    worker.delivery.refresh.assert_called_once_with()
    worker.db.event.assert_called_once()
    assert worker.db.set.call_count == 1
    key, health = worker.db.set.call_args.args
    assert key == 'subtitle_studio_health' and health['ok'] is False
    assert health['error'].startswith('OperationalError：')


def test_archive_collect_event_failure_keeps_original_bounded_processing(monkeypatch):
    worker = archive_worker(monkeypatch)
    worker.media.return_value = [{'fingerprint': str(i), 'inventory_revision': i} for i in range(3)]
    worker.collect.side_effect = [io_error(), None]
    worker.db.event.side_effect = io_error()
    SubtitleStudio.tick(worker)
    assert not worker.lock.locked() and worker.collect.call_count == 2
    worker.delivery.refresh.assert_called_once_with()
    worker.db.event.assert_called_once()


def test_archive_normal_health_write_is_exact_and_not_retried(monkeypatch):
    worker = archive_worker(monkeypatch)
    SubtitleStudio.tick(worker)
    assert not worker.lock.locked()
    worker.delivery.refresh.assert_called_once_with()
    worker.db.event.assert_not_called()
    assert worker.db.set.call_count == 1
    assert worker.db.set.call_args.args[0] == 'subtitle_studio_health'
    assert worker.db.set.call_args.args[1]['ok'] is True


def test_archive_business_read_error_still_escapes_without_success(monkeypatch):
    worker = archive_worker(monkeypatch)
    worker.media.side_effect = io_error()
    with pytest.raises(sqlite3.OperationalError):
        SubtitleStudio.tick(worker)
    assert not worker.lock.locked()
    worker.delivery.refresh.assert_not_called()
    worker.db.set.assert_not_called()


def test_archive_existing_stop_skips_collection_and_releases_lock(monkeypatch):
    worker = archive_worker(monkeypatch)
    worker.engine.stop_event.set()
    worker.media.return_value = [{'fingerprint': 'stopped', 'inventory_revision': 1}]
    SubtitleStudio.tick(worker)
    assert not worker.lock.locked()
    worker.collect.assert_not_called()
    worker.db.one.assert_not_called()
