"""Version-bound progress for published subtitle deliveries; watches stay shared."""
import hashlib
import json
import math
import re
import time
from fastapi import HTTPException
from .db import ROOT


class FinishedViewing:
    def __init__(self, db, root=ROOT):
        self.db, self.root = db, root
        db.execute('''CREATE TABLE IF NOT EXISTS finished_playback_receipts(
            session TEXT PRIMARY KEY, media_id TEXT NOT NULL, sequence INTEGER NOT NULL, updated REAL NOT NULL)''')

    def catalog(self):
        path = self.root / 'static/finished-media/index.json'
        if not path.exists(): return []
        rows = []
        for r in json.loads(path.read_text(encoding='utf-8-sig')).get('releases', []):
            if (r.get('status') != 'finished' or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', str(r.get('id', '')))
                or not re.fullmatch(r'[a-f0-9]{64}', str(r.get('video_sha256', '')))
                or any(type(r.get(k)) is not int or r[k] < 1 for k in ('show_id', 'season', 'episode'))): continue
            show = self.db.show(r['show_id'])
            if not show or int(self.db.mapping(r['show_id']).get('season') or 1) != r['season']: continue
            media = self.root / 'static/finished-media' / r['id'] / 'episode.mp4'
            try:
                if not media.is_file() or not media.stat().st_size: continue
            except OSError: continue
            key = 'finished:' + hashlib.sha256((r['id'] + ':' + r['video_sha256']).encode()).hexdigest()[:32]
            progress = self.db.one('SELECT position,duration,last_played,updated FROM web_progress WHERE media_id=?', (key,))
            watch = self.db.one('SELECT finished FROM watches WHERE show_id=? AND episode=?', (r['show_id'], r['episode']))
            rows.append({'id': r['id'], 'show_id': r['show_id'], 'season': r['season'], 'episode': r['episode'],
                         'video_sha256': r['video_sha256'], 'media_id': key, 'progress': progress,
                         'finished': bool(watch and watch['finished']), 'show_state': show['state']})
        return rows

    def item(self, identity):
        row = next((r for r in self.catalog() if r['id'] == identity), None)
        if row is None: raise HTTPException(404, '成品不存在或作品季集归属已变化')
        return row

    def progress(self, identity, p):
        row = self.item(identity)
        try: pos, duration = float(p['position']), float(p['duration'])
        except (KeyError, TypeError, ValueError): raise ValueError('播放进度无效') from None
        if not all(math.isfinite(x) and 0 <= x <= 86400 for x in (pos, duration)) or duration <= 0 or pos > duration + 1:
            raise ValueError('播放进度无效')
        session, sequence = p.get('session'), p.get('sequence')
        if (not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', session)
            or type(sequence) is not int or not 1 <= sequence <= 2147483647): raise ValueError('播放会话无效')
        now = time.time()
        with self.db.gate, self.db.connect() as c:
            receipt = c.execute('SELECT media_id,sequence FROM finished_playback_receipts WHERE session=?', (session,)).fetchone()
            if receipt and (receipt[0] != row['media_id'] or receipt[1] >= sequence): return {'ok': True, 'ignored': True}
            c.execute('''INSERT INTO web_progress(media_id,position,duration,last_played,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                last_played=coalesce(excluded.last_played,web_progress.last_played),updated=excluded.updated''',
                (row['media_id'], pos, duration, now if p.get('playing') is True and pos > 0 else None, now))
            c.execute('INSERT INTO finished_playback_receipts VALUES(?,?,?,?) ON CONFLICT(session) DO UPDATE SET sequence=excluded.sequence,updated=excluded.updated',
                      (session, row['media_id'], sequence, now))
            c.execute('DELETE FROM finished_playback_receipts WHERE updated<?', (now - 7 * 86400,))
        # Opening, seeking and reaching the end never change watches.
        return {'ok': True}


def register_finished_viewing(app, db, mark_watch):
    viewing = FinishedViewing(db)
    app.state.finished_viewing = viewing

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
