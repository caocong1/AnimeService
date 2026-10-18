"""Writer-v2 core and passive legacy fences. No production v2 routes or activation.

The future authenticated HTTP adapter must inject a canonical media resolver and
call guard_legacy in the SAME transaction as every legacy progress write. This
core is not a viewing lease, watched-state writer or subtitle owner.
"""
import hashlib
import json
import math
import re
import time
import unicodedata
from pathlib import Path

PROTOCOL = 'playback-writer/v2'
MAX_GENERATION = 9007199254740991


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def identifier(value, name, minimum=16, maximum=80):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{%d,%d}' % (minimum, maximum), value):
        raise ValueError(f'invalid {name}')
    return value


def integer(value, name, minimum=0, maximum=MAX_GENERATION):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'invalid {name}')
    return value


class WriterConflict(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class PlaybackMediaResolver:
    """Resolve server inventory/release aliases to the EXISTING progress row.

    Ordinary identity retains the inventory's path-derived key and size check;
    this does not claim to hash or detect same-size replacement bytes. Finished
    identity uses the trusted release manifest's video signature. No caller may
    supply its own path, signature or alias map.
    """
    def __init__(self, player):
        self.player = player

    def __call__(self, requested):
        if not isinstance(requested, str) or not 1 <= len(requested) <= 200:
            raise ValueError('invalid media identity')
        if requested.startswith('finished:'):
            viewing = getattr(self.player, 'finished_viewing', None)
            if viewing is None:
                raise ValueError('finished media unavailable')
            matches = [row for row in viewing.catalog()
                       if requested in ('finished:' + row['id'], row['media_id'])]
            keys = {row['media_id'] for row in matches}
            if len(keys) != 1:
                raise ValueError('unknown or ambiguous finished media identity')
            # Revalidate existence using the same server path checks as playback.
            checked = self.player.item('finished:' + matches[0]['id'])
            if (checked.get('release_id') != matches[0]['id']
                    or checked.get('release', {}).get('video_sha256') != matches[0]['video_sha256']):
                raise ValueError('finished media identity changed during lookup')
            return keys.pop()
        if not re.fullmatch('[0-9a-f]{32}', requested):
            raise ValueError('unknown media identity')
        row = self.player.item(requested)
        key = hashlib.sha256(str(Path(row['path'])).lower().encode()).hexdigest()[:32]
        if row.get('id') != requested or key != requested:
            raise ValueError('media inventory identity changed')
        return key


def guard_legacy_progress(c, media_key, *, enabled=False):
    """Passive fence in an ALREADY OPEN write transaction; never create tables.

    No v2 tables and disabled is a no-op. Durable generations fence forever,
    including expiry/release/feature-off. Caller must hold BEGIN IMMEDIATE so a
    concurrent claim cannot commit between this check and the progress write.
    """
    if not c.in_transaction:
        raise ValueError('legacy progress guard requires an explicit transaction')
    if enabled is True:
        raise WriterConflict('session_required')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='playback_writers'").fetchone():
        return
    row = c.execute('SELECT generation FROM playback_writers WHERE media_key=?', (media_key,)).fetchone()
    if row and (type(row[0]) is not int or row[0] != 0):
        raise WriterConflict('session_required')


def guard_legacy_write(db, c, media_key):
    # A future trusted adapter may install this callback on the shared Store.
    # No production caller sets it today; never read enablement from a packet.
    enabled = getattr(db, 'playback_writer_enabled', None)
    guard_legacy_progress(c, media_key, enabled=callable(enabled) and enabled() is True)


class PlaybackWriters:
    def __init__(self, db, resolve_media, *, enabled=None, clock=None, ttl=120, keep_days=7):
        self.db, self.resolve_media = db, resolve_media
        self.enabled = enabled or (lambda: False)
        self.clock = clock or time.time
        self.ttl = integer(ttl, 'ttl', 15, 600)
        self.keep_days = integer(keep_days, 'keep_days', 1, 30)
        with db.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS playback_writers(
                  media_key TEXT PRIMARY KEY,generation INTEGER NOT NULL,session TEXT NOT NULL,
                  token_digest TEXT NOT NULL,device_id TEXT NOT NULL,sequence INTEGER NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS playback_claim_receipts(
                  request_id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,result TEXT NOT NULL,recorded_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS playback_claim_receipts_age ON playback_claim_receipts(recorded_at);
            ''')

    def capabilities(self):
        # Internal only. Production must not advertise until Web AND signed TV are ready.
        return {'protocol': PROTOCOL, 'enabled': self.enabled() is True}

    def key(self, requested):
        if not isinstance(requested, str) or not 1 <= len(requested) <= 200:
            raise ValueError('invalid media identity')
        key = self.resolve_media(requested)  # trusted server resolver, not a client alias assertion
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError('unknown media identity')
        return key

    @staticmethod
    def public(row, key, now):
        active = bool(row and row['expires'] > now)
        return {'media_key': key, 'generation': row['generation'] if row else 0,
                'active': active, 'expires': row['expires'] if row else 0,
                'device_id': row['device_id'] if active else None}

    def state(self, media_key):
        key = self.key(media_key)
        row = self.db.one('SELECT * FROM playback_writers WHERE media_key=?', (key,))
        return self.public(row, key, self.clock())

    def claim(self, packet):
        key = self.key(packet.get('media_key'))
        session = identifier(packet.get('session'), 'session')
        token = identifier(packet.get('writer_token'), 'writer_token', 32, 128)
        request = identifier(packet.get('request_id'), 'request_id')
        expected = integer(packet.get('expected_generation'), 'expected_generation', 0, MAX_GENERATION - 1)
        mode = packet.get('mode')
        device = packet.get('device_id')
        if mode not in ('open', 'takeover'):
            raise ValueError('invalid claim mode')
        if (not isinstance(device, str) or not 1 <= len(device) <= 64
                or any(unicodedata.category(ch).startswith('C') for ch in device) or token in device):
            raise ValueError('invalid device label')
        request_digest = digest(canonical({'media_key': key, 'session': session, 'token_digest': digest(token),
                                          'device_id': device, 'expected_generation': expected, 'mode': mode}))
        now = self.clock()
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM playback_claim_receipts WHERE recorded_at<?', (now - self.keep_days * 86400,))
            receipt = c.execute('SELECT * FROM playback_claim_receipts WHERE request_id=?', (request,)).fetchone()
            if receipt:
                if receipt['request_digest'] != request_digest:
                    raise ValueError('claim request id reused with different identity')
                return {**json.loads(receipt['result']), 'replayed': True}
            row = c.execute('SELECT * FROM playback_writers WHERE media_key=?', (key,)).fetchone()
            state = self.public(row, key, now)
            reason = ('disabled' if self.enabled() is not True else
                      'generation_moved' if expected != state['generation'] else
                      'takeover_required' if state['active'] and mode == 'open' else '')
            if reason:
                result = {'ok': False, 'reason': reason, **state}
            else:
                generation = state['generation'] + 1
                c.execute('''INSERT INTO playback_writers VALUES(?,?,?,?,?,0,?)
                    ON CONFLICT(media_key) DO UPDATE SET generation=excluded.generation,session=excluded.session,
                    token_digest=excluded.token_digest,device_id=excluded.device_id,sequence=0,expires=excluded.expires''',
                          (key, generation, session, digest(token), device, now + self.ttl))
                result = {'ok': True, 'media_key': key, 'generation': generation, 'expires': now + self.ttl}
            c.execute('INSERT INTO playback_claim_receipts VALUES(?,?,?,?)',
                      (request, request_digest, canonical(result), now))
            return result

    def guard_legacy(self, c, media_key):
        """Call within the progress write's transaction, including after feature rollback.

        Never delete generation rows to re-enable legacy; expiry/release do not
        authorize old clients. An HTTP adapter must map this to structured 409.
        """
        key = self.key(media_key)
        guard_legacy_progress(c, key, enabled=self.enabled() is True)

    def _packet(self, packet):
        return (identifier(packet.get('session'), 'session'),
                integer(packet.get('generation'), 'generation', 1),
                digest(identifier(packet.get('writer_token'), 'writer_token', 32, 128)),
                integer(packet.get('sequence'), 'sequence', 1, 2147483647))

    def update(self, media_key, packet, *, action='heartbeat'):
        if action not in ('progress', 'heartbeat', 'release'):
            raise ValueError('invalid writer action')
        key = self.key(media_key)
        session, generation, token_digest, sequence = self._packet(packet)
        if action == 'progress':
            try:
                if isinstance(packet['position'], bool) or isinstance(packet['duration'], bool):
                    raise ValueError()
                position, duration = float(packet['position']), float(packet['duration'])
            except (KeyError, TypeError, ValueError):
                raise ValueError('invalid progress') from None
            if not (all(math.isfinite(x) and 0 <= x <= 86400 for x in (position, duration))
                    and duration > 0 and position <= duration + 1):
                raise ValueError('invalid progress')
        now = self.clock()
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM playback_writers WHERE media_key=?', (key,)).fetchone()
            reason = ('disabled' if self.enabled() is not True else
                      'fenced' if not row or row['generation'] != generation or row['session'] != session or row['token_digest'] != token_digest else
                      'expired' if row['expires'] <= now else
                      'out_of_order' if sequence <= row['sequence'] else '')
            if reason:
                return {'ok': True, 'ignored': True, 'reason': reason}
            c.execute('UPDATE playback_writers SET sequence=?,expires=? WHERE media_key=?',
                      (sequence, 0 if action == 'release' else now + self.ttl, key))
            if action == 'progress':
                c.execute('''INSERT INTO web_progress(media_id,position,duration,last_played,updated) VALUES(?,?,?,?,?)
                    ON CONFLICT(media_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,
                    last_played=coalesce(excluded.last_played,web_progress.last_played),updated=excluded.updated''',
                          (key, position, duration, now if packet.get('playing') is True and position > 0 else None, now))
            return {'ok': True, 'generation': generation, 'sequence': sequence}
