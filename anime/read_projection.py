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

    def rows(self, sql, args=()):
        return [dict(row) for row in self.con.execute(sql, args)]

    def execute(self, *args, **kwargs):
        raise ValueError('Projection is read-only')

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
