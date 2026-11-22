"""Version-bound progress for published subtitle deliveries; watches stay shared."""
import hashlib
import json
import math
import re
import stat
import time
from fastapi import HTTPException
from .db import ROOT


class FinishedViewing:
    def __init__(self, db, root=ROOT):
        self.db, self.root = db, root
        db.execute('''CREATE TABLE IF NOT EXISTS finished_playback_receipts(
            session TEXT PRIMARY KEY, media_id TEXT NOT NULL, sequence INTEGER NOT NULL, updated REAL NOT NULL)''')

    def catalog(self):
        return self._catalog()

    def _catalog(self, identity=None):
        path = self.root / 'static/finished-media/index.json'
        if not path.exists(): return []
        releases = []
        for r in json.loads(path.read_text(encoding='utf-8-sig')).get('releases', []):
            if identity is not None and r.get('id') != identity: continue
            if (r.get('status') != 'finished' or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', str(r.get('id', '')))
                or not re.fullmatch(r'[a-f0-9]{64}', str(r.get('video_sha256', '')))
                or any(type(r.get(k)) is not int or r[k] < 1 for k in ('show_id', 'season', 'episode'))): continue
            key = 'finished:' + hashlib.sha256((r['id'] + ':' + r['video_sha256']).encode()).hexdigest()[:32]
            releases.append({**r, 'media_id': key})
        if not releases: return []

        # Join current viewing state once, rather than opening four SQLite
        # connections per release. Bound batches also support older SQLite
        # builds with a 999-parameter limit; one read snapshot spans all batches.
        records = {}
        with self.db.connect() as c:
            c.execute('BEGIN')
            for start in range(0, len(releases), 200):
                batch = releases[start:start + 200]
                values = ','.join('(?,?,?,?)' for _ in batch)
                params = [value for i, r in enumerate(batch, start)
                          for value in (i, r['show_id'], r['episode'], r['media_id'])]
                records.update((row['ordinal'], dict(row)) for row in c.execute(f'''
                    WITH requested(ordinal,show_id,episode,media_id) AS (VALUES {values})
                    SELECT r.ordinal,s.mapping,s.state,w.finished,p.media_id progress_id,
                           p.position,p.duration,p.last_played,p.updated
                    FROM requested r JOIN shows s ON s.id=r.show_id
                    LEFT JOIN watches w ON w.show_id=r.show_id AND w.episode=r.episode
                    LEFT JOIN web_progress p ON p.media_id=r.media_id''', params))

        rows = []
        for i, r in enumerate(releases):
            record = records.get(i)
            if not record or int(json.loads(record['mapping']).get('season') or 1) != r['season']: continue
            media = self.root / 'static/finished-media' / r['id'] / 'episode.mp4'
            try:
                media_stat = media.stat()
                if not stat.S_ISREG(media_stat.st_mode) or not media_stat.st_size: continue
            except OSError: continue
            progress = ({k: record[k] for k in ('position', 'duration', 'last_played', 'updated')}
                        if record['progress_id'] is not None else None)
            rows.append({'id': r['id'], 'show_id': r['show_id'], 'season': r['season'], 'episode': r['episode'],
                         'video_sha256': r['video_sha256'], 'media_id': r['media_id'], 'progress': progress,
                         'finished': bool(record['finished']), 'show_state': record['state']})
        return rows

    def item(self, identity):
        row = next((r for r in self._catalog(identity) if r['id'] == identity), None)
        if row is None: raise HTTPException(404, '成品不存在或作品季集归属已变化')
        return row

    def media(self, identity):
        """Expose a delivery to the existing player without inserting an episode."""
        record = self.item(identity)
        index = json.loads((self.root / 'static/finished-media/index.json').read_text(encoding='utf-8-sig'))
        release = next(r for r in index['releases'] if r['id'] == identity)
        path = self.root / 'static/finished-media' / identity / 'episode.mp4'
        return {'id': 'finished:' + identity, 'release_id': identity, 'name': identity + '.mp4',
                'folder': self.db.show(record['show_id'])['title'], 'path': str(path),
                'size': path.stat().st_size, 'legacy': False, 'show_id': record['show_id'],
                'episode': record['episode'], 'progress': record['progress'],
                'finished': record['finished'], 'release': release}

    def progress(self, identity, p):
        row = self.item(identity)
        try: pos, duration = float(p['position']), float(p['duration'])
        except (KeyError, TypeError, ValueError): raise ValueError('播放进度无效') from None
        if not all(math.isfinite(x) and 0 <= x <= 86400 for x in (pos, duration)) or duration <= 0 or pos > duration + 1:
            raise ValueError('播放进度无效')
        session, sequence = playback_order(p)
        if session is None: raise ValueError('播放会话无效')
        now = time.time()
        with self.db.gate, self.db.connect() as c:
            from .playback_writers import guard_legacy_write
            c.execute('BEGIN IMMEDIATE')
            guard_legacy_write(self.db, c, row['media_id'])
            if not in_order(c, row['media_id'], session, sequence, now): return {'ok': True, 'ignored': True}
            c.execute('''INSERT INTO web_progress(media_id,position,duration,last_played,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                last_played=coalesce(excluded.last_played,web_progress.last_played),updated=excluded.updated''',
                (row['media_id'], pos, duration, now if p.get('playing') is True and pos > 0 else None, now))
        # Opening, seeking and reaching the end never change watches.
        return {'ok': True}


def playback_order(p):
    """(session, sequence) when the client sent a valid pair, (None, None) when absent, ValueError when malformed."""
    session, sequence = p.get('session'), p.get('sequence')
    if session is None and sequence is None: return None, None
    if (not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', session)
        or type(sequence) is not int or not 1 <= sequence <= 2147483647): raise ValueError('播放会话无效')
    return session, sequence


def in_order(c, media_id, session, sequence, now):
    """Record this session's newest sequence; a late or foreign write must not move the saved position."""
    receipt = c.execute('SELECT media_id,sequence FROM finished_playback_receipts WHERE session=?', (session,)).fetchone()
    if receipt and (receipt[0] != media_id or receipt[1] >= sequence): return False
    c.execute('INSERT INTO finished_playback_receipts VALUES(?,?,?,?) ON CONFLICT(session) DO UPDATE SET sequence=excluded.sequence,updated=excluded.updated',
              (session, media_id, sequence, now))
    c.execute('DELETE FROM finished_playback_receipts WHERE updated<?', (now - 7 * 86400,))
    return True


def register_finished_viewing(app, db, mark_watch):
    viewing = FinishedViewing(db)
    app.state.finished_viewing = viewing
    app.state.webplayer.finished_viewing = viewing

    @app.get('/api/finished-viewing')
    def catalog(): return {'releases': viewing.catalog()}

    @app.get('/api/finished-viewing/{identity}')
    def item(identity: str): return viewing.item(identity)

    @app.post('/api/finished-viewing/{identity}/progress')
    def progress(identity: str, p: dict): return viewing.progress(identity, p)

    @app.post('/api/finished-viewing/{identity}/watch')
    def watch(identity: str, p: dict):
        with db.gate:
            row = viewing.item(identity)
            return mark_watch(row['show_id'], {'episode': row['episode'], 'finished': p.get('finished') is True})

    @app.post('/api/finished-viewing/{identity}/viewing-session')
    def lease(identity: str, p: dict):
        row = viewing.item(identity)
        return app.state.engine.subtitle_studio.production_targets.lease(
            {**row, 'id': row['media_id']}, p.get('session'), p.get('closed') is True)
