"""Read-only report: where the board's airing numbering and Bangumi sync numbering disagree.

Usage: python scripts/airing_numbering_diff.py <path-to-database-copy> [show_id ...]

Opens the database read-only (no migration, no writes), takes each selected show's mapping,
its airings rows and the cached chapter list (kv bangumi_episodes_<show_id>), and prints one
JSON line per show: the saved airing_offset (absent / 0 / n), the verified sort shift, the
episode numbers the board shows, the episode numbers sync would use, and whether they differ.
Titles are not printed. Run it on a copy before changing any numbering rule.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anime.bangumi import episode_map  # noqa: E402
from anime.db import Store  # noqa: E402


class ReadOnly:
    """The two accessors episode_map needs, over a read-only connection."""
    def __init__(self, path):
        self.con = sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro', uri=True)
        self.con.row_factory = sqlite3.Row

    def rows(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def get(self, key, default=None):
        row = self.one('SELECT value FROM kv WHERE key=?', (key,))
        return json.loads(row['value']) if row else default

    def mapping(self, sid):
        return json.loads(self.one('SELECT mapping FROM shows WHERE id=?', (sid,))['mapping'])


def report(db, sid):
    mapping = db.mapping(sid)
    saved = mapping.get('airing_offset')
    shift = db.get('airing_sort_offset_' + str(sid), 0)
    airings = db.rows('SELECT * FROM airings WHERE show_id=? ORDER BY episode', (sid,))
    board = sorted(a['episode'] for a in Store.adjust_airings(mapping, airings, shift if not saved else 0))
    items = db.get('bangumi_episodes_' + str(sid), {}).get('items', [])
    try:
        sync = sorted(episode_map(db, sid, items)) if items else None
        note = '' if items else 'no cached chapters'
    except ValueError as error:
        sync, note = None, str(error)
    return {'show_id': sid, 'airing_offset': 'absent' if saved is None else saved, 'sort_shift': shift,
            'proof': bool(db.get('bangumi_sync_numbering_' + str(sid))), 'board': board, 'sync': sync,
            'differs': sync is not None and board != sync, 'note': note}


def main(argv):
    if not argv:
        print(__doc__); return 2
    db = ReadOnly(argv[0])
    wanted = [int(x) for x in argv[1:]] or [r['id'] for r in db.rows('SELECT id FROM shows WHERE selected=1 ORDER BY id')]
    differing = 0
    for sid in wanted:
        line = report(db, sid)
        differing += line['differs']
        print(json.dumps(line, ensure_ascii=False))
    print(json.dumps({'shows': len(wanted), 'differing': differing}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
