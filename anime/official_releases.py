"""Verified official streaming schedules and bounded date-conflict review.

No network, media probing, acquisition or chat dispatch happens on a page read.
Broadcast catalog rows remain unchanged; release facts never grant production.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

ZONE = dt.timezone(dt.timedelta(hours=8))
SCHEDULE_PREFIX = 'official_release_schedule_'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def evidence_ok(db, evidence):
    try:
        root = db.path.parent.resolve()
        path = (root / evidence['path']).resolve()
        return (path.is_relative_to(root) and path.is_file()
                and path.stat().st_size <= 4 * 1024 * 1024
                and hashlib.sha256(path.read_bytes()).hexdigest() == evidence['sha256'])
    except (OSError, ValueError, TypeError, KeyError):
        return False


def validate_schedule(db, record, now, fresh=True):
    if not isinstance(record, dict) or record.get('version') != 1:
        raise ValueError('Invalid official schedule version')
    try:
        sid = record['show_id']
        show = db.show(sid)
        mapping = json.loads(show['mapping']) if show else {}
        source = urlsplit(record['source_url'])
        anchor = record['catalog_anchor']
        stream = dt.datetime.fromisoformat(record['stream_anchor_at'])
        verified, until = record['verified_at'], record['valid_until']
        if (not show or not show['selected'] or show['cleanup_hold']
                or record['mapping_sha256'] != digest(mapping)
                or record['season'] != (mapping.get('season') or 1)
                or record['original'] != show['original']
                or source.scheme != 'https' or not source.hostname or source.username or source.password
                or stream.utcoffset() is None
                or any(type(record[k]) is not int for k in ('episode_start', 'episode_end', 'interval_days'))
                or not 1 <= record['episode_start'] <= anchor['episode'] <= record['episode_end'] <= (show['total'] or 999)
                or not 1 <= record['interval_days'] <= 14
                or type(verified) not in (int, float) or type(until) not in (int, float)
                or not 0 < verified <= now + 30 or not verified < until <= verified + 8 * 86400
                or (fresh and now >= until) or not evidence_ok(db, record['evidence'])):
            raise ValueError('Official schedule identity, evidence or freshness invalid')
        dt.date.fromisoformat(anchor['date'])
        row = next((a for a in db.local_airings(sid) if a['episode'] == anchor['episode']), {})
        if row.get('source_episode') != anchor['source_episode'] or row.get('airdate') != anchor['date']:
            raise ValueError('Official schedule catalog anchor changed')
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError('Incomplete official schedule') from None
    return record


def _schedule(db, sid, now):
    # Request-local only: launch/write entrypoints revalidate the evidence anew.
    cache = db.__dict__.setdefault('_official_release_schedules', {}) if hasattr(db, 'con') else {}
    if (sid, now) in cache:
        return cache[(sid, now)]
    record = db.get(SCHEDULE_PREFIX + str(sid))
    try:
        if not isinstance(record, dict) or record.get('show_id') != sid:
            raise ValueError('Schedule belongs to another show')
        validate_schedule(db, record, now)
    except (OSError, ValueError, TypeError):
        record = None
    cache[(sid, now)] = record
    return record


def _release_for_record(record, episode, airing):
    if record is None:
        return None
    try:
        anchor = record['catalog_anchor']
        delta = episode - anchor['episode']
        expected = dt.date.fromisoformat(anchor['date']) + dt.timedelta(days=delta * record['interval_days'])
        if (not record['episode_start'] <= episode <= record['episode_end']
                or airing.get('source_episode', episode) != anchor['source_episode'] + delta
                or airing.get('airdate') != expected.isoformat()):
            return None
        return dt.datetime.fromisoformat(record['stream_anchor_at']) + dt.timedelta(days=delta * record['interval_days'])
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def scheduled_release(db, sid, episode, airing, now):
    return _release_for_record(_schedule(db, sid, now), episode, airing)


def _transaction_scope(c, record, episode, source_episode, catalog_date):
    show = c.execute('SELECT original,mapping,selected,cleanup_hold FROM shows WHERE id=?', (record['show_id'],)).fetchone()
    if not show or not show['selected'] or show['cleanup_hold'] or show['original'] != record['original']:
        return False
    mapping = json.loads(show['mapping'])
    fallback = c.execute('SELECT value FROM kv WHERE key=?', ('airing_sort_offset_' + str(record['show_id']),)).fetchone()
    offset = mapping.get('airing_offset') or (json.loads(fallback['value']) if fallback else 0)
    airing = c.execute('SELECT airdate FROM airings WHERE show_id=? AND episode=?',
                       (record['show_id'], source_episode)).fetchone()
    return (digest(mapping) == record['mapping_sha256'] and source_episode - offset == episode
            and airing is not None and airing['airdate'] == catalog_date)


def register_schedule(db, record, now=None, apply=False, expected_sha=None):
    now = time.time() if now is None else now
    validate_schedule(db, record, now)
    key = SCHEDULE_PREFIX + str(record['show_id'])
    old = db.get(key)
    old_sha = digest(old) if old is not None else None
    new_sha = digest(record)
    result = {'show_id': record['show_id'], 'old_sha256': old_sha, 'new_sha256': new_sha,
              'changed': old_sha != new_sha, 'applied': False}
    if not apply or old_sha == new_sha:
        return result
    if old is not None and old_sha != expected_sha:
        raise ValueError('Existing schedule changed; inspect plan and supply its expected SHA')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        if (digest(json.loads(current['value'])) if current else None) != old_sha:
            raise ValueError('Concurrent official schedule update')
        # Recheck catalog/mapping under the same write transaction.
        anchor = record['catalog_anchor']
        if not _transaction_scope(c, record, anchor['episode'], anchor['source_episode'], anchor['date']):
            raise ValueError('Schedule scope or anchor changed before apply')
        if old is not None:
            c.execute('INSERT OR IGNORE INTO kv VALUES(?,?)',
                      (key + '_history_' + old_sha, json.dumps(old, ensure_ascii=False)))
        c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(record, ensure_ascii=False)))
    return {**result, 'applied': True}


def _released_proposals(db, now):
    proposals = []
    watched = {(r['show_id'], r['episode']) for r in db.rows('SELECT show_id,episode FROM watches WHERE finished=1')}
    for item in db.rows("SELECT key,value FROM kv WHERE key GLOB 'official_release_schedule_[0-9]*'"):
        # History entries are immutable archives, not active schedules.
        if not item['key'][len(SCHEDULE_PREFIX):].isdigit():
            continue
        record = json.loads(item['value'])
        if not isinstance(record, dict) or str(record.get('show_id')) != item['key'][len(SCHEDULE_PREFIX):]:
            continue
        sid = record.get('show_id')
        try:
            validate_schedule(db, record, now)
        except (OSError, ValueError, TypeError):
            continue
        for airing in db.local_airings(sid):
            ep = airing['episode']
            if (sid, ep) in watched:
                continue
            released = _release_for_record(record, ep, airing)
            if released is None or released.timestamp() > now or released.astimezone(ZONE).date().isoformat() >= (airing.get('airdate') or ''):
                continue
            key = f'verified_early_release_{sid}_{ep}'
            if db.get(key) is None:
                proposals.append((key, { 'version': 1, 'show_id': sid, 'episode': ep, 'season': record['season'],
                    'source_episode': airing['source_episode'], 'catalog_airdate': airing['airdate'],
                    'released_at': released.isoformat(), 'source_url': record['source_url'],
                    'evidence': record['evidence'], 'schedule_sha256': digest(record),
                    'mapping_sha256': record['mapping_sha256'],
                    'verified_at': record['verified_at']}, record))
    return proposals


def reconcile_released_facts(db, now):
    """Refresh-only: retain elapsed verified dates across later schedule expiry."""
    from .read_projection import read_projection
    with read_projection(db.path) as snapshot:
        proposals = _released_proposals(snapshot, now)
    changed = 0
    if proposals:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for key, proof, record in proposals:
                current = c.execute('SELECT value FROM kv WHERE key=?', (SCHEDULE_PREFIX + str(proof['show_id']),)).fetchone()
                if (not current or digest(json.loads(current['value'])) != digest(record)
                        or not _transaction_scope(c, record, proof['episode'], proof['source_episode'], proof['catalog_airdate'])
                        or c.execute('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1',
                                     (proof['show_id'], proof['episode'])).fetchone()):
                    continue
                changed += c.execute('INSERT OR IGNORE INTO kv VALUES(?,?)', (key, json.dumps(proof, ensure_ascii=False))).rowcount
    return changed


def review_plan(db, now=None):
    """Find completed media/date conflicts even when no delivery job exists."""
    from .viewing_workflow import unreleased_date, scope_blocker
    now = time.time() if now is None else now
    if not hasattr(db, 'con'):
        from .read_projection import read_projection
        with read_projection(db.path) as snapshot:
            return review_plan(snapshot, now)
    tables = {r['name'] for r in db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    targets, items = {}, []
    today = dt.datetime.fromtimestamp(now, ZONE).date().isoformat()
    future_airings = {}
    for airing in db.rows('SELECT * FROM airings WHERE airdate>?', (today,)):
        future_airings.setdefault(airing['show_id'], []).append(airing)
    watched = {(r['show_id'], r['episode']) for r in db.rows('SELECT show_id,episode FROM watches WHERE finished=1')}
    for show in db.rows("SELECT id,title,state,original,mapping,air_date FROM shows WHERE selected=1 AND cleanup_hold=0"):
        sid = show['id']
        mapping = json.loads(show['mapping'])
        for airing in db.adjust_airings(mapping, future_airings.get(sid, []), db.get('airing_sort_offset_' + str(sid), 0)):
            targets[(sid, airing['episode'])] = (show, airing)
        if show['air_date'] and show['air_date'] > today and (sid, 1) not in targets:
            targets[(sid, 1)] = (show, {'episode': 1, 'source_episode': 1, 'airdate': show['air_date']})
        record = db.get(SCHEDULE_PREFIX + str(sid))
        if isinstance(record, dict) and record and show['state'] == 'watching':
            try:
                validate_schedule(db, record, now)
                due = record['valid_until'] - now <= 86400
            except (OSError, ValueError, TypeError):
                due = True
            if due and any(k[0] == sid for k in targets):
                condition = digest({'schedule': record, 'mapping': mapping,
                                    'airings': future_airings.get(sid, [])})
                review = db.get(f'official_release_review_{sid}_None', {})
                waiting = (review.get('condition') == condition and evidence_ok(db, review.get('evidence'))
                           and review.get('next_check_at', 0) > now)
                items.append({'show_id': sid, 'episode': None, 'title': show['title'],
                              'action': 'revalidate_official_schedule',
                              'status': 'retry_wait' if waiting else 'actionable',
                              'next_check_at': review.get('next_check_at') if waiting else None,
                              'condition': condition, 'source_url': record.get('source_url')})
    media = {}
    def add(sid, ep, path, size, identity):
        if (sid, ep) not in targets:
            return
        try:
            p = Path(path)
            if p.is_file() and (not size or p.stat().st_size == size):
                media.setdefault((sid, ep), {})[str(p)] = {'path': str(p), 'size': p.stat().st_size,
                                                           'mtime_ns': p.stat().st_mtime_ns, 'identity': identity}
        except OSError:
            pass
    for r in db.rows("SELECT show_id,episode,path,size,hash FROM episodes WHERE status='complete'"):
        add(r['show_id'], r['episode'], r['path'], r['size'], r['hash'])
    for table in ('subtitle_samples', 'subtitle_studio_media'):
        columns = {r['name'] for r in db.rows(f'PRAGMA table_info({table})')} if table in tables else set()
        if {'path', 'show_id', 'episode'} <= columns:
            for r in db.rows(f"SELECT * FROM {table} WHERE path<>''"):
                add(r['show_id'], r['episode'], r['path'], r.get('size', 0), r.get('hash') or r.get('fingerprint'))
    for r in db.rows("SELECT show_id,hash,selection,save_path FROM tasks WHERE status IN ('complete','linked')"):
        try:
            root = Path(r['save_path']).resolve()
            for ep, f in json.loads(r['selection']).get('selected', {}).items():
                path = (root / f['name']).resolve()
                if str(ep).isdigit() and path.is_relative_to(root):
                    add(r['show_id'], int(ep), path, f['size'], r['hash'])
        except (OSError, ValueError, TypeError, KeyError):
            continue
    for (sid, ep), files in sorted(media.items()):
        show, airing = targets[(sid, ep)]
        if (sid, ep) in watched:
            continue
        future = unreleased_date(db, sid, ep, now)
        if not future:
            continue
        condition = digest({'show_id': sid, 'episode': ep, 'mapping': show['mapping'], 'airing': airing,
                            'media': sorted(files.values(), key=lambda f: f['path'])})
        review = db.get(f'official_release_review_{sid}_{ep}', {})
        waiting = (review.get('condition') == condition and evidence_ok(db, review.get('evidence'))
                   and review.get('next_check_at', 0) > now)
        official_time = scheduled_release(db, sid, ep, airing, now)
        awaiting = official_time is not None and official_time.timestamp() > now
        items.append({'show_id': sid, 'episode': ep, 'title': show['title'], 'catalog_airdate': airing['airdate'],
                      'source_episode': airing['source_episode'], 'condition': condition,
                      'action': 'verify_official_release',
                      'status': 'awaiting_official_release' if awaiting else 'retry_wait' if waiting else 'actionable',
                      'next_check_at': official_time.timestamp() if awaiting else review.get('next_check_at') if waiting else None,
                      'production_scope_blocker': scope_blocker(db, sid, ep) or
                          ('个人状态尚未进入在追字幕制作' if show['state'] != 'watching' else ''),
                      'media': list(files.values())})
    return {'schema': 1, 'checked_at': now, 'items': items,
            'next_action': next((i for i in items if i['status'] == 'actionable'), None),
            'summary': {s: sum(i['status'] == s for i in items)
                        for s in ('actionable', 'retry_wait', 'awaiting_official_release')}}


def record_review(db, report, now=None, apply=False):
    now = time.time() if now is None else now
    key = f"official_release_review_{report.get('show_id')}_{report.get('episode')}"
    old = db.get(key, {})
    if old.get('receipt_sha256') == digest(report):
        return {'applied': False, 'idempotent': True}
    row = next((r for r in review_plan(db, now)['items'] if r['show_id'] == report.get('show_id')
                and r['episode'] == report.get('episode') and r['condition'] == report.get('condition')), None)
    if (not row or report.get('outcome') not in ('source_unavailable', 'not_formal', 'awaiting_official_release')
            or not evidence_ok(db, report.get('evidence'))):
        raise ValueError('Review must bind the current media/date condition and actual evidence')
    key = f"official_release_review_{row['show_id']}_{row['episode']}"
    if row['status'] != 'actionable':
        raise ValueError('Review condition is cooling down; do not repeat the same failed lookup')
    count = old.get('attempts', 0) + 1 if old.get('condition') == row['condition'] else 1
    value = {**report, 'recorded_at': now, 'receipt_sha256': digest(report), 'attempts': count,
             'next_check_at': now + (1800 if count < 2 else 21600)}
    if apply:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            if (json.loads(current['value']) if current else {}) != old:
                raise ValueError('Concurrent release review update; read back before retrying')
            c.execute('INSERT OR IGNORE INTO kv VALUES(?,?)', (key + '_history_' + digest(report), json.dumps(value, ensure_ascii=False)))
            c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
    return {'applied': apply, 'review': value}
