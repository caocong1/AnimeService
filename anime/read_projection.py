"""One SQLite read transaction for multi-query projections; no initialization."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .db import Store


class ProjectionStore(Store):
    def __init__(self, path):
        self.path = Path(path)
        self.con = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA query_only=ON')
        self.con.execute('BEGIN')
        self._shows = {}
        self._values = {}

    def rows(self, sql, args=()):
        return [dict(row) for row in self.con.execute(sql, args)]

    def execute(self, *args, **kwargs):
        raise ValueError('Projection is read-only')

    def show(self, sid):
        if sid not in self._shows:
            self._shows[sid] = super().show(sid)
        row = self._shows[sid]
        # Callers may annotate their row; never share that mutation with another
        # consumer of the same immutable SQLite snapshot.
        return dict(row) if row is not None else None

    def get(self, key, default=None):
        import json
        if key not in self._values:
            self._values[key] = self.one('SELECT value FROM kv WHERE key=?', (key,))
        row = self._values[key]
        # Decode a fresh value, retaining Store's missing/default and mutation
        # semantics. These lookups live only until this read transaction closes.
        return json.loads(row['value']) if row else default

    @contextmanager
    def connect(self):
        yield self.con


@contextmanager
def read_projection(path):
    db = ProjectionStore(path)
    try:
        yield db
    finally:
        db.con.close()
