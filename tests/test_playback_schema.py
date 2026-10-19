"""Explicit opt-in schema remains empty, atomic and retryable. Synthetic DBs only."""
import contextlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from anime.playback_writers import PlaybackWriters


class SchemaStore:
    def __init__(self, path, *, fail_after=None):
        self.path, self.fail_after = path, fail_after

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.path)
        try:
            if self.fail_after is None:
                yield c
            else:
                class DiskFull:
                    count = 0
                    def execute(inner, sql):
                        if sql.lstrip().startswith('CREATE'):
                            inner.count += 1
                            if inner.count > self.fail_after:
                                raise sqlite3.OperationalError('database or disk is full')
                        return c.execute(sql)
                yield DiskFull()
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()


def schema(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'playback_%' ORDER BY name").fetchall()


def seed(path):
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE web_progress(media_id TEXT PRIMARY KEY,position REAL)')
        c.execute("INSERT INTO web_progress VALUES('synthetic',42)")


@pytest.mark.parametrize('after',[1,2])
def test_disk_full_in_schema_transaction_rolls_back_all_ddl_and_retry_converges(tmp_path, after):
    path = tmp_path/'test.db';seed(path)
    with pytest.raises(sqlite3.OperationalError, match='disk is full'):
        PlaybackWriters(SchemaStore(path,fail_after=after),lambda key:key)
    assert schema(path) == []
    PlaybackWriters(SchemaStore(path),lambda key:key)
    assert len(schema(path)) == 3
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT position FROM web_progress').fetchone() == (42,)


@pytest.mark.parametrize('after',[1,2])
def test_process_exit_mid_schema_recovers_wal_without_partial_ddl_or_lost_progress(tmp_path, after):
    path=tmp_path/'test.db';seed(path)
    code = '''
import contextlib,os,sqlite3,sys
from anime.playback_writers import PlaybackWriters
class Store:
 @contextlib.contextmanager
 def connect(self):
  c=sqlite3.connect(sys.argv[1]);n=0
  def trace(sql):
   nonlocal n
   if sql.lstrip().startswith('CREATE'):
    n+=1
    if n>int(sys.argv[2]):os._exit(73)
  c.set_trace_callback(trace)
  yield c
  c.commit();c.close()
PlaybackWriters(Store(),lambda k:k)
'''
    result=subprocess.run([sys.executable,'-c',code,str(path),str(after)],cwd=Path(__file__).resolve().parents[1],
                          timeout=20,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==73
    assert schema(path)==[]
    PlaybackWriters(SchemaStore(path),lambda key:key)
    with sqlite3.connect(path) as c:
        assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert c.execute('SELECT position FROM web_progress').fetchone()==(42,)


def test_partial_old_autocommit_schema_and_missing_index_converge_idempotently(tmp_path):
    complete=tmp_path/'complete.db';PlaybackWriters(SchemaStore(complete),lambda key:key)
    expected=schema(complete)
    for prefix in range(3):
        path=tmp_path/f'partial-{prefix}.db'
        tables=[r for r in expected if r[0]=='table']
        with sqlite3.connect(path) as c:
            for _,_,sql in tables[:prefix]:c.execute(sql)
        writers=PlaybackWriters(SchemaStore(path),lambda key:key)
        assert writers.capabilities()['enabled'] is False
        assert schema(path)==expected
        PlaybackWriters(SchemaStore(path),lambda key:key)
        assert schema(path)==expected
        with sqlite3.connect(path) as c:
            for table in ('playback_writers','playback_claim_receipts'):
                assert c.execute('SELECT count(*) FROM '+table).fetchone()==(0,)
