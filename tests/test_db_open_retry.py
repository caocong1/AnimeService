"""Opening a connection retries a transient storage error while nothing has run; nothing after that is repeated."""
import sqlite3
import time

import pytest

from anime import db as store_module
from anime.db import Store
from anime.read_projection import ProjectionStore


def failing(name):
    error = sqlite3.OperationalError('synthetic')
    error.sqlite_errorname = name
    return error


@pytest.fixture
def opens(monkeypatch):
    """Counts connections; the next PRAGMA synchronous / schema reads fail with the names queued in `plan`."""
    real, state = sqlite3.connect, {'opens': 0, 'plan': []}
    class Flaky(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql in ('PRAGMA synchronous=FULL', 'PRAGMA schema_version') and state['plan']:
                raise failing(state['plan'].pop(0))
            return super().execute(sql, *args)
    def connect(*args, **kwargs):
        state['opens'] += 1
        return real(*args, factory=Flaky, **kwargs)
    monkeypatch.setattr(store_module.sqlite3, 'connect', connect)
    monkeypatch.setattr(store_module.time, 'sleep', lambda seconds: None)
    for key, value in (('count', 0), ('time', None), ('code', None)):
        monkeypatch.setitem(store_module.DB_ERRORS, key, value)
    return state


def test_transient_open_error_is_retried_and_counted(tmp_path, opens):
    db = Store(tmp_path / 'test.db')
    db.set('poll_seconds', 180)
    opens.update(opens=0, plan=['SQLITE_IOERR_SHMMAP'])
    assert db.get('poll_seconds') == 180 and opens['opens'] == 2
    assert store_module.DB_ERRORS['count'] == 1 and store_module.DB_ERRORS['code'] == 'SQLITE_IOERR_SHMMAP'
    opens.update(opens=0, plan=['SQLITE_IOERR_READ'] * 3)
    with pytest.raises(sqlite3.OperationalError):
        db.get('poll_seconds')
    assert opens['opens'] == 3 and store_module.DB_ERRORS['count'] == 4


@pytest.mark.parametrize('name', ['SQLITE_ERROR', 'SQLITE_BUSY', 'SQLITE_LOCKED'])
def test_other_open_errors_are_not_retried(tmp_path, opens, name):
    db = Store(tmp_path / 'test.db')
    opens.update(opens=0, plan=[name])
    with pytest.raises(sqlite3.OperationalError):
        db.get('poll_seconds')
    assert opens['opens'] == 1 and store_module.DB_ERRORS['count'] == 0


def test_a_failing_caller_statement_is_never_repeated(tmp_path, opens):
    db = Store(tmp_path / 'test.db')
    opens['opens'] = 0
    with pytest.raises(sqlite3.OperationalError):
        db.execute('SELECT * FROM missing_table')
    assert opens['opens'] == 1


def test_busy_open_still_fails_fast(tmp_path):
    db = Store(tmp_path / 'test.db')
    holder = sqlite3.connect(db.path)
    holder.execute('PRAGMA locking_mode=EXCLUSIVE')
    holder.execute('BEGIN EXCLUSIVE')
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            with db.connect(timeout=0.2) as c:
                c.execute('SELECT 1').fetchone()
        assert time.monotonic() - started < 1.5
    finally:
        holder.rollback()
        holder.close()


def test_projection_open_retries_its_schema_read(tmp_path, opens):
    db = Store(tmp_path / 'test.db')
    db.set('poll_seconds', 180)
    opens.update(opens=0, plan=['SQLITE_CANTOPEN'])
    projection = ProjectionStore(db.path)
    assert projection.get('poll_seconds') == 180 and opens['opens'] == 2
