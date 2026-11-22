"""After a burst that a long reader pinned, the WAL shrinks back to the limit instead of keeping its peak size."""
import os,sqlite3

from anime.db import Store,WAL_LIMIT


def test_wal_shrinks_after_a_pinned_burst(tmp_path):
    db=Store(tmp_path/'test.db');wal=str(db.path)+'-wal'
    keeper=sqlite3.connect(db.path);keeper.execute('SELECT count(*) FROM sqlite_schema').fetchone()  # like the app's lifespan keeper: the WAL file is not deleted between requests
    reader=sqlite3.connect(db.path);reader.execute('BEGIN');reader.execute('SELECT count(*) FROM kv').fetchone()
    try:
        blob='x'*(1024*1024)
        for i in range(40):db.set(f'burst_{i}',blob)
        assert os.path.getsize(wal)>WAL_LIMIT  # the open snapshot kept every frame
    finally:reader.close()
    with db.connect() as c:assert c.execute('PRAGMA journal_size_limit').fetchone()[0]==WAL_LIMIT
    for i in range(3):db.set('after',i)
    with db.connect() as c:c.execute('PRAGMA wal_checkpoint(PASSIVE)')
    db.set('after','reset')
    try:assert os.path.getsize(wal)<=WAL_LIMIT and db.get('after')=='reset' and db.get('burst_39')==blob
    finally:keeper.close()
