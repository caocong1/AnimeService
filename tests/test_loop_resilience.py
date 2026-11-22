"""Long-running loops survive failing diagnostics and back off while the same failure repeats.

Synthetic workers and temp DBs only; no real Engine, downloader or desktop process.
"""
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from anime import diagnostics
from anime.dandan_history import DandanHistory
from anime.db import Store
from anime.desktop_guardian import maintain_desktop
from anime.engine import Engine


def io_error():
    error = sqlite3.OperationalError('disk I/O error')
    error.sqlite_errorname = 'SQLITE_IOERR_READ'
    return error


def test_backoff_keeps_the_base_for_two_failures_then_doubles_to_a_cap():
    assert [diagnostics.backoff(n, 30) for n in range(8)] == [30, 30, 30, 60, 120, 240, 300, 300]
    assert diagnostics.backoff(50, 5, cap=60) == 60


def test_loop_report_never_raises_even_when_the_error_text_and_log_fail(monkeypatch):
    monkeypatch.setattr(diagnostics, 'safe_error', Mock(side_effect=RuntimeError('formatter broke')))
    monkeypatch.setattr(diagnostics._LOG, 'error', Mock(side_effect=OSError('log unavailable')))
    db = Mock()
    assert diagnostics.report_loop_error(db, 'service', io_error()) is False
    db.event.assert_not_called()


def test_worker_backs_off_while_cycles_keep_failing_and_resets_after_success(monkeypatch):
    stop = threading.Event()
    waits = []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 6:
            stop.set()
    worker = SimpleNamespace(db=Mock(), stop_event=stop, wake=Mock(wait=Mock(side_effect=wait)), maintain_loop=Mock(),
                             cycle=Mock(side_effect=[io_error()] * 4 + [None, io_error()]))
    worker.db.event.side_effect = io_error()
    monkeypatch.setattr('anime.engine.threading.Thread', Mock())
    Engine.run(worker)
    assert waits == [30, 30, 60, 120, 30, 30]
    assert worker.cycle.call_args_list[0] == call(True) and worker.cycle.call_count == 6


def test_maintenance_loop_backs_off_on_repeated_failure():
    stop = Mock()
    stop.wait.side_effect = [False] * 4 + [True]
    worker = SimpleNamespace(db=Mock(), stop_event=stop, maintenance=Mock(side_effect=io_error()), step=Mock())
    worker.db.event.side_effect = io_error()
    Engine.maintain_loop(worker)
    assert stop.wait.call_args_list == [call(30), call(30), call(30), call(60), call(120)]


def test_tracking_loop_survives_a_failing_database_and_backs_off():
    stop = threading.Event()
    waits = []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 4:
            stop.set()
    db = Mock()
    db.rows.side_effect = io_error()
    db.event.side_effect = io_error()
    worker = SimpleNamespace(db=db, stop_event=stop, tracking_wake=Mock(wait=Mock(side_effect=wait)))
    Engine.tracking_loop(worker)
    assert waits == [5, 5, 10, 20] and db.rows.call_count == 4


def test_step_failure_with_a_failing_ledger_does_not_escape(monkeypatch):
    db = Mock()
    db.get.side_effect = io_error()
    db.event.side_effect = io_error()
    worker = SimpleNamespace(db=db, cycle_errors_lock=threading.Lock())
    Engine.step(worker, 'downloads.missing', Mock(side_effect=RuntimeError('step failed')))
    Engine.step(worker, 'downloads.missing', Mock())  # a success whose ledger read fails is not raised either
    db.set.assert_not_called()


def test_steps_on_two_threads_do_not_lose_ledger_updates(tmp_path):
    db = Store(tmp_path / 'data/test.db')
    db.set('cycle_errors', {'recovering': {'error': 'old', 'count': 1, 'since': 0, 'last': 0}})
    barrier, original = threading.Barrier(2), db.get
    def get(key, default=None):
        value = original(key, default)
        if key == 'cycle_errors':
            try:
                barrier.wait(timeout=0.5)  # without serialization both threads read the same stale blob
            except threading.BrokenBarrierError:
                pass
        return value
    db.get = get
    worker = SimpleNamespace(db=db, cycle_errors_lock=threading.Lock())
    threads = [threading.Thread(target=Engine.step, args=(worker, 'failing', Mock(side_effect=RuntimeError('x')))),
               threading.Thread(target=Engine.step, args=(worker, 'recovering', Mock()))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert set(original('cycle_errors')) == {'failing'}


def test_desktop_guardian_survives_a_failing_health_write(monkeypatch, tmp_path):
    monkeypatch.setattr('anime.desktop_guardian.ROOT', tmp_path)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/desktop-autostart.json').write_text('{not json')
    db = Mock()
    db.set.side_effect = io_error()
    stop = Mock()
    stop.is_set.side_effect = [False, False, True]
    maintain_desktop(stop, db)
    assert stop.wait.call_args_list == [call(120), call(120)] and db.set.call_count == 2


def test_dandan_history_thread_survives_a_failing_health_write():
    history = SimpleNamespace(db=Mock(), sync=Mock(side_effect=[io_error(), io_error(), io_error(), {}]))
    history.db.event.side_effect = io_error()
    stop = Mock()
    stop.is_set.side_effect = [False] * 4 + [True]
    DandanHistory.run(history, stop)
    assert stop.wait.call_args_list == [call(60), call(60), call(120), call(60)]
