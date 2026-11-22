"""Read current home playback rows in one bounded database snapshot."""
import hashlib
import json
import stat
from pathlib import Path

from .show_list import chunks
from .source_blacklist import KEY, blacklist_blocker


def home_summary(db):
    with db.connect() as connection:
        connection.execute('BEGIN')
        data = [dict(row) for row in connection.execute('''
            SELECT e.show_id,e.episode,e.path,e.size,s.title,s.image,s.total,s.watched,s.state,
                   e.first_completed,t.title source_title,t.selection source_selection
            FROM episodes e JOIN shows s ON s.id=e.show_id LEFT JOIN tasks t ON t.hash=e.hash
            WHERE e.status='complete' AND s.state NOT IN ('dropped','paused','completed')
              AND NOT EXISTS(SELECT 1 FROM watches w WHERE w.show_id=e.show_id
                             AND w.episode=e.episode AND w.finished=1)
            ORDER BY e.first_completed DESC,e.show_id,e.episode''')]
        row = connection.execute('SELECT value FROM kv WHERE key=?', (KEY,)).fetchone()
        blacklist = json.loads(row['value']) if row else []
        entries = []
        for row in data:
            title, selection = row.pop('source_title'), row.pop('source_selection')
            path = Path(row.pop('path') or '')
            size = row.pop('size')
            if blacklist and (blacklist_blocker(blacklist, title, selection) or blacklist_blocker(blacklist, path.name)): continue
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size != size: continue
            except OSError: continue
            row['media_id'] = hashlib.sha256(str(path).lower().encode()).hexdigest()[:32]
            entries.append(row)
        progress = {}
        keys = list({row['media_id'] for row in entries})
        for batch in chunks(keys):
            marks = ','.join('?' for _ in batch)
            progress.update((row['media_id'], {key: row[key] for key in ('position', 'duration', 'last_played')})
                            for row in connection.execute(f'''
                                SELECT media_id,position,duration,last_played FROM web_progress
                                WHERE media_id IN ({marks})''', batch))
        watching = connection.execute("SELECT count(*) FROM shows WHERE selected=1 AND state IN ('trial','watching')").fetchone()[0]
    for row in entries:
        row['progress'] = progress.get(row['media_id'])
    resume = [row for row in entries if row['progress'] and row['progress']['last_played']
              and 0 < row['progress']['position'] < row['progress']['duration'] - 30]
    resume.sort(key=lambda row: row['progress']['last_played'], reverse=True)
    first = {}
    for row in entries:
        if row['show_id'] not in first or row['episode'] < first[row['show_id']]['episode']:
            first[row['show_id']] = row
    return {'resume': resume[:8], 'next': list(first.values())[:18],
            'unwatched': len(entries), 'watching': watching}
